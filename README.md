<p align="center">
  <img src="app/static/app-logo.png" width="112" alt="Logo des KI-Webmonitors">
</p>
<h1 align="center">KI-Webmonitor</h1>
<p align="center"><strong>Webseiten und Dokumente im Blick behalten</strong></p>

Der kostenlose KI-Webmonitor prüft Webseiten und verlinkte Dateien nach einem festgelegten Zeitplan. Du legst für jedes Projekt fest, welche Bereiche er prüfen und welche Änderungen er melden soll. Im Dashboard findest du die Ergebnisse mit Quellenlink und Prüfverlauf. Heruntergeladene Dateifassungen stehen im Archiv zum Vergleich bereit.

Web-App, Brave und Archiv laufen in Docker. Für die KI-Auswertung verbindet sich der Stack mit einem selbst betriebenen lokalen oMLX-Server über dessen OpenAI-kompatible API oder einen Cloudanbieter deiner Wahl.

![Projektübersicht mit Favicons für DKE, VDE, elektro.net und weitere Projekte aus der Gruppe Arbeit](docs/screenshots/dashboard.png)

*Die Screenshots zeigen die App mit ausgewählten Arbeitsprojekten und Beispieldaten.*

## Ideen für eigene Projekte

Für Architektur und Technische Gebäudeausrüstung (TGA) in Deutschland bieten sich die Veröffentlichungsseiten von Behörden, Netzbetreibern, Fachverbänden und Verlagen an. Im Projektauftrag legst du fest, welche Dokumente, Fachmeldungen oder Artikel der Monitor erfassen soll. So kannst du die Auswahl auf ein Bundesland, ein Gewerk oder ein bestimmtes Bauvorhaben eingrenzen.

