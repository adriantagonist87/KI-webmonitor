# KI-Webmonitor

Die [Projektübersicht mit Screenshots](README.md) führt durch die Funktionen und
die Installation direkt aus GitHub. Diese Seite beschreibt zusätzliche Betriebsdetails.

Webseiten und ihre verlinkten Dokumente mit einem lokalen KI-Server überwachen.
Brave, Anwendung und Daten laufen in Docker. Die KI-Inferenz erfolgt ausschließlich
über einen selbst betriebenen OpenAI-kompatiblen oMLX-Server; der Stack enthält
keine Modelle und benötigt keinen Cloud-KI-Anbieter.

- Projekte mit eigenem Prompt, Zeitplan, Tags und Webseitenzugang.
- Grafisch bedienbarer Brave mit Werbeblocker und dauerhaftem Anmeldeprofil.
- Optionaler [visueller Bedienmodus mit echter Maus und Tastatur](docs/VISUELLE-BROWSERBEDIENUNG.md), pro Projekt wählbar und zunächst in Erprobung.
- Technischer Webseitenvergleich und verständliche, aufklappbare Berichte.
- Vollständiger Dateidownload, **SHA-256-Vergleich und Dateimetadaten**.
- Der digitale Text der ersten Seite kann zur Ermittlung des Dokumenttitels
  verwendet werden. Der Dateivergleich erfolgt per Hash, ohne OCR.
- HTML-Wochenbericht mit Textalternative; konfigurierbarer SMTP-Versand.
- Telegram-Sofortmeldungen mit KI-Auswertung bei Änderungen und Prüfproblemen.
- Dateiverzeichnis, ZIP nach Projektordnern, Export und Reimport.

## Offline-Installation mit Portainer

1. Die fünf Release-Images auf dem Linux-Docker-Host laden oder aus der eigenen
   Registry beziehen. Ein vorhandenes Offline-Imagepaket lässt sich so laden:

   ```sh
   gzip -dc KI-Webmonitor-1.5.12-Images.tar.gz | docker image load
   ```

2. In Portainer **Stacks → Add stack → Web editor** öffnen und
   [portainer-stack.yaml](portainer-stack.yaml) einfügen. Alternativ das eigene
   Git-Repository mit dieser Datei auswählen.
3. Optional die Werte aus [.env.example](.env.example) als Stack-Variablen setzen.
   API-Adresse, Token und Modell können vollständig in der Weboberfläche bleiben.
4. Stack starten. Daten-Volumes, Konfiguration und Browserprofil entstehen
   automatisch. Kein Bootstrap-Skript und keine vorbereiteten Host-Verzeichnisse
   sind bei einer neuen Installation erforderlich.

