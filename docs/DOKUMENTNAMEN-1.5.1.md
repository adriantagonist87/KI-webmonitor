# Dokumentnamen und kompakte Dateiansicht

Seit Version 1.5.2 werden Dokumentnamen aus dem digitalen Text der ersten
Seite des gespeicherten Originals ermittelt. Die frühere Zuordnung aus
Webseitenlinks und Linkumgebung wurde durch die Erstseitenprüfung ersetzt.
Das Modell wählt zusammenhängende Titelzeilen aus. Zugehörige Angaben wie
„ab 01/2017“, „bis 12/2016“, Ausgabe oder Stand bleiben erhalten.
Der Dateiname dient als Orientierung und erscheint separat in der Tabelle.

Bei PDFs liest der Textfilter ausschließlich Seite eins. Bei Word, Excel und
weiteren unterstützten Office-Formaten verwendet der Leser die erste nach
LibreOffice-Drucklayout ausgegebene Seite. PowerPoint beginnt mit der ersten
Druckfolie. OCR wird nicht verwendet. ZIP-Dateien und unbekannte Binärdateien
haben keine unterstützte erste Seite. Ohne belegten Titel bleibt ein Gedankenstrich
mit Status im Tooltip stehen.

Der Hintergrunddienst arbeitet die vorhandenen Archivversionen ab und prüft
neue oder geänderte Fassungen automatisch. Bei identischem Hash kann der Titel
der vorherigen Erfassung übernommen werden. Der Titel und sein Prüfstatus
stehen in der jeweiligen Begleitdatei. Der extrahierte Seitentext wird nur
für die Auswahl der Titelzeilen an die konfigurierte lokale KI übergeben.
Die Originaldateien, Downloads und der SHA-256-Vergleich bleiben unverändert.

Der separate Leser startet ohne Netzwerk, Archiv- oder Geheimniszugriff. Die
Anwendung übergibt die geprüften Originalbytes über einen lokalen Unix-Socket.
Temporäre Dateien liegen im Arbeitsspeicher des Lesers.

## Frühere Version 1.5.1

Die Dateisymbole sind halb so breit und hoch wie in 1.4.1/1.5.0.
Die neue Spalte „Dokumentname“ ergänzt den unveränderten Originaldateinamen.
Sie ist sortierbar und Teil der vorhandenen Suche. Lange Namen werden auf zwei
Zeilen begrenzt; der vollständige Name und seine Herkunft stehen im Tooltip.
ZIP-Ordnerstruktur, Downloadnamen und Originaldateien bleiben unverändert.

## Herkunft des Namens

1. Eine eindeutige Linkbeschriftung der beobachteten Quellseite wird übernommen.
   Zusätze wie „(PDF, 19,0 MB)“ entfallen beim Anzeigenamen.
2. Bei allgemeinen Downloadlinks kann das konfigurierte lokale Analysemodell
   den Titel aus dem unmittelbar benachbarten Webseitentext auswählen. Zulässig
   ist nur ein tatsächlich vorhandener Textausschnitt; Ergänzungen werden verworfen.
3. Ohne brauchbare Quellenbezeichnung wird ein vorhandener Titel aus den
   Dateimetadaten, sonst eine lesbare Fassung des Dateinamens verwendet.
   Der Tooltip kennzeichnet die jeweilige Herkunft.

Die KI erhält dabei ausschließlich kleine Ausschnitte der Quellwebseite im
Kontext des jeweiligen Projekts. Dokumentseiten, OCR, PDF-Volltexte und Office-
Zellinhalte werden nicht gelesen oder an die KI geschickt. Eine fehlgeschlagene
Namenszuordnung beeinflusst den Download oder SHA-256-Vergleich nicht.

Die Benennung läuft beim normalen Dokumentinventar mit. Das Dateiverzeichnis
selbst löst keine KI-Aufrufe aus. Für Bestandsdaten lassen sich die Namen einmalig
aus den bereits archivierten Webseiten und ihren Linkinventaren ergänzen, ohne
die Dokumente erneut herunterzuladen.

## Speicherung und Kompatibilität

`artifacts/<run-id>/<snapshot-id>.extraction.json` enthält zusätzlich
`document_name` mit Text, Herkunft, Quellseite und Dokument-URL. Der Name wird nur
bei passender URL und passendem `source_sha256` angezeigt. Identische Dateien
verschiedener Projekte behalten getrennte Namen und Quellenbezüge.
Die vorhandene Aufbewahrung und der Verlaufsexport sichern diese Begleitdatei
bereits mit. Es sind weder neue Tabellen noch eine neue Exportversion nötig.

## Sammlung von Dateien und Hash-Prüfung

Die Sammlung von Links auf HTML-Seiten, ausklappbaren Bereichen und nachgeladenen
Listen bleibt bestehen. Die Hash-Prüfung untersucht den Dokumentkörper nicht;
ausschließlich innerhalb eines PDFs enthaltene Links werden deshalb nicht verfolgt.
Die zusätzliche Namensspalte erweitert den Suchumfang des Monitors nicht.

Bei BIM Berlin bestätigt der erfolgreiche Lauf vom 07.11.2026 insgesamt
60 Originaldateien und vier archivierte HTML-Quellen: zwei Hauptdokumente,
37 Anlagen zum Projekthandbuch, 12 VOB- und neun UVgO-Unterlagen.
Die zusätzlichen Dokumente stammen aus den nachgeladenen Webseitenlisten.
Hier ist keine PDF-Inhaltsanalyse für die vollständige Sammlung erforderlich.

## Prüfungen

Gezielte Tests decken quellengestützte Namen, begrenzte KI-Eingaben, verworfene
erfundene Titel, getrennte Projektzuordnungen, Suche/Sortierung, unveränderte
Originalbytes, archivierte Quellen sowie bestehende Hash-/Import-/Verteiler-
Funktionen ab. Die Browserabnahme umfasst Hell/Dunkel, Schriftgrößen 50–150 %,
die kompakteren Icons und die interaktive Dokumentnamensuche und -sortierung.

224 gezielte Tests bestanden, ohne ausgelassene Tests. Ein zusätzlicher echter
oMLX-Aufruf bestätigt die Namenszuordnung ausschließlich aus Webseitentext.
Bei der Live-Ergänzung wurden alle 395 gespeicherten Dateiversionen benannt:
335 aus Linkbeschriftungen, 57 aus KI-Zuordnung zum Webseitentext und drei aus
vorhandenen Dateimetadaten. Alle 60 BIM-Dokumente besitzen einen quellengestützten
Namen. Die Originalhashes ihrer 55 unterschiedlichen Dateien wurden erneut
verifiziert, ohne Dateien herunterzuladen oder den Änderungsverlauf zu erweitern.
Der BIM-Projektprompt enthält nun die aktuelle Hash-/Metadaten-Regel statt alter
OCR-Anweisungen; seine Navigation zu den vier HTML-Quellen bleibt erhalten.

Die öffentliche HTTPS-Abnahme bestätigt Dokumentnamen, Suche/Sortierung, kompakte
Icons, bestehende Berichtsarchive, Verteilerverwaltung, Anmeldung/Abmeldung und
Brave-WebSocket. Alle fünf Container laufen mit Release 1.5.1 gesund.
Datenbank und bisherige Metadaten-Begleitdateien wurden vor dem Update gesichert.
