# Betrieb, Backup und Wiederherstellung

## Zustandsprüfung

In Portainer müssen alle fünf Betriebsdienste **healthy** sein. Alternativ
`docker compose ps`. Bei Docker Standalone startet `restart: unless-stopped`
nach Prozessabbruch/Hostneustart erneut; ein Zustand `unhealthy` allein löst
keinen Neustart aus. Browserprofile und SQLite niemals gleichzeitig in zwei
Installationen verwenden.

## Prüfverlauf aller Projekte bereinigen

Unter **Einstellungen → Prüfverlauf** die neuesten Prüfungen je Projekt,
einen Zeitraum in Tagen oder den gesamten Prüfverlauf auswählen. Die Vorschau
zeigt die Anzahl der Prüfungen und Projekte vor der endgültigen Bestätigung.
Ein Zeitraum bezieht sich auf das Abschlussdatum der Prüfung. Laufende oder
wartende Prüfungen müssen vorher abgeschlossen sein.

Die Bereinigung entfernt die ausgewählten Läufe samt Änderungen und nicht
mehr benötigten Belegen. Gemeinsam genutzte Dateien bleiben verfügbar, solange
eine verbleibende Prüfung sie referenziert. Projekte, Zeitpläne und bereits
gespeicherte Wochenberichte bleiben erhalten. Verweise alter Berichte auf
gelöschte Prüfungen sind danach nicht mehr aufrufbar. Quellen ohne verbleibenden
Vergleichsstand werden im nächsten Lauf neu erfasst. SQLite verwendet die
freigewordenen Datenbankseiten für neue Einträge.

Vor einer endgültigen Löschung kann unter **Export / Import** ein ZIP mit
Verlauf gespeichert werden. Seit 1.5.17 werden die Seitentexte darin als
separate ZIP-Einträge geschrieben. Der einfache JSON-Export ist weiterhin
mit Version 8 des Transferformats kompatibel. Für einen ZIP-Import im neuen
Format 9 mindestens App-Version 1.5.17 verwenden. Bei einem Rechtefehler
die Eigentümerschaft der betroffenen Archivdatei für den App-Benutzer
(UID 1000) prüfen.

## Vollständige Volume-Sicherung

Die Oberfläche exportiert Projekte/Einstellungen und optional Verlauf/Dateien.
Für Browseranmeldungen und Webseitenzugänge ist zusätzlich eine vollständige
Sicherung der vier Daten-Volumes nötig. Das Archiv ist vertraulich und sollte
extern verschlüsselt aufbewahrt werden. Portainer-ENV separat sichern.

Eine laufende Prüfung zuerst abschließen lassen. Dann auf dem Linux-Docker-Host:

```sh
umask 077
docker compose stop app browser
docker compose -f compose.yaml -f compose.tools.yaml run --rm --no-deps -T backup < /dev/null > webmonitor-backup.tar.gz
sha256sum webmonitor-backup.tar.gz > webmonitor-backup.tar.gz.sha256
docker compose up -d --wait
```

Exitcode der Sicherung prüfen; bei Fehler kein unvollständiges Archiv verwenden.
Auch nach einer fehlgeschlagenen Sicherung die zuvor laufenden Dienste wieder
starten. Bei abweichendem Portainer-Stacknamen bei jedem Befehl `-p STACKNAME`
angeben; dieselben privaten Environment-/Speichervorgaben wie im Stack verwenden.

Der Hilfscontainer läuft ohne Netzwerk, Rootrechte oder Docker-Socket. Er liest
alle vier Volumes ausschließlich lesend und schreibt das komprimierte Archiv
auf stdout. Es enthält ein SHA-256-Dateimanifest und Datenbank, Berichte,
Originaldateien, Konfiguration, Geheimnisse sowie das Browserprofil. Alte
zusätzliche Browser-/Mailpit-Verzeichnisse unter dem App-Datenverzeichnis werden
nicht doppelt aufgenommen. Sperrdateien des Browsers werden ausgelassen.

## Wiederherstellung auf einem neuen Host

Images und Compose-Dateien übertragen. Eine neue Installation mit **leeren**
Volumes verwenden; sie noch nicht starten. Dann:

```sh
sha256sum -c webmonitor-backup.tar.gz.sha256
docker compose -f compose.yaml -f compose.tools.yaml run --rm --no-deps -T restore < webmonitor-backup.tar.gz
docker compose up -d --wait
```

Die vier Volumes entstehen bei Bedarf automatisch. Der Helfer verweigert
vorhandene Zieldaten und prüft Pfade, Dateitypen, Manifest und Dateihashes vor
dem ersten Schreiben. Links, Pfadtraversal und Archivbomben werden abgewiesen.
Bei einem Schreib-/Platzfehler die unvollständigen neuen Ziel-Volumes gezielt
entfernen und mit leeren Volumes wiederholen; niemals produktive Daten löschen.
Der enthaltene Restore-Helfer verarbeitet bis zu 900 MiB komprimiert und 20 GiB
entpackt. Größere Bestände mit einem geeigneten Docker-Volume-Backupverfahren
sichern/wiederherstellen. Die Archiv-Erzeugung selbst streamt ohne diese
900-MiB-Eingabegrenze.

