"""Auditable model relevance decisions; raw technical evidence remains intact."""
import json
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
from urllib.parse import urldefrag, urlsplit
import store
from document_inventory import document_identity
from store import one,execute,now,event,db


SELF_LINK_POLICY = 'page-self-link-v1'
DOCUMENT_HASH_POLICY = 'document-sha256-only-v1'
_SHA256 = re.compile(r'[a-f0-9]{64}\Z')


def _verified_original(snapshot):
    """Read only a bound regular original through non-following directory FDs."""
    relative=snapshot.get('file'); expected=snapshot.get('sha256')
    if (not isinstance(relative,str) or '\\' in relative or ':' in relative
            or not isinstance(expected,str) or not _SHA256.fullmatch(expected)):
        return False
    rel=PurePosixPath(relative);root=Path(store.DATA)
    if (not root.is_absolute() or root.resolve()!=root or root.is_symlink()
            or len(rel.parts)!=3 or rel.parts[0]!='artifacts' or rel.is_absolute()
            or any(part in ('.','..') for part in rel.parts) or relative!=rel.as_posix()):
        return False
    handles=[]
    try:
        descriptor=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);handles.append(descriptor)
        for part in rel.parts[:-1]:
            descriptor=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor);handles.append(descriptor)
        descriptor=os.open(rel.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=descriptor)
        with os.fdopen(descriptor,'rb') as source:
            info=os.fstat(source.fileno())
            return (stat.S_ISREG(info.st_mode) and 0<=info.st_size<=30*1024**2
                    and hashlib.file_digest(source,'sha256').hexdigest()==expected)
    except (OSError,ValueError):
        return False
    finally:
        for descriptor in reversed(handles):os.close(descriptor)


def review_document_hash_change(change_id,run_id=None):
    """Revalidate a byte change without reading text or invoking a model."""
    with db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        value=connection.execute('SELECT * FROM changes WHERE id=?',(change_id,)).fetchone()
        row=dict(value) if value else None
        if not row or row['kind']!='Datei geändert' or not row['previous_id'] or (run_id is not None and row['run_id']!=run_id):return None
        try:detail=json.loads(row['evidence'])
        except (TypeError,ValueError):return None
        marker=detail.get('document_comparison') if isinstance(detail,dict) else None
        if not isinstance(marker,dict) or marker.get('policy')!=DOCUMENT_HASH_POLICY or marker.get('mode')!='sha256':return None
        sources=[]
        for sid in (row['previous_id'],row['current_id']):
            source=connection.execute('SELECT * FROM snapshots WHERE id=?',(sid,)).fetchone()
            sources.append(dict(source) if source else None)
        previous,current=sources
        if (not previous or not current or previous['kind']!='document' or current['kind']!='document'
                or previous['project_id']!=row['project_id'] or current['project_id']!=row['project_id']
                or previous['run_id']==current['run_id'] or current['run_id']!=row['run_id']
                or previous['sha256']==current['sha256']):return None
        try:
            identity=document_identity(_source_url(row['url']) or '')
            if not identity or any(document_identity(_source_url(s['url']) or '')!=identity for s in sources):return None
        except ValueError:return None
        expected={'previous_snapshot_id':previous['id'],'current_snapshot_id':current['id'],
                  'previous_sha256':previous['sha256'],'current_sha256':current['sha256']}
        if any(marker.get(key)!=value for key,value in expected.items()):return None
        if not all(_verified_original(source) for source in sources):return None
        reason='Die vollständige Originaldatei hat sich geändert. Der SHA-256-Vergleich bewertet ausschließlich Dateibytes; Dokumentinhalte werden nicht ausgewertet.'
        quote=previous['sha256']+' → '+current['sha256']
        old=detail.get('ai_assessment')
        result={'change_id':change_id,'relevance':'relevant','policy':DOCUMENT_HASH_POLICY,
                'method':'deterministic','updated':False,'originals_verified':True}
        if (isinstance(old,dict) and old.get('policy')==DOCUMENT_HASH_POLICY and old.get('method')=='deterministic'
                and old.get('relevance')=='relevant' and old.get('evidence_quote')==quote):return result
        history=detail.get('assessment_history',[])
        if not isinstance(history,list):return None
        stamp=now()
        if 'ai_assessment' in detail:
            detail['assessment_history']=[*history,{'assessment':old,'replaced_at':stamp,'replaced_by_policy':DOCUMENT_HASH_POLICY}]
        marker['originals_verified']=True
        detail['ai_assessment']={'relevance':'relevant','reason':reason,'evidence_quote':quote,
                                 'policy':DOCUMENT_HASH_POLICY,'method':'deterministic','reviewed_at':stamp}
        connection.execute('UPDATE changes SET evidence=? WHERE id=?',(json.dumps(detail,ensure_ascii=False),change_id))
        connection.execute('INSERT INTO events(run_id,time,message) VALUES(?,?,?)',
                           (row['run_id'],stamp,'Dateiversion geändert; beide Originaldateien per SHA-256 verifiziert. Keine Inhaltsanalyse.'))
        result['updated']=True
        return result


