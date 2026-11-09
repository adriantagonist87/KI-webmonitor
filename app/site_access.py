"""Origin-bound website credentials. Values never enter project rows or templates."""
import json
import os
import re
import threading
from contextlib import contextmanager
from urllib.parse import urlsplit, quote, parse_qsl
import soupsieve

from fastapi import HTTPException, Request
import store

PROVIDER = 'elektro_subscription'
GENERIC_PROVIDER = 'generic_form'
ORIGIN = 'https://www.elektro.net'
GENERIC_SELECTORS = ('form_selector','username_selector','password_selector','submit_selector','success_selector','consent_selector','close_selector')
GENERIC_KEYS = {'provider','origin','enabled','login_url','submit_url',*GENERIC_SELECTORS}
GENERIC_DEFAULTS = {'form_selector':'form:has(input[type="password"])',
    'username_selector':'input[autocomplete="username"],input[type="email"],input[name="username"],input[name="email"],input[name="login"]',
    'password_selector':'input[type="password"]','submit_selector':'button[type="submit"],input[type="submit"],button:not([type])',
    'success_selector':'a[href*="logout"],a[href*="signout"],a[href*="abmelden"],[data-authenticated="true"]',
    'consent_selector':'','close_selector':''}
_UNSAFE_AUTH_PATH = re.compile(r'(?i)(?:^|[/_.-])(logout|signout|delete|remove|unsubscribe|checkout|purchase|payment|admin|settings|preferences|terminate|register|signup|reset)(?:$|[/_.-])')
_ID = re.compile(r'[A-Za-z0-9_-]{1,80}\Z')
_ABO = re.compile(r'[0-9]{9}\Z')
_POSTCODE = re.compile(r'[0-9]{5}\Z')
_LOCK = threading.RLock()


class AccessError(ValueError):
    """Every message is fixed text and contains no submitted or stored value."""


class StorageError(AccessError):
    pass


def _project(pid):
    if not isinstance(pid, str) or not _ID.fullmatch(pid):
        return None
    return store.one('SELECT id,name,url FROM projects WHERE id=?', (pid,))


def _supported(project):
    if not project:
        return False
    try:
        u = urlsplit(project['url'])
        return bool(u.scheme == 'https' and u.hostname and not u.username and not u.password and u.port != 0)
    except (ValueError, TypeError, KeyError):
        return False


def origin_for(url):
    try:
        u=urlsplit(url)
        if u.scheme!='https' or not u.hostname or u.username or u.password or u.port==0:
            raise ValueError()
        host=u.hostname.lower()
        if ':' in host:host='['+host+']'
        return 'https://'+host+(':'+str(u.port) if u.port not in (None,443) else '')
    except (ValueError,TypeError):
        raise AccessError('Webseitenzugänge erfordern eine HTTPS-Adresse ohne Zugangsdaten in der URL.') from None


def validate_public_config(project_url, config):
    """Validate portable metadata without reading any credential or network state."""
    if not isinstance(config,dict) or type(config.get('enabled')) is not bool:
        raise AccessError('Ungültige öffentliche Webseiten-Zugangskonfiguration.')
    origin=origin_for(project_url)
    if config.get('provider')==PROVIDER:
        if set(config)!={'provider','origin','enabled'} or origin!=ORIGIN or config.get('origin')!=ORIGIN:
            raise AccessError('Der Abonnementzugang muss zur freigegebenen Anbieteradresse gehören.')
        return dict(config)
    if config.get('provider')!=GENERIC_PROVIDER or set(config)!=GENERIC_KEYS or config.get('origin')!=origin:
        raise AccessError('Webseitenzugang und Projekt müssen derselben HTTPS-Adresse zugeordnet sein.')
    result=dict(config)
    for key in ('login_url','submit_url'):
        value=config.get(key)
        if not isinstance(value,str) or len(value)>2048:
            raise AccessError('Ungültige Anmeldeadresse.')
        try:
            u=urlsplit(value)
            if origin_for(value)!=origin or u.fragment or not u.path.startswith('/') or _UNSAFE_AUTH_PATH.search(u.path):
                raise ValueError()
            if u.query:
                # Some ordinary forms carry one fixed, same-origin return URL.
                # No arbitrary parameters, credentials, nested redirects or admin targets.
                pairs=parse_qsl(u.query,keep_blank_values=True,strict_parsing=True,max_num_fields=2)
                if len(pairs)!=1 or pairs[0][0]!='return_url':raise ValueError()
                target=urlsplit(pairs[0][1])
                if origin_for(pairs[0][1])!=origin or target.query or target.fragment or not target.path.startswith('/') or _UNSAFE_AUTH_PATH.search(target.path):raise ValueError()
        except (ValueError,AccessError):
            raise AccessError('Anmelde- und Formularadresse müssen HTTPS-Adressen desselben Projekts sein; nur eine feste interne return_url ist erlaubt.') from None
        result[key]=origin+quote(u.path,safe="/%:@-._~!$&'()*+,;=")+('?' + u.query if u.query else '')
    for key in GENERIC_SELECTORS:
        value=config[key]
        if not isinstance(value,str) or len(value)>500 or any(c in value for c in ('\x00','\r','\n')):
            raise AccessError('Die Formularauswahl muss ein kurzer CSS-Selektor sein.')
        value=value.strip();result[key]=value
        if '-soup-' in value.lower():raise AccessError('Bitte einen im Browser verwendbaren CSS-Selektor angeben.')
        if re.search(r'\[\s*value\b',value,re.I):
            raise AccessError('Formularselektoren dürfen keine Eingabewerte enthalten; stattdessen id oder name verwenden.')
        if value:
            try:soupsieve.compile(value)
            except Exception:raise AccessError('Ein Formularselektor ist ungültig.') from None
    if result['success_selector'].lower() in ('html','body','main','section','article','a','div','span','*'):
        raise AccessError('Als Anmeldebestätigung bitte ein eindeutiges Element wie einen Abmelden-Link wählen.')
    if result['enabled'] and any(not result[k] for k in GENERIC_SELECTORS[:5]):
        raise AccessError('Für die automatische Anmeldung sind Formular, beide Eingabefelder, Anmeldeknopf und Erfolgsmerkmal erforderlich.')
    if result['username_selector'] and result['username_selector']==result['password_selector']:
        raise AccessError('Benutzername und Passwort benötigen unterschiedliche Eingabefelder.')
    return result