Danach prüfen: fünf gesunde Dienste, Projektanzahl, Berichte und Dateidownloads,
Webseitenzugänge und gespeicherte Browseranmeldung. ENV-Overrides wieder setzen
und KI-Verbindung testen. Erst danach eine alte Installation entfernen.

## Optionales Testzubehör

```sh
docker compose -f compose.yaml -f compose.test.yaml up -d --wait
```

Das ergänzt Mailpit und das eigene Testportal. Testberichte werden tatsächlich
an den lokalen Test-SMTP auf Port 1025 geschickt; das Postfach ist ausschließlich
über `127.0.0.1:8767` beziehungsweise SSH-Tunnel erreichbar. Das Testportal hat
keinen veröffentlichten Port. Nur in diesem Modus erlaubt der Proxy dessen
festgelegte private Adresse auf Port 8000. Zum Entfernen zuvor Testberichte
abschließen und wieder die normale Konfiguration einsetzen; alte Testcontainer
gezielt über Portainer entfernen. Testpostfach-Inhalte sind temporär.

## Bestehende Bind-Installation aktualisieren

Die bestehende Installation unter `/opt/ki-webmonitor` kann ihre Daten behalten.
Vorher mit dem alten geprüften Backup-Verfahren sichern. In ihrer privaten
`.env` die vorhandenen Quellen ausdrücklich setzen:

```dotenv
VERSION=1.2.1
KIWM_DATA=/opt/ki-webmonitor/data
KIWM_CONFIG=/opt/ki-webmonitor/config
KIWM_SECRETS=/opt/ki-webmonitor/secrets
KIWM_BROWSER_DATA=/opt/ki-webmonitor/data/browser
```

Die vorhandenen KI-Environment-Werte, Ports und den Netzbereich erhalten. Nach
Laden der vier Release-Images kann der administrative Helfer die Migration
im vorhandenen Portainer-Stack ausführen:

```sh
python3 scripts/redeploy.py --stack-id ID --endpoint-id ENDPOINT --expected-name STACKNAME --token-file /privater/pfad/token.jwt --retire-test-services
```

Der Token muss privat (0600) sein. Der Helfer prüft die lokale Stackidentität,
wartende/laufende Prüfungen, unveränderte Daten- und Browserpfade, Variablen,
Image-Dateirechte und Gesundheitszustand. Ausschließlich eindeutig zugeordnete
alte `fixture`-/`mailpit`-Container werden entfernt; ihre Hostdaten bleiben.
Bekannte veraltete Stackvariablen entfallen, gültige KI-Vorgaben bleiben erhalten.
Diese Hilfe setzt die bestehende Bind-Installation voraus. Neue Installationen
benötigen sie nicht. Bestehende Host-Backups mit `scripts/backup.py` bleiben
für diese Bind-Verzeichnisse nutzbar; die Volume-Helfer gelten ebenfalls.

## Update und Deinstallation

Für ein Update sichern, aktive Prüfungen abschließen lassen, neue Images laden,
`VERSION` ändern und den vorhandenen Stack aktualisieren. Anschließend Dienste,
UI, Browser und KI-Verbindung prüfen. Bei Portainer als Verwalter nicht parallel
dieselbe Installation mit Compose verändern. Administrative Offline-Sicherung
und die dokumentierte Migration erfordern eine abgestimmte Wartungszeit.

Zum Entfernen den Stack in Portainer löschen oder `docker compose down`
verwenden. Die Daten bleiben zunächst erhalten. Nur wenn ausdrücklich auch
alle Daten gelöscht werden sollen, die vier exakt zugeordneten Volumes unter
Portainer → Volumes löschen oder nach Prüfung `docker compose down --volumes`
verwenden. Bind-Verzeichnisse werden dadurch nicht gelöscht und müssen bei
gewünschter vollständiger Entfernung separat, nach Pfadprüfung, entfernt werden.
Keine globalen Docker-Prune-Befehle verwenden. Fremde Dienste und Portainer
selbst gehören nicht zur Deinstallation.

Referenzen: [Docker-Volume-Backup](https://docs.docker.com/engine/storage/volumes/#back-up-restore-or-migrate-data-volumes),
[Compose-Überschreibungen](https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/).

## Kommunikationswege

Telegram und SMTP benötigen keine zusätzlichen Volumes oder veröffentlichten
Ports. Webwerte und Zugangsdaten liegen in den bestehenden Konfigurations- und
Geheimnis-Volumes. Der Telegram-Ausgang ist Teil der SQLite-Datenbank und wird
mitgesichert. Nach Restore werden während des Backups laufende Zustellungen als
unklar behandelt und nicht automatisch doppelt versandt. Die einzelnen
Einrichtungs- und Testschritte stehen in [KOMMUNIKATION.md](KOMMUNIKATION.md).