Die [Installation mit veröffentlichten Images](docs/PORTAINER.md#testinstallation-aus-github)
ist ohne diesen Offline-Schritt möglich. Die fünf Images lassen sich auch mit
Docker Compose aus dem Quellcode bauen.
Der [GitHub-Workflow](.github/workflows/images.yml) kann Images in die Registry
des eigenen Repositories veröffentlichen. Beispieladressen mit `OWNER` sind
Platzhalter. [Portainer und Releases](docs/PORTAINER.md)

## Persönliche Einstellungen

| Variable | Standard / Wirkung |
| --- | --- |
| `VERSION` | `1.5.12`, gemeinsame Version der fünf Images |
| `WEB_PORT` | `8765`, geschützter Zugang zur Anwendung |
| `BROWSER_PORT` | `8766`, geschützter Zugang zum grafischen Brave |
| `KIWM_OMLX_URL` | Optional: Adresse des lokalen KI-Servers einschließlich Port |
| `KIWM_OMLX_API_KEY` | Optional: API-Token |
| `KIWM_MODEL` | Optional: ein gemeinsames Modell für alle Aufgaben |

Nichtleere KI-Environment-Werte haben Vorrang vor den gespeicherten Webwerten.
Leere Werte verwenden die Webkonfiguration. Getrennte Modellrollen,
Netzbereich und bestehende Bind-Verzeichnisse sind unter
[advanced.env.example](advanced.env.example) dokumentiert. Echte Tokens gehören
in private Portainer-/Environment-Einstellungen, niemals ins Repository.

## Sicher öffnen

Beide Ports sind ausschließlich an `127.0.0.1` des Docker-Hosts gebunden.
Auf dem Windows-PC beispielsweise diesen SSH-Tunnel offen lassen:

```powershell
ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 -L 127.0.0.1:8766:127.0.0.1:8766 BENUTZER@DOCKER-HOST
```

- Anwendung: <http://localhost:8765/>
- Brave: <http://localhost:8766/vnc.html?autoconnect=true&resize=scale>

`localhost` ist das lokale Tunnelende; Anwendung und Browser laufen auf dem
Docker-Host. Die SSH-Hostidentität vor der ersten Verwendung vertrauenswürdig
prüfen. Nach Portänderungen den Tunnel entsprechend anpassen.

## Bedienung

1. Unter **Einstellungen** die KI-Verbindung konfigurieren und Text, Tool Calling
   sowie Vision testen. Ein gemeinsames Modell genügt, wenn es alle Funktionen
   beherrscht; getrennte Modelle bleiben möglich.
2. Tags zentral unter **Tags** anlegen. Ein **neues Projekt** mit Alias, Start-URL,
   Tags, Farbe und Prompt speichern. Globale Prüfzeit übernehmen oder einen
   eigenen Zeitplan über die Auswahlfelder einstellen.
3. Zugangsdaten bei Bedarf im Projekt unter **Webseitenzugang** hinterlegen.
   Im Prompt nur darauf verweisen: „Verwende den hinterlegten Webseitenzugang.“
   Passwörter und Einmalcodes gehören nicht in den Prompt.
4. Für manuelle Anmeldung unter **Brave-Browser** den manuellen Modus aktivieren,
   anmelden und anschließend den manuellen Modus beenden. Cookies und Sitzung
   bleiben im Browser-Volume erhalten. Erforderliche CAPTCHAs/2FA bleiben manuell.
5. Das Refresh-Symbol startet die Prüfung. Das Auge öffnet Projekt und Auswertung.
   Der Automatik-Button rechts unten pausiert oder aktiviert die Zeitsteuerung.
   Pausierte Projekte lassen sich gelb filtern; wartende automatische Läufe
   entfallen beim Pausieren, laufende und manuelle Prüfungen bleiben erhalten.
   Änderungsbefunde sind direkt sichtbar; Quellen und technische Details lassen
   sich aufklappen. Eine allgemeine Promptvorlage steht in [docs/PROJEKTE.md](docs/PROJEKTE.md).

Eine erste Fassung ist ein Ausgangsstand. Dateien werden vollständig geladen;
gleiche Bytes gelten als unverändert, andere Bytes als **Datei geändert**.
Ein neues HTTP-Datum allein beweist keine Dateiänderung. Verfügbare PDF- und
HTTP-Datumsmetadaten werden zusätzlich angezeigt. Die optionale Titelermittlung
aus dem digitalen Text von Seite eins läuft unabhängig davon.
HTML-Artikel und sichtbare Webseiteninhalte kann die KI weiterhin auswerten.

## Prüfungen, Berichte und Speicher

Öffentliche GitHub-Projekte können mit `https://github.com/OWNER/REPO/releases`
überwacht werden. Der erste Lauf merkt sich vorhandene stabile Versionen als
Ausgangsstand. Spätere Läufe melden jeden neuen stabilen Versions-Tag einmal.
Entwürfe und Vorabversionen werden ausgefiltert. Die lokale KI fasst belegte
neue Funktionen aus den Release Notes zusammen. Beim Eintragen eigener erlaubter
Hostnamen sind `github.com` und `api.github.com` erforderlich.

Die [DKE-Arbeitsergebnisse](https://www.dke.de/de/normen-standards/arbeitsergebnisse#aetable)
werden über die öffentlichen Tabellenfragmente für nationale Normen geprüft.
Der Monatsabruf wird mit dem aktuellen und dem vorherigen Quartal abgeglichen.
Normnummer und Ausgabemonat identifizieren eine Ausgabe. Künftige Ausgabemonate
werden zurückgestellt. Der erste erfolgreiche Lauf erfasst den Ausgangsstand.
Ausgaben mit `VDE V` in der Bezeichnung werden als **Vornorm** gespeichert und
in der Auswertung sowie in Benachrichtigungen gekennzeichnet.

Telegram und SMTP haben eigene Einträge in der Sidebar. Beide lassen sich über
die Oberfläche und über optionale Portainer-ENV-Werte konfigurieren. Verbindung
und Testnachricht/Testmail können dort über Knöpfe geprüft werden. Telegram kann
global für alle Projekte aus- oder eingeschaltet werden oder den einzelnen
Projektvorgaben folgen. Standardmäßig ist es aus. Details und sämtliche Variablen:
[Telegram und SMTP](docs/KOMMUNIKATION.md).

Alle Projekte verwenden zunächst mindestens fünf Sekunden Abstand zwischen
gesteuerter Navigation, Scrollen und Downloads. Bereits verstrichene KI-Arbeitszeit
zählt mit. Erkannter Bot-Schutz beendet den aktuellen Lauf und unterdrückt die
zusätzliche 30-Minuten-Fehlerwiederholung; das Projekt bleibt regulär aktiviert.
HTTP `Retry-After` wird berücksichtigt. Es gibt keine CAPTCHA-Umgehung.

Der konsolidierte Wochenbericht entsteht montags um 08:00 Uhr Europe/Berlin.
Ohne produktive SMTP-Konfiguration bleibt er als **Vorschau · nicht versandt**
gespeichert. Ein Testbericht ist ebenfalls eine Vorschau. Das optionale
[Testzubehör](compose.test.yaml) bietet echtes Test-SMTP mit Mailpit.
Produktiver Versand wird ausschließlich über die SMTP-Einstellungen aktiviert.

Identische Originale werden gemeinsam gespeichert. Standardmäßig bleiben zwei
Dateiversionen je Quelle sowie 30 Tage Detaildateien erhalten. Die Aufbewahrung
ist in der Oberfläche einstellbar. Der ZIP-Sammeldownload enthält portable,
bereinigte Projektordner und ein Zuordnungsmanifest.

## Aufbau und Daten

Der normale Stack enthält sechs Dienste. Alle laufen ohne Rootrechte, mit
schreibgeschütztem Root-Dateisystem, ohne Linux-Capabilities und ohne Docker-Socket.

| Dienst | Aufgabe | Daten-Volumes |
| --- | --- | --- |
| app | Oberfläche, Agent, SQLite, Zeitplanung, Berichte | `data`, `config`, `secrets` |
| browser | Brave, CDP und noVNC | `browser-data` |
| page-reader | Digitaler Text der ersten Druckseite, ohne Netzwerkzugang | `page-reader-socket` |
| proxy | Internetzugriff, sperrt private Ziele | keine |
| gateway | Konfigurierte lokale KI-API, SMTP und feste Telegram-Bot-API | `config`, `secrets`, nur lesbar |
| ingress | Lokaler Zugang zu App und Brave | keine |

Docker versieht Volume-Namen mit dem Stacknamen. Die Anwendung besitzt keinen
direkten Internet-/LAN-Zugang. Interne Ports werden nicht veröffentlicht:
App/Gateway 8000, Proxy 3128, CDP 9223 und noVNC 6080. Nur ingress veröffentlicht
die lokalen App-/Browserports. Brave benötigt in der getesteten LXC
`--no-sandbox`; die Container- und Netzgrenzen bleiben deshalb relevant.

## Compose und Entwicklung

`compose.yaml` und `portainer-stack.yaml` sind identisch und verwenden fertige
Images. Nach dem Laden der Images genügt:

```sh
docker compose up -d --wait
```

Zum lokalen Bauen aus dem Quellcode:

```sh
docker compose -f compose.yaml -f compose.build.yaml build
docker compose up -d --wait
python3 scripts/validate-compose.py --env-file /dev/null
```

`compose.test.yaml` ergänzt ausschließlich optional Mailpit und das eigene
Testportal. `compose.tools.yaml` enthält Offline-Backup und Wiederherstellung.
Unterstützt und zu prüfen ist Linux amd64 mit Docker Standalone und Compose v2;
Swarm und andere CPU-Architekturen sind nicht zugesichert.

- [Betrieb, Backup, Wiederherstellung, Update und Deinstallation](docs/OPERATIONS.md)
- [Portainer, Variablen und GitHub-Veröffentlichung](docs/PORTAINER.md)
- [Git und private Dateien](docs/GIT.md)
- [Release-1.2-Prüfung](docs/RELEASE-1.2.md)

Die Dashboard-Bedienung von Release 1.2.1 ist in
[RELEASE-1.2.1.md](docs/RELEASE-1.2.1.md) dokumentiert.

Der optionale HTTP-LAN-Zugang aus dem lokalen Netzwerk ist in
[LAN-Zugang](docs/LAN-ZUGANG.md) dokumentiert. Der SSH-Zugang bleibt parallel erhalten.
Externes HTTPS über einen [Cloudflare Tunnel](docs/CLOUDFLARE.md) wird mit
`KIWM_PUBLIC_URL` ausdrücklich freigegeben.

Admin-Anmeldung und Portainer-ENV: [Anmeldung](docs/ANMELDUNG.md).

## Aktueller Bedienungs- und VDE-Befund

Der optionale visuelle Modus wurde mit VDE erfolgreich geprüft: Anmeldung,
Filter „Normen“, 17 Einträge des Ausgabejahrs 2026 und KI-Bericht.
[Visueller Bedienmodus](docs/VISUELLE-BROWSERBEDIENUNG.md)
Bot-Schutz beendet weiterhin den aktuellen Versuch ohne Zusatzwiederholung;
der reguläre Zeitplan bleibt aktiv.

## Kompakter Wochenbericht

Release 1.4 verwendet dieselbe HTML-Ausgabe für Mail und isolierte Vorschau, mit Tagfarben, eingebetteten Favicons und drei Spalten für unveränderte Quellen. [Aufbau und Prüfungen](docs/WOCHENBERICHT-1.4.md).

Farbige Dateisymbole und die systemunabhängige Animationssteuerung: [Darstellung 1.4.1](docs/DARSTELLUNG-1.4.1.md).

Kompakte Dateisymbole und Dokumentnamen aus digitalem Text von Seite eins: [Dateiansicht](docs/DOKUMENTNAMEN-1.5.1.md).

## Verteilerlisten

Der Admin verwaltet Empfänger mit Alias sowie Projekt- und Tag-Auswahl über den Sidebar-Eintrag „Verteilerlisten“. Empfängerberichte enthalten ausschließlich relevante Änderungen und unveränderte Quellen. Globaler Bericht, Empfängerfassungen und Versandprotokoll bleiben im Adminarchiv einsehbar. [Verteiler und Berichtsarchiv](docs/VERTEILER.md).
