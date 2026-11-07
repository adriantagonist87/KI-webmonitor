"""Private deterministic browser-login fixture; all identifiers are fake canaries.

Mount with ``app.include_router(router)`` in the existing fixture service only.
There are no external requests, real subscriber credentials, or model calls.
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse


router = APIRouter(prefix='/auth-test')
COUNTERS = {'attempts': 0, 'successes': 0, 'failures': 0, 'csrf_failures': 0}
COOKIE = 'kiwm_auth_test'
COOKIE_PATH = '/auth-test/'
CSRF = 'fixture-csrf'


def signed_in(request: Request):
    return request.cookies.get(COOKIE) == 'signed-in'


BASE_STYLE = '''
body{font:18px/1.5 sans-serif;padding:32px;max-width:980px;margin:auto;color:#202832}
input,button{font:inherit;padding:9px 12px;margin:8px 0}label{display:block}
input{display:block;min-width:280px}button{cursor:pointer}a{color:#1467cc}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;
  align-items:center;justify-content:center}.overlay-panel{background:white;
  padding:28px;max-width:440px;border:2px solid #8ba2ba;border-radius:12px}
.cookie-banner{z-index:2000}.advertisement{z-index:1500}
.advertisement button{float:right}.account{padding:12px;background:#edf2f7}
.tabs{display:flex;gap:20px;margin:24px 0}.tab-link[aria-selected=true]{font-weight:bold}
.error,.paywall{padding:14px;border:1px solid #a22;background:#fff4f4}
[hidden]{display:none!important}
'''


# Counters contain no input values and survive the successful-login redirect.
COUNTER_SCRIPT = '''
window.kiwmAuthTestCounters=JSON.parse(sessionStorage.getItem('kiwm_auth_counters')||
  '{"adClosures":0,"adShows":0,"cookieAccepts":0}');
function syncTestCounters(){
  sessionStorage.setItem('kiwm_auth_counters',JSON.stringify(window.kiwmAuthTestCounters));
  document.body.dataset.adClosures=String(window.kiwmAuthTestCounters.adClosures);
  document.body.dataset.adShows=String(window.kiwmAuthTestCounters.adShows);
  document.body.dataset.cookieAccepts=String(window.kiwmAuthTestCounters.cookieAccepts);
}
syncTestCounters();
'''


def page(body: str, *, script='', status=200):
    return HTMLResponse(
        '<!doctype html><html lang="de"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Autologin-Testportal</title><style>' + BASE_STYLE + '</style></head>'
        '<body>' + body + '<script>' + COUNTER_SCRIPT + script + '</script></body></html>',
        status_code=status,
        headers={'Cache-Control': 'no-store'},
    )


def member_nav():
    return '<nav><a href="/auth-test/logout">Abmelden</a></nav>'


def login_page(request: Request, error='', status=200):
    error_html = '<p class="error" role="alert">' + error + '</p>' if error else ''
    consent = ''
    if request.cookies.get('kiwm_auth_test_consent') != 'accepted':
        consent = '''<div class="cookie-banner overlay" role="dialog" aria-label="Cookie-Einstellungen">
          <div class="overlay-panel"><h2>Cookie-Einstellungen</h2>
          <p>Für diesen privaten Test die Cookieauswahl bestätigen.</p>
          <button type="button" onclick="acceptTestConsent()">Akzeptieren</button></div></div>'''
    body = '''<h1>ABO-LOGIN</h1>''' + error_html + '''
      <form method="post" action="/auth-test/login" autocomplete="off">
        <input type="hidden" name="login[csrfToken]" value="fixture-csrf">
        <label for="subscriber">Abonummer</label>
        <input id="subscriber" name="login[userIdentifier]" type="text" inputmode="numeric"
          autocomplete="username" aria-label="Abonummer" required>
        <label for="postal-code">PLZ</label>
        <input id="postal-code" name="login[password]" type="text" inputmode="numeric"
          autocomplete="current-password" aria-label="PLZ" required>
        <button type="submit">Einloggen</button>
      </form>''' + consent
    script = '''
function acceptTestConsent(){
  document.cookie='kiwm_auth_test_consent=accepted; Path=/auth-test/; Max-Age=86400; SameSite=Lax';
  window.kiwmAuthTestCounters.cookieAccepts++;syncTestCounters();
  document.querySelector('.cookie-banner')?.remove();
}
function showTestAdvertisement(){
  if(document.querySelector('.advertisement'))return;
  const wrapper=document.createElement('div');
  wrapper.className='advertisement overlay';wrapper.setAttribute('role','dialog');
  wrapper.setAttribute('aria-label','Werbung');
  wrapper.innerHTML='<div class="overlay-panel"><button type="button" aria-label="Werbung schließen">X</button><h2>Werbeeinblendung</h2><p>Dieses Fenster versperrt absichtlich das Loginformular.</p></div>';
  wrapper.querySelector('button').addEventListener('click',()=>{
    window.kiwmAuthTestCounters.adClosures++;syncTestCounters();wrapper.remove();
  });
  document.body.appendChild(wrapper);
  window.kiwmAuthTestCounters.adShows++;syncTestCounters();
}
showTestAdvertisement();
document.getElementById('subscriber').addEventListener('input',()=>{
  setTimeout(showTestAdvertisement,0);
},{once:true});
'''
    return page(body, script=script, status=status)


@router.get('/login')
async def login(request: Request):
    if signed_in(request):
        return page('<h1>Anmeldung aktiv</h1>' + member_nav() +
                    '<p>Die gespeicherte Sitzung wurde wiederverwendet.</p>'
                    '<a href="/auth-test/target">Zur Fachübersicht</a>')
    return login_page(request)


@router.post('/login')
async def submit_login(request: Request):
    values = await request.form()
    COUNTERS['attempts'] += 1
    if values.get('login[csrfToken]') != CSRF:
        COUNTERS['csrf_failures'] += 1
        COUNTERS['failures'] += 1
        return login_page(request, 'Ungültige Formularbestätigung.', status=403)
    if values.get('login[userIdentifier]') != '987654321' or values.get('login[password]') != '54321':
        COUNTERS['failures'] += 1
        return login_page(request, 'Anmeldung fehlgeschlagen. Zugangsdaten prüfen.')
    COUNTERS['successes'] += 1
    response = RedirectResponse('/auth-test/home', status_code=303)
    response.set_cookie(COOKIE, 'signed-in', path=COOKIE_PATH, max_age=86400 * 30,
                        httponly=True, samesite='lax')
    response.headers['Cache-Control'] = 'no-store'
    return response


@router.get('/home')
async def home(request: Request):
    if not signed_in(request):
        return RedirectResponse('/auth-test/login', status_code=303)
    return page('<h1>Hauptseite nach der Anmeldung</h1>' + member_nav() +
                '<p>Der Login hat absichtlich nicht zur ursprünglich gewünschten Seite zurückgeführt.</p>'
                '<a href="/auth-test/target">Praxisprobleme öffnen</a>')


@router.get('/target')
async def target(request: Request):
    if not signed_in(request):
        return page('<h1>Praxisprobleme</h1><section class="paywall">'
                    '<h2>Anmeldung erforderlich</h2><a href="/auth-test/login">ABO-LOGIN</a>'
                    '<p>Diese Übersicht ist noch gesperrt.</p></section>')
    body = member_nav() + '''
      <header class="account" aria-label="Benutzerkonto">Abonummer 987654321 · PLZ 54321</header>
      <main><h1>Praxisprobleme</h1>
      <nav class="tabs" role="tablist">
        <a class="tab-link" href="#answer" role="tab" aria-selected="false">Fragen mit Antwort</a>
        <a class="tab-link" href="#open" role="tab" aria-selected="true">Offene Fragen</a>
      </nav>
      <section id="answer" role="tabpanel" hidden>
        <h2>Fragen mit Antwort</h2>
        <article><h3><a href="/auth-test/article/one">Leitungsquerschnitt im Bestand</a></h3>
          <p>Ein überprüfter erster Testeintrag.</p></article>
        <article><h3><a href="/auth-test/article/two">Fachlicher Wert 54321</a></h3>
          <p>Der fachliche Zahlenwert 54321 muss erhalten bleiben.</p></article>
        <article><h3><a href="/auth-test/article/three">RCD-Prüfung und Fristen</a></h3>
          <p>Ein überprüfter dritter Testeintrag.</p></article>
      </section>
      <section id="open" role="tabpanel"><h2>Offene Fragen</h2>
        <h3>Unpassender Titel aus dem falschen Reiter</h3>
        <p>Dieser Inhalt darf nicht als beantwortete Frage übernommen werden.</p>
      </section></main>'''
    script = '''
document.querySelectorAll('a.tab-link').forEach(link=>link.addEventListener('click',event=>{
  event.preventDefault();const selected=link.getAttribute('href').slice(1);
  document.querySelectorAll('main section[role=tabpanel]').forEach(panel=>{panel.hidden=panel.id!==selected;});
  document.querySelectorAll('a.tab-link').forEach(tab=>{tab.setAttribute('aria-selected',String(tab===link));});
  history.replaceState(null,'',location.pathname+'#'+selected);
}));
'''
    return page(body, script=script)


@router.get('/protected')
async def protected(request: Request):
    if signed_in(request):
        return page('<h1>Geschützter Fachinhalt</h1>' + member_nav() +
                    '<p>Die verifizierte Testantwort ist vollständig zugänglich.</p>')
    return page('<h1>Geschützter Fachinhalt</h1><section class="paywall">'
                '<a href="/auth-test/login">Jetzt einloggen</a>'
                '<p>Lorem ipsum dolor sit amet. Antwort ohne Anmeldung gesperrt.</p></section>')


@router.get('/article/{slug}')
async def article(slug: str, request: Request):
    if not signed_in(request):
        return RedirectResponse('/auth-test/login', status_code=303)
    titles = {'one': 'Leitungsquerschnitt im Bestand', 'two': 'Fachlicher Wert 54321',
              'three': 'RCD-Prüfung und Fristen'}
    title = titles.get(slug)
    if title is None:
        return page('<h1>Nicht gefunden</h1>', status=404)
    return page('<h1>' + title + '</h1>' + member_nav() +
                '<p>Geschützter Fachinhalt der privaten Authentifizierungsprüfung.</p>'
                '<a href="/auth-test/target">Zur Übersicht</a>')


@router.get('/status')
async def status():
    return JSONResponse(dict(COUNTERS), headers={'Cache-Control': 'no-store'})


@router.get('/expire')
@router.get('/logout')
async def expire():
    response = RedirectResponse('/auth-test/target', status_code=303)
    response.delete_cookie(COOKIE, path=COOKIE_PATH)
    response.headers['Cache-Control'] = 'no-store'
    return response


GENERIC_COOKIE = 'kiwm_generic_auth_test'
GENERIC_USER = 'test-reader@example.test'
GENERIC_PASSWORD = 'fake-only-C0de!'


def generic_signed_in(request):
    return request.cookies.get(GENERIC_COOKIE)=='signed-in'


def generic_login_page(request, error=''):
    consent='' if request.cookies.get('kiwm_generic_consent')=='accepted' else '''<div class="cookie-banner" role="dialog"><button id="generic-cookie-accept" type="button" onclick="document.cookie='kiwm_generic_consent=accepted; Path=/auth-test/generic/; SameSite=Lax';this.parentElement.remove()">Akzeptieren</button></div>'''
    body='''<h1>Privater Formular-Test</h1><p role="alert">'''+error+'''</p>
    <form id="generic-login" action="/auth-test/generic/login" method="post" autocomplete="off">
    <input type="hidden" name="csrf" value="generic-fixture-csrf">
    <label>E-Mail<input id="generic-user" name="email" type="email" autocomplete="username"></label>
    <label>Passwort<input id="generic-pass" name="pass" type="password" autocomplete="current-password"></label>
    <button id="generic-submit" type="submit">Anmelden</button></form>'''+consent
    script='''function showGenericAd(){if(document.querySelector('.advertisement'))return;const ad=document.createElement('div');ad.className='advertisement';ad.setAttribute('role','dialog');ad.innerHTML='<button id="generic-ad-close" type="button">X</button><p>Werbe-Test</p>';ad.querySelector('button').onclick=()=>ad.remove();document.body.appendChild(ad)};showGenericAd();document.querySelector('#generic-user').addEventListener('input',showGenericAd,{once:true});'''
    return page(body,script=script)


@router.get('/generic/login')
async def generic_login(request: Request):
    if generic_signed_in(request):
        return page('<h1>Angemeldet</h1><a id="generic-logout" href="/auth-test/generic/logout">Abmelden</a>')
    return generic_login_page(request)


@router.post('/generic/login')
async def generic_submit(request: Request):
    values=await request.form()
    if values.get('csrf')!='generic-fixture-csrf' or values.get('email')!=GENERIC_USER or values.get('pass')!=GENERIC_PASSWORD:
        return generic_login_page(request,'Zugang nicht bestätigt.')
    response=RedirectResponse('/auth-test/generic/home',status_code=303)
    response.set_cookie(GENERIC_COOKIE,'signed-in',path='/auth-test/generic/',httponly=True,samesite='lax',max_age=86400)
    return response


@router.get('/generic/home')
@router.get('/generic/target')
async def generic_target(request: Request):
    if not generic_signed_in(request):return RedirectResponse('/auth-test/generic/login',status_code=303)
    return page('<header class="account">'+GENERIC_USER+'</header><main><h1>Neue Richtlinie</h1><p>Frist 30 Tage.</p><a id="generic-logout" href="/auth-test/generic/logout">Abmelden</a></main>')


@router.get('/generic/logout')
async def generic_logout(request: Request):
    response=RedirectResponse('/auth-test/generic/login',status_code=303)
    response.delete_cookie(GENERIC_COOKIE,path='/auth-test/generic/')
    return response
