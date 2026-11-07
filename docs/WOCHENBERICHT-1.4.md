# Wochenbericht 1.4.0

Der Wochenbericht verwendet eine gemeinsame HTML-Ausgabe für SMTP und Web-Vorschau.
Die Originalanlagen werden weiterhin ausschließlich anhand von Hash und Metadaten
überwacht. Ihre Inhalte gehen nicht an die KI.

## Aufbau und Daten

- Aktuelle Tag-Namen und HEX-Farben aus der zentralen Tagverwaltung.
- Pro Tag: relevante Änderungen, Hinweise, anschließend unveränderte Quellen in drei Spalten.
- Gesamtzahl eindeutiger Projekte; Mehrfach-Tags vervielfachen nur die Gruppenzuordnung.
- Ein kurzer Nachrichtensatz pro geändertem/auffälligem Projekt. Die lokale Analyse-KI
  verdichtet ausschließlich bereits gespeicherte Änderungsbelege. Bei Fehler oder nach
  höchstens 120 Sekunden bleibt eine knappe Zusammenfassung der vorhandenen Belege erhalten.
- Ein späterer Abruffehler verdrängt keine zuvor erkannte Änderung des Berichtszeitraums.
- Alle Quellen werden ausgegeben, auch bei 100 Projekten. Keine Begrenzung auf die Demo-Daten.

Die ursprüngliche Quellen-URL steckt hinter dem Projektnamen. „Webmonitor“ führt auf
`KIWM_PUBLIC_URL` plus die gemeinsame Projektdetailroute. Ohne konfigurierte öffentliche
HTTPS-Adresse entfällt dieser zusätzliche Link; es werden keine lokalen Ersatzadressen
in E-Mails eingesetzt. Quelladressen mit eingebetteten Zugangsdaten oder bekannten
Passwort-/Token-Queryparametern werden nicht als Link ausgegeben.

## Gemeinsamer Renderer und Bilder

`email_reports.build_model()` aggregiert die Daten, `compact_headlines()` verdichtet die
vorhandenen Befunde, `render()` erzeugt einmal HTML, Klartext und Bildteile.
`make_report()` speichert diesen Stand vor dem Versand unter `data/reports/`:

- `<id>.html`: tatsächlicher Mailinhalt
- `<id>.txt`: lesbare Textalternative
- `<id>.images.json`: unveränderlicher PNG-Bildbestand und Berichtszähler

SMTP-Wiederholungen lesen diese Dateien erneut; sie erzeugen keinen neuen Bericht.
Ältere, bereits gespeicherte Berichte werden nicht nachträglich umgestaltet.
Erweiterter Export und Volume-Backup schließen die Bilddatei automatisch ein.

Vorhandene Favicons werden aus dem Projektbestand übernommen. SVGs werden nach der
bestehenden Bereinigung lokal mit CairoSVG zu PNG gerendert. Fehlende/defekte Icons
erhalten einen neutralen Platzhalter. Gleiche PNGs werden innerhalb der Mail dedupliziert.
Das Original-App-Logo bleibt transparent. Es werden keine externen Bildadressen geladen.
`mail_message.build()` erzeugt multipart/alternative und multipart/related mit CID-PNGs.
Kurze ASCII-Content-IDs verhindern fehlerhaft umkodierte MIME-Bildkennungen.

Die geschützte Route `/reports/<id>/html` liefert dasselbe gespeicherte HTML. Nur
`cid:`-Bildreferenzen werden auf geschützte `/reports/<id>/assets/<cid>`-Routen geändert.
Ein Sandbox-Iframe trennt den Bericht vom Dashboard-CSS. Skripte im Mailinhalt sind
verboten. Nur diese Vorschau darf durch dieselbe App eingerahmt werden; die übrigen
Oberflächen behalten ihren Schutz gegen fremdes Einbetten.
`/reports/<id>/message.eml` erlaubt einen Download mit neutralen Vorschau-Mailheadern.
Für Transportheader und die Darstellung beim Empfänger ist die tatsächlich empfangene
EML maßgeblich.