def _source_url(value):
    """Compare complete absolute URLs; only the fragment may be discarded."""
    if not isinstance(value,str) or not value or any(c.isspace() or ord(c)<32 for c in value):
        return None
    try:
        result=urldefrag(value)[0]
        parsed=urlsplit(result)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
            return None
        parsed.port  # Reject malformed port syntax without normalizing the URL.
        return result
    except ValueError:
        return None


def _normalized_text(value):
    # Match archive normalization. Do not reorder lines, remove numbers, or
    # collapse internal whitespace that may carry meaning in the source.
    if not isinstance(value,str) or not value.strip():
        return None
    return '\n'.join(line.rstrip() for line in value.strip().splitlines())


def _link_set(value, *, serialized=False):
    if serialized:
        try:value=json.loads(value)
        except (ValueError,TypeError):return None
    if not isinstance(value,list) or any(not isinstance(link,str) or not _source_url(link) for link in value):
        return None
    return set(value)


def _self_link_proof(row,previous,current,detail):
    """Return exact changed URLs only for a fully proven page self-link delta."""
    if row['kind']!='Links geändert' or not previous or not current or not isinstance(detail,dict):
        return None
    # A visual change, text diff, collection update or unknown evidence must
    # never be silently hidden together with an otherwise harmless self-link.
    allowed={'links_added','links_removed','text_diff','ai_assessment','assessment_history'}
    if set(detail)-allowed or detail.get('text_diff'):
        return None
    if (previous['kind']!='page' or current['kind']!='page'
            or previous['id']==current['id'] or previous['run_id']==current['run_id']
            or current['run_id']!=row['run_id']
            or previous['project_id']!=row['project_id'] or current['project_id']!=row['project_id']):
        return None
    source=_source_url(row['url'])
    if not source or any(_source_url(snapshot['url'])!=source for snapshot in (previous,current)):
        return None
    before_text,after_text=_normalized_text(previous['text']),_normalized_text(current['text'])
    if before_text is None or before_text!=after_text:
        return None
    before,after=_link_set(previous['links'],serialized=True),_link_set(current['links'],serialized=True)
    added,removed=_link_set(detail.get('links_added',[])),_link_set(detail.get('links_removed',[]))
    if any(value is None for value in (before,after,added,removed)):
        return None
    if added!=after-before or removed!=before-after or not (added or removed):
        return None
    changed=sorted(added|removed)
    if any(_source_url(link)!=source for link in changed):
        return None
    return changed


