# Portainer und Release-Images

`portainer-stack.yaml` ist die normale Installation mit sechs Diensten. Sie
stimmt bytegleich mit `compose.yaml` überein. Testportal, Test-SMTP und
administrative Hilfscontainer gehören nicht zum Standardstack.

## Starten

Die fünf Images `ki-webmonitor-app`, `ki-webmonitor-browser`, `ki-webmonitor-proxy`,
`ki-webmonitor-ingress` und `ki-webmonitor-page-reader` mit dem Tag `1.5.12` auf den
Docker-Host laden. App und Gateway verwenden dasselbe App-Image.

```sh
gzip -dc KI-Webmonitor-1.5.12-Images.tar.gz | docker image load
```

In Portainer einen neuen **Standalone-Stack** anlegen und die Stack-Datei in
den Editor einfügen, hochladen oder aus dem eigenen Git-Repository beziehen.
Die persönlichen Environment-Werte aus `.env.example` sind optional. Mit
leeren KI-Werten erfolgt die Konfiguration vollständig in der Weboberfläche.
Daten-Volumes werden automatisch mit passenden Dateirechten angelegt. Portainer
zeigt sie unter **Volumes** an. Bootstrap und lokale Builds sind nicht erforderlich.

Die Ports bleiben auf dem Docker-Host lokal gebunden. Zugriff per SSH-Tunnel
wie in der README. Bestehende Verzeichnisse lassen sich über die vier
`KIWM_DATA`, `KIWM_CONFIG`, `KIWM_SECRETS`, `KIWM_BROWSER_DATA`-Werte aus
`advanced.env.example` ausdrücklich weiterverwenden. Bereits initialisierte
Bind-Verzeichnisse müssen vorher existieren und UID/GID 1000 gehören.
Ein Wechsel der Speicherquelle ist eine Datenmigration, kein gewöhnliches Update.

## KI-Environment und Weboberfläche

Nichtleere Werte `KIWM_OMLX_URL`, `KIWM_OMLX_API_KEY`, `KIWM_MODEL` überschreiben
die Webkonfiguration. Getrennte Rollen sind optional über `KIWM_AGENT_MODEL`,
`KIWM_ANALYSIS_MODEL`, `KIWM_VISION_MODEL` möglich. Reihenfolge: spezifische
Rollenvariable, gemeinsames Modell, gespeicherter Webwert. Zum Zurückwechseln
die betreffenden ENV-Werte leeren und den Stack aktualisieren.

Diese Variablen erhalten ausschließlich App und Gateway. Tokens werden nicht
in die UI-Konfigurationsdatei oder App-Exporte kopiert, sind aber für Docker- und
Portainer-Administratoren sichtbar. Private Portainer-Variablen separat sichern.

## Update

Vorher eine vollständige Sicherung erstellen und laufende Prüfungen abschließen.
Die neuen fünf Images laden, `VERSION` ändern und die Stack-Datei aktualisieren.
Stackname und Speichervariablen erhalten. Keine zweite Installation auf dasselbe
Browserprofil oder dieselbe SQLite-Datenbank ansetzen. Portainer kann Images
aus einer Registry laden, sobald diese tatsächlich veröffentlicht sind.

Die eigene ältere Sieben-Dienste-Installation benötigt einmalig die dokumentierte
Migration in `OPERATIONS.md`; deren Datenverzeichnisse bleiben dabei erhalten.

## GitHub und Registry

Der enthaltene Workflow baut die fünf Images für Linux amd64. Vor dem Build
prüft er öffentliche Quelldateien und die Compose-Konfiguration. Ein manuell
gestarteter Workflow baut standardmäßig ohne Veröffentlichung. Ein `v*`-Tag
oder ausdrücklich gesetztes Publish veröffentlicht in die GHCR des eigenen
Repositories. `v1.3.6` wird zum Image-Tag `1.3.6` normalisiert.

Erst nach Anlegen des eigenen Repositories und Freigabe der Packages kann man
beispielsweise setzen:

```dotenv
IMAGE_PREFIX=ghcr.io/OWNER/REPOSITORY
VERSION=1.5.12
```

Der Stack verwendet dann `${IMAGE_PREFIX}-app`, `-browser`, `-proxy`, `-ingress` und `-page-reader`.
`OWNER/REPOSITORY` ist ein Platzhalter. Es wurde bisher kein Repository gepusht
und kein Image öffentlich veröffentlicht. Lizenzen und Asset-Rechte vor einer
öffentlichen Veröffentlichung prüfen. Die lokale Auslieferung enthält keine
Laufzeitdaten, Browserprofile, API-/SMTP- oder Webseitenzugangsdaten.

Referenzen: [Portainer-Stacks](https://docs.portainer.io/user/docker/stacks/add),
[Docker-Volumes](https://docs.docker.com/engine/storage/volumes/),
[GitHub-Containerveröffentlichung](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images).

## Telegram und SMTP

Eigene Sidebar-Seiten, Testknöpfe und optionale ENV-Vorgaben stehen in
[KOMMUNIKATION.md](KOMMUNIKATION.md). Die Versandvariablen sind in
`.env.example` aufgeführt und gehen nur an App und Gateway. Neue Installationen
versenden standardmäßig keine Telegram-Nachrichten. Bestehende SMTP-Werte bleiben
bei einem Update erhalten.

Admin-Anmeldung: `KIWM_ADMIN_USERNAME` und `KIWM_ADMIN_PASSWORD` als Stack-ENV setzen.
Sitzungen und Abmelden: [ANMELDUNG.md](ANMELDUNG.md).