## Prüfung am 7. November 2026

223 gezielte Anwendungstests bestanden, keine ausgelassen, darunter 23 neue Berichtstests.
Geprüft wurden die 14-Quellen-Demo, 100 Quellen, echte Privat-Zuordnung, Mehrfach-Tags,
leere Gruppen, defekte/SVG-Favicons, HTML-Escaping, gefährliche URLs, spätere Abruffehler,
Modellausfall, MIME-Bildreferenzen, Vorschau-Routen, Archivexport und Wiederherstellung.
Anmeldung, externer Zugang, SMTP-Konfiguration, Wiederholungsjobs und bestehender
Berichtsversand wurden in den Regressionstests mitgeprüft.

Ein echter SMTP-Versand an den isolierten Mailpit wurde empfangen. Die daraus gelesene
EML enthält genau den gespeicherten HTML-Inhalt und die passenden Inlinebilder.
Die Vorschau wurde durch den neuen nginx-Container in Brave geladen; alle 14 Quellen
und sämtliche Icons waren sichtbar, die Schrift blieb vom Dashboard isoliert.
HTTP-zu-HTTPS-Umleitung, Anmeldung, LAN-Zugriff, Brave/noVNC, authentifizierter WebSocket,
Abweisung fremder Origins und Abmeldung bestanden den separaten nginx-Integrationstest.

Die erste Thunderbird-Sichtprüfung deckte eine MIME-Faltung zu langer Content-IDs auf.
Diese wurde behoben und durch einen eigenen Regressionstest abgesichert.
Outlook ist installiert, wurde vom Computer-Use-Berechtigungssystem aber nicht zum
Zugriff freigegeben. Die EML wurde stattdessen in einem isolierten Thunderbird-Container
geöffnet und visuell geprüft; Logo, alle Inlinebilder, Tagfarben und drei Spalten waren korrekt;
eine Outlook-Abnahme ist damit nicht behauptet. Empfängerprogramme können eigene
Schrift-/Bildrichtlinien anwenden; Pixelgleichheit zwischen Mailclients ist nicht zugesichert.

Die ursprünglichen Designreferenzen und Demo-Projektlisten wurden nicht in Produktdaten
übernommen. Der normale Wochenzeitplan und die Erkennung von Webseitenänderungen
bleiben unverändert.

## Betriebsabnahme

Version 1.4.0 wurde über den bestehenden Portainer-Stack eingespielt. Alle fünf Dienste
sind gesund; Browserprofil, Einstellungen, ENV-Werte und vorheriger Automatikmodus
wurden erhalten. Für den direkten Compose-Betrieb wurde auch die lokale VERSION
mit dem laufenden Stack abgeglichen. Vorher wurden Datenbank, Stack und Quelldateien gesichert.

Der Bericht mit den realen 14 Projekten wurde über die konfigurierte lokale Analyse-KI
erstellt: 2 Projekte mit Änderungen, 1 mit Hinweisen, 11 unverändert. Auf ausdrücklichen
Wunsch wurde genau eine Testmail über das konfigurierte produktive SMTP-Konto gesendet;
der Server nahm sie an. Der anschließend vom Nutzer gezeigte Empfangsscreenshot bestätigt
Logo, projektbezogene Favicons, Tagfarben und die dreispaltige Darstellung.

Die öffentliche HTTPS-Vorschau und ihre Bild-/EML-Routen bestanden die Prüfung mit
der bestehenden realen Anmeldung. Der HTML-Inhalt stimmt exakt mit der gespeicherten
Mail überein; alle 10 deduplizierten Inlinebilder wurden korrekt ausgeliefert. Nach der
Abmeldung sind Vorschau, Bilder und EML-Download wieder gesperrt. Die Dateianalyse
bleibt auf Hash/Metadaten beschränkt.
