# Anmeldung und Administrator Â· 1.3.6

Die App besitzt eine eigene, fÃ¼r Smartphones geeignete Anmeldeseite unter
`/login`. Es gibt keinen HTTP-Basic-Dialog mehr. Nach dem Anmelden steht oben
rechts **Abmelden** zur VerfÃ¼gung. Downloads und Brave verwenden dieselbe
Sitzung. HTTP im LAN bleibt bestehen.

## Portainer und Compose

In den Environment-Variablen des **bestehenden** Stacks setzen:

```dotenv
KIWM_ADMIN_USERNAME=admin
KIWM_ADMIN_PASSWORD=
KIWM_SESSION_HOURS=168
```

Ein eigenes Passwort mit mindestens 12 und hÃ¶chstens 512 Zeichen eintragen;
der leere Beispielwert ist kein gÃ¼ltiges Passwort. Benutzername und Passwort
werden nur dem App-Container Ã¼bergeben. `KIWM_SESSION_HOURS` ist optional:
1 bis 720 Stunden, standardmÃ¤ÃŸig sieben Tage. Danach **Stack aktualisieren**.
Bei Compose dieselben Werte in die private `.env` eintragen.

In dieser Installation bleibt der bisherige Benutzer `webmonitor` mit seinem
bisherigen Passwort erhalten; beide Werte sind jetzt im Portainer-Stack
konfiguriert. Das Passwort wird niemals in eine verÃ¶ffentlichte Stack-Datei
eingetragen. Docker-/Portainer-Administratoren kÃ¶nnen ENV-Werte sehen.
Portainer-ENV oder lokale `.env` bewusst als Verwaltungsquelle wÃ¤hlen, damit
ein spÃ¤teres administratives Update keine veralteten Werte Ã¼bernimmt.

Bei leerem oder unvollstÃ¤ndigem Administratorzugang bleibt der LAN-Zugang
geschlossen. AusschlieÃŸlich beim normalen Stack ohne LAN-Erweiterung und
ohne beide Admin-Werte bleibt der vorhandene SSH-Tunnelmodus mÃ¶glich.

## Sitzungen

Der Browser erhÃ¤lt einen zufÃ¤lligen, nicht lesbaren SitzungsschlÃ¼ssel als
HttpOnly-Cookie. In `/data/auth/sessions.db` liegt ausschlieÃŸlich dessen Hash;
Passwort und rohe SitzungsschlÃ¼ssel werden dort nicht gespeichert. Ein
privater InstallationsschlÃ¼ssel liegt daneben in `session.key`. Beide gehÃ¶ren
zu einer vollstÃ¤ndigen Datensicherung, nicht zu Projekt-JSON-Exporten.

Sitzungen Ã¼berstehen Container-Neustarts. Abmelden widerruft die Sitzung auf
dem Server. GeÃ¤nderte Admin-Zugangsdaten widerrufen nach dem Stack-Update
alle bisherigen Sitzungen. Neue Anmeldungen ersetzen vorhandene Sitzungstoken.
Falsche Zugangsdaten ergeben eine verstÃ¤ndliche Fehlermeldung im Formular;
nach fÃ¼nf Fehlversuchen pro Minute wird der Client vorÃ¼bergehend begrenzt.

Die Anmeldung verwendet CSRF-Schutz und akzeptiert nur lokale RÃ¼cksprungziele.
Die Browsersteuerung ist zusÃ¤tzlich Ã¼ber eine nginx-SitzungsprÃ¼fung geschÃ¼tzt.
Benutzername, Passwort und Sitzungstoken werden nicht an KI-Modelle Ã¼bergeben,
nicht in Projektprompts geschrieben und nicht in Anwendungs-Logs ausgegeben.

Referenzen: [nginx SitzungsprÃ¼fung](https://nginx.org/en/docs/http/ngx_http_auth_request_module.html),
[OWASP Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html).

## Abnahme dieser Installation

93 gezielte Anwendungstests in fünf Suiten, 65 administrative Tests und der
Compose-/Portainer-Abgleich bestanden. Im echten Windows-LAN-Test wurden
Login, falsches Passwort, Login-CSRF, Logout, PDF-Download mit SHA-256-Abgleich
und der authentifizierte noVNC-WebSocket geprüft. Nach einem tatsächlichen
App-Neustart blieb die Sitzung gültig; Abmelden widerrief sie anschließend.
Im schmalen Browserfenster wurden Formular und Abmelden zusätzlich bedient.
Alle 14 Projektdefinitionen und bisherigen privaten Konfigurationen sind erhalten.