def public_config(pid):
    """Return only explicitly supported public metadata for the current origin."""
    if not _supported(_project(pid)):
        return None
    row = store.one('SELECT value FROM state WHERE key=?', ('site_auth:' + pid,))
    if not row:
        return None
    try:
        config = json.loads(row['value'])
    except (ValueError, TypeError):
        return None
    try:return validate_public_config(_project(pid)['url'],config)
    except AccessError:return None


def vault_path():
    return store.SECRETS.parent / 'site-logins.json'


def _read_vault():
    path = vault_path()
    if path.is_symlink() or path.with_suffix('.tmp').is_symlink():
        raise StorageError('Der geschützte Zugangsspeicher ist nicht verfügbar.')
    try:
        if os.name == 'posix' and path.stat().st_mode & 0o077:
            raise StorageError('Die Dateirechte des Zugangsspeichers sind nicht geschützt.')
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {'version': 1, 'logins': {}}
    except (OSError, UnicodeError, ValueError):
        raise StorageError('Der geschützte Zugangsspeicher ist nicht lesbar.') from None
    if (not isinstance(value, dict) or set(value) != {'version', 'logins'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['logins'], dict)):
        raise StorageError('Der geschützte Zugangsspeicher ist ungültig.')
    return value


def _valid_record(record):
    legacy=(isinstance(record, dict)
            and set(record) == {'provider', 'origin', 'abonummer', 'postcode'}
            and record['provider'] == PROVIDER and record['origin'] == ORIGIN
            and isinstance(record['abonummer'], str) and bool(_ABO.fullmatch(record['abonummer']))
            and isinstance(record['postcode'], str) and bool(_POSTCODE.fullmatch(record['postcode'])))
    if legacy:return True
    if not isinstance(record,dict) or set(record)!={'provider','origin','username','password'} or record.get('provider')!=GENERIC_PROVIDER:
        return False
    try:valid_origin=origin_for(record['origin'])==record['origin']
    except (AccessError,KeyError):return False
    return (valid_origin and _generic_value(record['username'],512) and _generic_value(record['password'],4096))


def _generic_value(value, limit):
    return isinstance(value,str) and 0<len(value)<=limit and not any(c in value for c in ('\x00','\r','\n'))


def _matches_profile(record, profile):
    return (_valid_record(record) and record['provider'] == profile['provider']
            and record['origin'] == profile['origin'])


def validate_transfer_logins(logins, profiles):
    if not isinstance(logins, dict) or any(pid not in profiles or not _matches_profile(record, profiles[pid])
                                           for pid, record in logins.items()):
        raise AccessError('Die gespeicherten Zugangsdaten passen nicht zur Website-Zuordnung im Paket.')


