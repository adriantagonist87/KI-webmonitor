"""Persistent review state for technically proven source changes."""
import json
import re
from collections import Counter
from store import rows, db

CONTENT_KINDS = {'Inhalt geändert', 'Links geändert', 'Neues Dokument', 'Neue Seite', 'Datei geändert', 'Neue stabile Version', 'Neue Normausgabe', 'Neue Vornorm',
                 'Scantext geändert (OCR)', 'Textabweichung (OCR-Vorversion)',
                 'Dateifassung geändert (Textvergleich eingeschränkt)'}


def priority(kind, evidence=None):
    kinds = set(kind.split('; '))
    assessment = evidence.get('ai_assessment') if isinstance(evidence, dict) else None
    if isinstance(assessment, dict) and assessment.get('relevance') in ('irrelevant', 'uncertain'):
        return 'notice'
    if CONTENT_KINDS.intersection(kinds):
        return 'attention'
    # This existing archive kind proves changed original bytes with unchanged
    # extracted text. Relevance alone must never promote visual/unknown hints.
    if ('Datei technisch geändert' in kinds and isinstance(assessment, dict)
            and assessment.get('relevance') == 'relevant'):
        return 'attention'
    return 'notice'


def number_changed(evidence):
    diff = evidence.get('text_diff', '')
    before, after = [], []
    for line in diff.splitlines():
        if line.startswith('---') or line.startswith('+++'):
            continue
        if line.startswith('-'):
            before.extend(re.findall(r'\d+(?:[.,/-]\d+)*', line[1:]))
        elif line.startswith('+'):
            after.extend(re.findall(r'\d+(?:[.,/-]\d+)*', line[1:]))
    return Counter(before) != Counter(after)


def pending(project_id):
    changes = rows('''SELECT c.*,r.finished FROM changes c
        JOIN runs r ON r.id=c.run_id
        LEFT JOIN change_reviews v ON v.change_id=c.id
        WHERE c.project_id=? AND r.status IN ('success','partial')
        AND r.finished IS NOT NULL AND v.change_id IS NULL
        AND COALESCE(json_extract(c.evidence,'$.ai_assessment.relevance'),'uncertain')<>'irrelevant'
        AND NOT (c.previous_id IS NULL AND c.kind IN ('Neue Seite','Neues Dokument'))
        ORDER BY r.finished DESC,c.detected DESC''', (project_id,))
    for c in changes:
        c['evidence'] = json.loads(c['evidence'])
        c['priority'] = priority(c['kind'], c['evidence'])
        c['number_changed'] = number_changed(c['evidence'])
    return changes


def summary(project_id):
    changes = pending(project_id)
    important = [c for c in changes if c['priority'] == 'attention']
    return {'pending_changes': changes, 'attention_count': len(important),
            'notice_count': len(changes)-len(important),
            'change_run_id': (important or changes)[0]['run_id'] if changes else None,
            'review_through': max((c['finished'] for c in changes), default='')}


def latest_relevant_change(project_id):
    with db() as c:
        changes = c.execute('''SELECT c.kind,c.evidence,c.detected FROM changes c
            JOIN runs r ON r.id=c.run_id
            WHERE c.project_id=? AND r.status IN ('success','partial') AND r.finished IS NOT NULL
            AND COALESCE(json_extract(c.evidence,'$.ai_assessment.relevance'),'uncertain')<>'irrelevant'
            AND NOT (c.previous_id IS NULL AND c.kind IN ('Neue Seite','Neues Dokument'))
            ORDER BY julianday(c.detected) DESC,c.rowid DESC''', (project_id,))
        for change in changes:
            if priority(change['kind'], json.loads(change['evidence'])) == 'attention':
                return change['detected']


def acknowledge(project_id, through, reviewed_at):
    # The cutoff comes from the displayed page. A run finishing later stays pending.
    with db() as c:
        c.execute('''INSERT OR IGNORE INTO change_reviews(change_id,reviewed_at)
            SELECT ch.id,? FROM changes ch JOIN runs r ON r.id=ch.run_id
            WHERE ch.project_id=? AND r.status IN ('success','partial')
            AND r.finished IS NOT NULL AND r.finished<=?''',
                  (reviewed_at, project_id, through))