def review_self_link_change(change_id,run_id=None):
    """Audit one current or historical change without deleting raw evidence.

    None means the conservative policy does not apply. A proven result includes
    ``updated``; rechecking our own valid classification is idempotent. Neither
    the archive nor the user's acknowledgement state is modified.
    """
    if not isinstance(change_id,str) or not change_id or (run_id is not None and not isinstance(run_id,str)):
        return None
    with db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        result=connection.execute('SELECT * FROM changes WHERE id=?',(change_id,)).fetchone()
        row=dict(result) if result else None
        if not row or not row['previous_id'] or (run_id is not None and row['run_id']!=run_id):
            return None
        try:detail=json.loads(row['evidence'])
        except (ValueError,TypeError):return None
        snapshots=[]
        for sid in (row['previous_id'],row['current_id']):
            result=connection.execute('SELECT * FROM snapshots WHERE id=?',(sid,)).fetchone()
            snapshots.append(dict(result) if result else None)
        proof=_self_link_proof(row,*snapshots,detail)
        if not proof:return None
        previous=detail.get('ai_assessment')
        reason='Nur ein Verweis auf dieselbe Quellseite wurde geändert; der archivierte Seitentext und alle anderen Linkziele sind unverändert.'
        response={'change_id':change_id,'relevance':'irrelevant','reason':reason,
                  'raw_evidence_preserved':True,'policy':SELF_LINK_POLICY,'method':'deterministic','updated':False}
        if (isinstance(previous,dict) and previous.get('policy')==SELF_LINK_POLICY
                and previous.get('method')=='deterministic' and previous.get('relevance')=='irrelevant'
                and previous.get('evidence_quote') in proof):
            return response
        history=detail.get('assessment_history',[])
        if not isinstance(history,list):return None  # Preserve unknown historical data.
        stamp=now()
        if 'ai_assessment' in detail:
            history=[*history,{'assessment':previous,'replaced_at':stamp,'replaced_by_policy':SELF_LINK_POLICY}]
            detail['assessment_history']=history
        detail['ai_assessment']={'relevance':'irrelevant','reason':reason,'evidence_quote':proof[0],
                                 'reviewed_at':stamp,'policy':SELF_LINK_POLICY,'method':'deterministic'}
        connection.execute('UPDATE changes SET evidence=? WHERE id=?',
                           (json.dumps(detail,ensure_ascii=False),change_id))
        connection.execute('INSERT INTO events(run_id,time,message) VALUES(?,?,?)',
                           (row['run_id'],stamp,'Deterministische Relevanzprüfung: unveränderter Seiteninhalt; nur Selbstverweis ('+SELF_LINK_POLICY+').'))
        response['updated']=True
        return response


def review_self_link_changes(run_id):
    """Apply the same policy to a run; report only actual new classifications."""
    if not isinstance(run_id,str) or not run_id:return []
    with db() as connection:
        ids=[row['id'] for row in connection.execute('SELECT id FROM changes WHERE run_id=?',(run_id,))]
    results=[review_self_link_change(change_id,run_id) for change_id in ids]
    return [result for result in results if result and result['updated']]


def assess_change(run_id,change_id,relevance,reason,evidence_quote):
    row=one('SELECT * FROM changes WHERE id=? AND run_id=?',(change_id,run_id))
    if not row or not row['previous_id']: raise ValueError('No matching proven change in this run')
    if relevance not in ('relevant','irrelevant','uncertain'): raise ValueError('Invalid relevance')
    if not isinstance(reason,str) or len(reason.strip())<15: raise ValueError('Explain the relevance using source evidence')
    detail=json.loads(row['evidence'])
    proof=detail.get('text_diff','')+'\n'+'\n'.join(detail.get('links_added',[])+detail.get('links_removed',[]))
    if relevance=='irrelevant' and (not isinstance(evidence_quote,str) or len(evidence_quote.strip())<10 or evidence_quote not in proof):
        raise ValueError('An irrelevant classification needs an exact quote from the technical difference')
    detail['ai_assessment']={'relevance':relevance,'reason':reason[:1000],'evidence_quote':(evidence_quote or '')[:1200],'reviewed_at':now()}
    execute('UPDATE changes SET evidence=? WHERE id=?',(json.dumps(detail,ensure_ascii=False),change_id))
    event(run_id,'KI-Relevanzbewertung: '+relevance+' - '+reason[:240])
    return {'change_id':change_id,'relevance':relevance,'reason':reason,'raw_evidence_preserved':True}