@contextmanager
def transfer_vault():
    """Hold the native vault lock through an export or the importing DB commit."""
    with _LOCK:
        path = vault_path()
        previous = _read_vault()
        existed = path.exists()
        written = False

        def merge(logins):
            nonlocal written
            store.save_json(path, {**previous, 'logins': {**previous['logins'], **logins}})
            written = True

        try:
            yield previous['logins'], merge
        except BaseException:
            if written:
                if existed:
                    store.save_json(path, previous)
                else:
                    path.unlink(missing_ok=True)
            raise


def get_secret(pid):
    """Internal only. Caller must separately check public_config.enabled to log in.

    Disabled but still stored values remain available to the privacy scrubber.
    Changing a project's origin cannot carry its previous credentials to a new site.
    """
    config=public_config(pid)
    if config is None:
        return None
    with _LOCK:
        record = _read_vault()['logins'].get(pid)
        if not _matches_profile(record, config):
            return None
        return dict(record)


def status(pid):
    supported = _supported(_project(pid))
    config = public_config(pid) if supported else None
    try:
        configured = bool(get_secret(pid)) if config else False
    except StorageError:
        configured = False
    return {'supported': supported, 'configured': configured,
            'enabled': bool(config and config['enabled'])}


def save(pid, enabled, abonummer='', postcode='', clear=False):
    """Save values separately from metadata; preserve blank fields and other entries."""
    project = _project(pid)
    if not project:
        raise AccessError('Projekt nicht gefunden.')
    if not _supported(project) or origin_for(project['url'])!=ORIGIN:
        raise AccessError('Dieser Webseitenzugang ist nur für https://www.elektro.net verfügbar.')
    if type(enabled) is not bool or type(clear) is not bool:
        raise AccessError('Ungültige Zugangseinstellung.')
    if not isinstance(abonummer, str) or not isinstance(postcode, str):
        raise AccessError('Ungültige Zugangsdaten.')
    abonummer, postcode = abonummer.strip(), postcode.strip()
    if not clear:
        if abonummer and not _ABO.fullmatch(abonummer):
            raise AccessError('Die Abonummer muss genau neun Ziffern enthalten.')
        if postcode and not _POSTCODE.fullmatch(postcode):
            raise AccessError('Die PLZ muss genau fünf Ziffern enthalten.')
    with _LOCK:
        vault = _read_vault()
        old_record = vault['logins'].get(pid)
        old_record = old_record if _valid_record(old_record) and old_record['provider']==PROVIDER else None
        if clear:
            vault['logins'].pop(pid, None)
            enabled = False
        else:
            number = abonummer or (old_record['abonummer'] if old_record else '')
            code = postcode or (old_record['postcode'] if old_record else '')
            if bool(number) != bool(code) or (enabled and not (number and code)):
                raise AccessError('Für die automatische Anmeldung sind Abonummer und PLZ erforderlich.')
            if number and code:
                vault['logins'][pid] = {'provider': PROVIDER, 'origin': ORIGIN,
                                        'abonummer': number, 'postcode': code}
        path = vault_path()
        old_bytes = path.read_bytes() if path.exists() else None
        changed_vault = bool(clear or abonummer or postcode)
        try:
            if changed_vault:
                store.save_json(path, vault)
            config = {'provider': PROVIDER, 'origin': ORIGIN, 'enabled': enabled}
            store.execute('INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                          ('site_auth:' + pid, json.dumps(config)))
        except Exception:
            if changed_vault:
                if old_bytes is None:
                    path.unlink(missing_ok=True)
                else:
                    store.save_json(path, json.loads(old_bytes))
            raise
    return status(pid)


def save_generic(pid, enabled, config, username='', password='', clear=False):
    project=_project(pid)
    if not project:raise AccessError('Projekt nicht gefunden.')
    if type(enabled) is not bool or type(clear) is not bool:
        raise AccessError('Ungültige Zugangseinstellung.')
    if not isinstance(username,str) or not isinstance(password,str):raise AccessError('Ungültige Zugangsdaten.')
    if username and not _generic_value(username,512) or password and not _generic_value(password,4096):
        raise AccessError('Die Zugangsdaten sind zu lang oder enthalten ungültige Steuerzeichen.')
    config=validate_public_config(project['url'],{**config,'provider':GENERIC_PROVIDER,'origin':origin_for(project['url']),'enabled':enabled and not clear})
    with _LOCK:
        vault=_read_vault();old=vault['logins'].get(pid)
        old=old if _valid_record(old) and old['provider']==GENERIC_PROVIDER and old['origin']==config['origin'] else None
        removed_old=False
        if clear:
            vault['logins'].pop(pid,None)
        else:
            user=username or (old['username'] if old else '')
            code=password or (old['password'] if old else '')
            if bool(user)!=bool(code) or enabled and not (user and code):
                raise AccessError('Für die automatische Anmeldung sind Benutzername und Passwort erforderlich.')
            if user and code:vault['logins'][pid]={'provider':GENERIC_PROVIDER,'origin':config['origin'],'username':user,'password':code}
            elif pid in vault['logins'] and not old:vault['logins'].pop(pid);removed_old=True
        path=vault_path();before=path.read_bytes() if path.exists() else None
        changed=bool(clear or username or password or removed_old)
        try:
            if changed:store.save_json(path,vault)
            store.execute('INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('site_auth:'+pid,json.dumps(config)))
        except Exception:
            if changed:
                if before is None:path.unlink(missing_ok=True)
                else:store.save_json(path,json.loads(before))
            raise
    return status(pid)


def remove_secret(pid):
    """Remove only this project entry. Public state/database lifecycle is the caller's responsibility."""
    if not isinstance(pid,str) or not _ID.fullmatch(pid):raise AccessError('Ungültige Projektkennung.')
    with _LOCK:
        vault=_read_vault()
        if pid not in vault['logins']:return False
        del vault['logins'][pid];store.save_json(vault_path(),vault)
        return True


@contextmanager
def secret_removal(pid):
    """Roll back the private removal if the caller's database deletion fails."""
    with _LOCK:
        _read_vault()
        path=vault_path();before=path.read_bytes() if path.exists() else None
        changed=remove_secret(pid)
        try:yield
        except BaseException:
            if changed:
                if before is None:path.unlink(missing_ok=True)
                else:
                    tmp=path.with_suffix('.tmp')
                    with tmp.open('wb') as f:
                        os.chmod(tmp,0o600);f.write(before)
                    tmp.replace(path)
            raise


def register(app, render, redirect):
    """Register with the existing protected app; its middleware supplies CSRF checks."""
    def selected(pid):
        project = _project(pid)
        if not project:
            raise HTTPException(404, 'Projekt nicht gefunden.')
        return project

    def context(pid,project):
        config=public_config(pid)
        origin=origin_for(project['url']) if _supported(project) else ''
        defaults={'provider':PROVIDER if origin==ORIGIN else GENERIC_PROVIDER,'origin':origin,'enabled':False,'login_url':origin+'/','submit_url':''}
        defaults.update(GENERIC_DEFAULTS)
        fields={**defaults,**(config or {})}
        if fields['provider']==GENERIC_PROVIDER and fields['submit_url']==fields['login_url']:fields['submit_url']=''
        return {'project':project,'access':status(pid),'access_config':fields}

    @app.get('/projects/{pid}/access')
    async def access_page(pid: str, request: Request):
        project = selected(pid)
        saved = {'saved': 'Webseitenzugang gespeichert.',
                 'cleared': 'Zugangsdaten entfernt. Automatische Anmeldung deaktiviert.'}
        return render(request, 'site_access', **context(pid,project),
                      access_saved=saved.get(request.query_params.get('access_saved')))

    @app.post('/projects/{pid}/access')
    async def access_save(pid: str, request: Request):
        project = selected(pid)
        f = await request.form()
        try:
            provider=str(f.get('provider',PROVIDER if origin_for(project['url'])==ORIGIN else GENERIC_PROVIDER))
            if provider==PROVIDER:
                save(pid, f.get('enabled') == 'on', str(f.get('abonummer', '')),
                     str(f.get('postcode', '')), f.get('clear') == 'on')
            elif provider==GENERIC_PROVIDER:
                origin=origin_for(project['url']);login_url=str(f.get('login_url','')).strip() or origin+'/'
                config={'provider':GENERIC_PROVIDER,'origin':origin,'enabled':f.get('enabled')=='on',
                        'login_url':login_url,'submit_url':str(f.get('submit_url','')).strip() or login_url,
                        **{key:str(f.get(key,'')) for key in GENERIC_SELECTORS}}
                save_generic(pid,f.get('enabled')=='on',config,str(f.get('username','')),str(f.get('password','')),f.get('clear')=='on')
            else:raise AccessError('Unbekanntes Anmeldeverfahren.')
        except AccessError as error:
            response = render(request, 'site_access', **context(pid,project),
                              access_error=str(error))
            response.status_code = 503 if isinstance(error, StorageError) else 400
            return response
        except OSError:
            response = render(request, 'site_access', **context(pid,project),
                              access_error='Der Webseitenzugang konnte nicht gespeichert werden.')
            response.status_code = 503
            return response
        # Fresh GET leaves no credential-bearing POST as the current history entry.
        notice = 'cleared' if f.get('clear') == 'on' else 'saved'
        return redirect('/projects/' + pid + '/access?access_saved=' + notice)
