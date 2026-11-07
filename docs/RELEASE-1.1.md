# Release 1.1.0 – Abnahme am 7. November 2026

Die bestehende LXC-Installation wurde über die Portainer-API auf den vereinfachten
Stack umgestellt. Alle fünf Dienste sind gesund. Die 14 Projekte, ihre
Aktivierung und nächsten Prüftermine blieben erhalten. Die sieben privaten
Konfigurationsdateien stimmen bytegenau mit dem Stand vor der Migration überein.
Anwendung und Brave sind über die bisherigen lokalen SSH-Tunnelports erreichbar.

## Vereinfachung

- Produktionsdatei von 218 auf 124 Zeilen verkürzt; fünf statt sieben Dienste.
- `compose.yaml` und `portainer-stack.yaml` sind bytegleich.
- Sechs übliche persönliche Variablen; Webkonfiguration bleibt verwendbar.
- Vier versionierte Images, Gesundheitsprüfungen und Standardkonfiguration eingebaut.
- Neue Daten-Volumes entstehen ohne Host-Bootstrap mit passenden Besitzrechten.
- Testportal und Mailpit nur im optionalen Test-Overlay.
- Offline-Sicherung und Restore als kurzlebige, isolierte Hilfscontainer.
- Bestehende Daten- und Browserverzeichnisse bei der Migration weiterverwendet.

## Tatsächliche Prüfungen

| Prüfung | Ergebnis |
| --- | --- |
| Anwendung aus ausgeliefertem Image | 571 Tests in 39 Suiten bestanden, nichts ausgelassen |
| Administrative Werkzeuge | 65 Tests bestanden |
| Neuinstallation mit vier leeren Volumes | Fünf Dienste gesund; Dashboard und noVNC HTTP 200 |
| Projekt über Webformular | Alias, Prompt und Tag gespeichert |
| Standardbetrieb ohne SMTP | Bericht als Vorschau gespeichert, kein Versand behauptet |
| Optionales Testzubehör | Sieben Dienste gesund; echte Mailpit-Nachricht mit HTML und Text empfangen |
| App-Container mit anderer interner IP neu erstellt | Bestehender ingress erreicht App weiter; Dashboard HTTP 200 |
| Vollständige Volume-Sicherung | Vier Datenbereiche und SHA-256-Manifest erstellt |
| Restore in unabhängige, leere Volumes | Alle 493 Dateien mit Manifest identisch; SQLite-Integrität bestätigt |
| Wiederhergestellte Nutzdaten | Ein Projekt, ein Lauf, ein PDF-Snapshot und zwei Berichte vorhanden |
| Wiederhergestellter Brave | Persistentes Test-Cookie und Local Storage nach Browserneustart verfügbar |
| Wiederhergestelltes PDF | Binärhash identisch; Dashboard und noVNC HTTP 200 |
| Netzwerkgrenzen | Private Proxy-Ziele gesperrt; kein direkter LAN-Zugriff aus der App |
| Bestehende Installation migriert | Fünf Release-Dienste gesund; alle 14 Projekte aktiviert und Termine erhalten |
| Portainer-Konfiguration | Neue YAML gespeichert; vorhandene ENV-Zuordnung erhalten, veraltete Variablen entfernt |
| Lokaler oMLX nach Migration | Fünf verfügbare Modelle abgefragt; echte Textantwort erfolgreich |
| Git-Historie | Bekannte aktuelle Zugangswerte in erreichbaren Git-Objekten nicht gefunden |
| Auslieferung | Vier Images gespeichert, komprimiertes Archiv geprüft und erneut in Docker geladen |

Die Browser-, Download-, Hash-, ZIP-, Export-/Import- und Anbieter-Schutzprüfungen
verwenden eigene kontrollierte Testseiten und Dateien. Die realen Anbieter wurden
für diese Release-Abnahme nicht erneut abgefragt; insbesondere wurde keine
Bot-Schutzprüfung bei der Normenbibliothek ausgelöst oder umgangen.

Die private konsistente Sicherung vor dem Update liegt auf der LXC unter
`backups/ki-webmonitor-20261106T214520Z.tar.gz`. Laufzeit-Belege stehen dort unter
`research/release-1.1-*`. Sie gehören nicht in öffentliche Quellpakete.

## Grenzen und Veröffentlichung

Die fertigen Images sind für Linux/amd64 gebaut und im Offline-Imagearchiv
enthalten. Ein öffentliches GitHub-Repository oder Registry-Paket wurde nicht
angelegt. Der Workflow für eigene GitHub-/GHCR-Releases ist vorbereitet, wurde
aber nicht auf GitHub ausgeführt. Für Registry-Updates erst veröffentlichen und
anschließend den eigenen `IMAGE_PREFIX` verwenden.

Browseranmeldungen und Zugangsdaten sind vertrauliche Laufzeitdaten. Sie sind
kein Bestandteil der ausgelieferten Images, des Quell-ZIPs oder Git-Bundles.
Backups müssen separat geschützt werden. Der enthaltene Restore-Helfer hat eine
Eingabegrenze von 900 MiB komprimiert beziehungsweise 20 GiB entpackt; größere
Bestände benötigen ein anderes geeignetes Volume-Backupverfahren.

Der Browser benötigt in dieser LXC weiterhin `--no-sandbox`. Container laufen
ohne Rootrechte, ohne Capabilities und ohne Docker-Socket; die private
Netzisolation und der lokale Zugangsweg wurden beibehalten. Ein kompletter
Neustart der LXC wurde in dieser Abnahme nicht durchgeführt; Container-
Neuerstellung und Browserneustart wurden tatsächlich geprüft.