- **Gesetze und Bauvorschriften.** Verfolge Änderungen an Landesbauordnungen, Sonderbauverordnungen und Vorschriften zur Gebäudeenergie. Archiviere neu veröffentlichte Fassungen technischer Baubestimmungen und aktualisierte Bauantragsformulare. [Bundesrecht zur Gebäudeenergie](https://www.gesetze-im-internet.de/geg/) · [Technische Baubestimmungen beim DIBt](https://www.dibt.de/de/wir-bieten/technische-baubestimmungen)
- **Heizung, Lüftung, Klima und Sanitär.** Erfasse neue oder überarbeitete Planungsempfehlungen zu Wärmeversorgung, Kälteanlagen, Raumlufttechnik und Trinkwasserinstallationen. [Planungsempfehlungen des AMEV](https://www.amev-online.de/amevinhalt/planen/)
- **Elektrotechnik und Gebäudeautomation.** Beobachte neue Normausgaben und technische Hinweise zu Photovoltaik, Ladeinfrastruktur, Sicherheitsstromversorgung und Gebäudeautomation. Lass dir ausgetauschte Anschlussbedingungen oder Formulare deines Netzbetreibers melden. [Anschlussunterlagen von Stromnetz Berlin](https://www.stromnetz.berlin/partner/installateure/installateur-unterlagen/)
- **Architektur, Brandschutz und Bauphysik.** Sammle neue Arbeitshilfen zum barrierefreien Bauen, zum Schallschutz und zur Sanierung im Bestand. Prüfe, ob Feuerwehren Merkblätter zu Feuerwehrplänen oder anderen Vorgaben des vorbeugenden Brandschutzes aktualisieren. [Leitfaden Barrierefreies Bauen des Bundes](https://www.leitfadenbarrierefreiesbauen.de/) · [Merkblätter der Berliner Feuerwehr](https://www.berliner-feuerwehr.de/ihre-sicherheit/vorbeugender-brand-und-gefahrenschutz/)
- **BIM, Vergabe und Wettbewerbe.** Verfolge neue Ausschreibungen für Planungsleistungen, geänderte Vergabeunterlagen und neue Wettbewerbsauslobungen. Sammle überarbeitete BIM-Leitfäden und Vorlagen für Auftraggeber-Informationsanforderungen (AIA). [Muster-AIA bei BIM Deutschland](https://www.bimdeutschland.de/bim-wissen/auftraggeber-informationsanforderungen)
- **Fachartikel und Branchenmeldungen.** Sammle auf Fachportalen, Kammerseiten und in Zeitschriftenarchiven neue Beiträge zu Themen wie Wärmepumpen im Bestand, Holzbau, Förderung oder Honorarfragen. Der Wochenbericht bündelt die Treffer mit ihren Quellenlinks. [Meldungen aus dem Bundesbau](https://www.fib-bund.de/Inhalt/Neuigkeiten/)

## Installation

Der KI-Webmonitor läuft auf einem Linux-Docker-Host mit `amd64`. Alle drei Wege verwenden dieselben veröffentlichten Images und unterstützen den Zugriff per SSH-Tunnel oder im lokalen Netz.

| Installationsweg | Anleitung |
| --- | --- |
| **Portainer** | [Stack aus dem Release-Tag laden](docs/PORTAINER.md#testinstallation-aus-github) |
| **Dockhand** | [Git-Stack oder lokalen Stack im Editor anlegen](docs/DOCKHAND.md) |
| **Docker Compose** | [Projekt im Terminal einrichten und starten](docs/COMPOSE.md) |

Für die KI-Funktionen benötigt der Stack einen erreichbaren oMLX-Server. Nach der Anmeldung unter **Einstellungen** die oMLX-Adresse, den API-Token und das Modell hinterlegen und die Verbindung testen. Dieselben Werte lassen sich als `KIWM_OMLX_URL`, `KIWM_OMLX_API_KEY` und `KIWM_MODEL` in der Stack-Konfiguration setzen. Nichtleere Umgebungsvariablen haben Vorrang vor den Einstellungen in der App. Für Text, Tool Calling und Vision lassen sich bei Bedarf getrennte Modelle wählen.

## Ein Projekt einrichten

1. **Neues Projekt** wählen. Name, Start-URL, Tags und globalen oder individuellen Prüfzeitplan festlegen.
2. Im Prompt konkret beschreiben, **wo** der Agent suchen soll, **welche Inhalte** in den Prüfbereich gehören und **was** als Änderung zählt. Bei Menüs und Klappbereichen die sichtbaren Beschriftungen nennen. Erlaubte Hostnamen begrenzen die Webseiten, die das Projekt aufrufen darf.
3. Falls nötig, die Anmeldung über **Webseitenzugang** konfigurieren oder die Seite im grafischen Brave öffnen und sich dort anmelden. Brave speichert die Sitzung im Browserprofil. Zugangsdaten gehören in die Zugangseinstellungen, Passwörter und Einmalcodes bei manueller Anmeldung direkt in den Browser.
4. Die erste Prüfung über das Aktualisieren-Symbol starten und den erfassten Umfang prüfen. Der erste vollständige Lauf bildet den Ausgangsstand. Die Automatik lässt sich auf der Projektkarte jederzeit pausieren oder wieder einschalten.

Ein kurzer Beispielauftrag für eine Dokumentseite:

> Prüfe den Abschnitt „Merkblätter“. Klappe jede Kategorie nacheinander auf und erfasse alle dort verlinkten PDFs. Melde neue Merkblätter und Dateien, deren SHA-256 sich unter derselben URL geändert hat. Nenne je Änderung den Titel und den Quellenlink. Führe nicht erreichbare Quellen gesondert auf.

Weitere Beispiele für Artikel, Dokumentlisten, Akkordeons und Login stehen in der App und in [Neue Webseiten einrichten](docs/PROJEKTE.md).

## Änderungen nachvollziehen

Das Dashboard zeigt Prüfstatus, offene Änderungen und Hinweise. Es lässt sich nach Tags, Status und Prüfzeitpunkt filtern oder sortieren. In der Projektansicht steht die fachliche Auswertung zuerst. Der Verlauf führt zu einzelnen Prüfungen mit Quellen, Screenshots, technischen Unterschieden und Aktionsprotokoll.

![Projektauswertung für die DKE-Arbeitsergebnisse mit einer als Vornorm gekennzeichneten Ausgabe](docs/screenshots/dke-auswertung.png)

Für Fachartikel kann die KI die zugänglichen Texte kurz zusammenfassen, etwa die beantworteten Fragen in einem Fachportal. Der Prüfbericht weist auf offene oder nicht erreichbare Quellen hin.

![Auswertung von beantworteten Praxisfragen bei elektro.net](docs/screenshots/elektro-auswertung.png)

### Dateien und Dateinachweise

Bei PDF-, Word- und Excel-Dateien lädt der Monitor die Originaldatei und berechnet ihren SHA-256-Hash. Ein anderer Hash belegt, dass sich die Dateibytes geändert haben. PDF- oder HTTP-Datumsangaben werden ergänzend ausgewiesen. Für die fachliche Prüfung stehen die archivierten Fassungen zum Download bereit.

Das **Globale Dateiverzeichnis** bietet Suche und Sortierung über Projekte hinweg, einzelne Downloads und einen ZIP-Download mit einem Ordner je Projekt. Der separate Dokumentname kann aus dem digitalen Text der ersten Seite ermittelt werden. Diese Titelermittlung ist vom Hashvergleich getrennt und erfolgt ohne OCR. Aus einer geänderten Datei leitet die App keine Aussage über geänderte fachliche Inhalte ab.

![Dateiverzeichnis mit Projekt-Favicons, Dateityp, Dokumenttitel, Originaldateiname und Download](docs/screenshots/dateiverzeichnis.png)

## Benachrichtigungen und Wochenberichte

- **Telegram** meldet relevante Änderungen und Prüfprobleme. Die Automatik lässt sich global oder je Projekt steuern. Meldungen verlinken ihre Quellen.
- **E-Mail** liefert einen HTML-Wochenbericht mit Textalternative. Der Bericht entsteht montags um 08:00 Uhr in der Zeitzone `Europe/Berlin`. SMTP, Verbindungstest und Testmail lassen sich in der App einrichten. Vorschauen können ohne produktiven Versand erstellt werden.
- **Verteilerlisten** beschränken Empfängerberichte auf ausgewählte Projekte oder Tags. Empfänger sehen relevante Änderungen und unveränderte Quellen, jeweils mit Originalquellen. Adminhinweise und interne Webmonitor-Links bleiben im Adminbericht. Die Adminansicht archiviert Berichte und Versandstatus chronologisch.

![Wochenbericht für den Tag Arbeit mit Favicons der Quellen in einer geöffneten E-Mail im Stil von Outlook 2024](docs/screenshots/wochenbericht.png)

*Nachgestelltes Outlook-2024-Fenster mit Beispieladressen.*

[Telegram und SMTP](docs/KOMMUNIKATION.md) · [Verteiler und Versandprotokoll](docs/VERTEILER.md)

## Betrieb, Daten und Grenzen

- **Datenhaltung.** Projekte, Prüfverlauf, Berichte und Dateien bleiben in Docker-Volumes. Einfache Exporte enthalten die Projektkonfiguration, erweiterte Exporte auch den Verlauf. Für eine vollständige Wiederherstellung ist eine Sicherung der persistenten Volumes einschließlich Browserprofil und privater Konfiguration erforderlich. Aufbewahrungsgrenzen lassen sich in der App einstellen.
- **Webseitenzugriff.** Brave unterstützt JavaScript, Cookies und grafische Anmeldung. Der Agent kann Links, Klappbereiche und ausgewählte Bedienelemente nutzen. Bei erkanntem Bot-Schutz endet die Prüfung. Die nächste reguläre Prüfung bleibt geplant. CAPTCHAs und erforderliche manuelle Bestätigungen werden im Browser erledigt.
- **Prüfgrenzen.** Zeit, Anzahl der Seiten und Downloads sowie Dateigröße sind pro Lauf begrenzt. Das Ergebnis hängt vom sichtbaren Inhalt, der Webseitenstruktur und dem eingesetzten Modell ab. Unvollständige Prüfungen sind im Dashboard erkennbar.
- **Zugriff und Geheimnisse.** Den Stack per SSH-Tunnel oder im vertrauenswürdigen LAN mit Adminanmeldung betreiben. Zugangsdaten, API-Token, Browserprofile und Sicherungen gehören in private Speicherorte. Für öffentlichen HTTPS-Zugriff gelten die Einstellungen für den vorgeschalteten Proxy.

| Thema | Dokumentation |
| --- | --- |
| Umgebungsvariablen und Image-Build | [Beispiel für `.env`](.env.example), [erweiterte Variablen](advanced.env.example), [Release-Images bauen](docs/PORTAINER.md#github-und-registry) |
| Sicherung, Wiederherstellung und Updates | [Betrieb](docs/OPERATIONS.md) |
| Adminzugang und externer Zugriff | [Anmeldung](docs/ANMELDUNG.md), [Cloudflare Tunnel](docs/CLOUDFLARE.md) |
| Browser, Dienste und Netzgrenzen | [Architektur](docs/ARCHITEKTUR.md), [visuelle Brave-Bedienung](docs/VISUELLE-BROWSERBEDIENUNG.md) |

Der Quellcode steht unter der [MIT-Lizenz](LICENSE).
