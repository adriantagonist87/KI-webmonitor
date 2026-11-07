# Architektur und Berichtskonsistenz

Die [Bedienungsanleitung](../README.de.md) beschreibt Dateiprüfung und Konfiguration.

## Tatsächlich eingesetzte Software

Die Anwendung verwendet eine eigene Agentenschleife und Werkzeuge auf Basis
von Playwright/CDP, einen persistenten Brave-Browser, FastAPI und SQLite.
Das lokale Modell wird ausschließlich über die konfigurierte oMLX-API
angesprochen. Skyvern ist nicht installiert. Die installierte Abhängigkeit
`browser-use` steuert den aktuellen Browserablauf nicht. Ihre Versionsangabe
bezeichnet keine aktive Nutzung eines fertigen Browser-Use-Workflows.

Berichte werden allgemein aus Markdown und bereinigtem HTML dargestellt.
Es gibt keine eigenen Ergebnislayouts für VDE, IWW oder VdS. Die KI formuliert
die fachlichen Bewertungen und Berichte; technische Quellenvergleiche liefern
die Belege.

## Allgemeine Werkzeuge und Seitenauswahl

Navigation, Aufklappen, begrenzte Sammlungsprüfung, Dokumentdownload,
HTML-Textvergleich, SHA-256-Dateivergleich und Berichtsdarstellung sind wiederverwendbare Werkzeuge.
Seitenauswahl und Navigationsrezepte stehen überwiegend im jeweiligen
Projektauftrag und in gespeicherten Inhaltsbereichen. Sie sind keine globale
Regel, alle anderen Webseiten gleich auszuwerten.

Die Werkzeuge sind dennoch nicht universell: Die datierte Sammlungsprüfung
setzt eine absteigende Liste und erkennbare Datumsfelder voraus. Der spezielle
Karussellsammler erkennt Bootstrap-Strukturen. Allgemeine Textfilter können
bei abweichenden Seitenlayouts einen anderen Inhaltsbereich benötigen.
Diese Annahmen erfordern Regressionstests; ein erfolgreicher Test einer Seite
beweist keine vollständige Unterstützung beliebiger anderer Webseiten.

Tatsächliche Domain-Ausnahmen gibt es für die abgesicherte elektro.net-Anmeldung
und ihren Datenschutz, einen eng begrenzten lesenden GraphQL-Zugriff bei VdS
sowie den Cookieverzicht bei bestimmten öffentlichen Berliner Anlagen. Sie
sind eingegrenzt und keine allgemeine Erlaubnis für fremde Formularaktionen.

## Dokumentpipeline ab 06.11.2026

Dokumentdownload und Inhaltsanalyse sind getrennt: Der aktive Downloadpfad
prüft nur die vollständig übertragenen Bytes und ihre SHA-256-Hashes. Er ruft
keinen PDF-/Office-Textparser und keine OCR auf. Der archivierte Dokumenttext
bleibt leer. Der Sidecar `extraction.json` kennzeichnet den neuen Modus mit
`method: hash_only`, `content_analysis_performed: false` und dem verifizierten
Originalhash. Dateimetadaten sind kein Ersatz für den Bytevergleich.

Ein Befund `Datei geändert` ist an beide Originalversionen und ihre Hashes
gebunden. Er wird technisch revalidiert und benötigt keine semantische
Relevanzbewertung des ungelesenen Dokuments. Die KI beschreibt nur, dass sich
die Datei geändert hat. Ein Wechsel von historischen OCR-/Textextraktionen zum
Hashmodus erzeugt bei identischen Originalbytes keine Änderung.

Die frühere Dokumenttext-/OCR-Pipeline und ihre nicht mehr zutreffenden
Runtime-Tests wurden entfernt. Historische Extraktionsdaten bleiben für das
Archiv erhalten; die Cachebereinigung kennt weiterhin ihre alten Kennungen. Webseite,
Navigation und sichtbare HTML-Artikel werden weiterhin analysiert. Download-,
Domain-, Größen- und Anmeldeschutz bleiben bestehen.

## Ein Befund, mehrere Ansichten

Die gelbe Karte zählt alle noch nicht bestätigten relevanten Änderungen eines
Projekts. Eine erfolgreiche spätere Prüfung ohne neue Änderungen entfernt
diese offenen Befunde nicht. Daher müssen Projektübersicht und Prüfbericht
unterschiedliche Zeiträume deutlich zeigen.

- Ganz oben in der Projektansicht stehen die offenen relevanten Befunde mit
  vorhandener KI-Begründung, Quelle, eigenem Prüfdatum und Link zum Lauf.
- Die Ansicht eines einzelnen Laufs zeigt dessen relevante Befunde ebenfalls
  direkt. Rohdiffs und zusätzliche technische Hinweise bleiben ausklappbar.
- Die Auswertung der letzten Prüfung bleibt separat datiert. Sie ersetzt keine
  weiterhin offenen Befunde älterer Prüfungen.
- Der Ereignisbericht per E-Mail nennt die einzelnen Ereignisse und ihre
  Zeitpunkte. Eine losgelöste Zusammenfassung eines späteren unveränderten Laufs
  wird dort nicht angehängt.
- Die Bericht-KI erhält die aufmerksamkeitsrelevanten Befunde zusätzlich als
  kurze Liste. Ein relevanter Kategorienwechsel muss im sichtbaren Einstieg
  erklärt werden; daraus folgt keine Änderung eines ungelesenen Volltexts.

Diese Darstellung verwendet vorhandene KI-Bewertungen. Sie führt keine neue
VDE-Sonderregel ein und ändert die Projektaufträge nicht. Ob eine
Kategorieverschiebung fachlich relevant ist, bleibt eine Frage des jeweiligen
Auftrags und der belegten Bewertung.

## Frühere Abnahme der Berichtskonsistenz vom 06.11.2026

Anwendungscode und App-Image: `4698389`. Der vollständige isolierte Linux-Lauf
bestand mit **527 Tests in 31 Suiten**, ohne ausgelassene Fälle oder Fehler.
Geprüft wurden insbesondere ältere offene Befunde neben einer späteren
unveränderten Prüfung, Hinweise, bestätigte historische Befunde, HTML-Sicherheit,
mehr als 50 relevante Einträge und der Bestätigungszeitpunkt.

Der widersprüchliche VDE-Bericht wurde mit dem tatsächlichen lokalen
oMLX-Modell aus seinen archivierten Belegen neu erstellt. Es erfolgte dafür
kein neuer Webseitenabruf und keine Änderung der Relevanzentscheidung. Die
ursprüngliche Berichtsfassung bleibt unter
`data/artifacts/afc2efa3f0ff4db1a2aaf71deec41705/report-correction-before.json`
erhalten. Projekt- und Laufansicht wurden anschließend im produktiven Browser
geprüft. Eine HTML-Testmail mit Änderungsgrund und Ereignisdatum wurde über
den lokalen Test-SMTP-Dienst versandt und ihr Eingang bestätigt.

Die Übernahme erfolgte über den vorhandenen Portainer-Stack nach verifizierter
Sicherung `backups/ki-webmonitor-20261106T123659Z.tar.gz`. Alle sieben Dienste
sind gesund; die Automatik ist freigegeben. Alle 14 Projektdefinitionen und
Prompts, Einstellungen, Zugangsdaten, Rohbelege und Nutzerbestätigungen wurden
erhalten. Die Testprotokolle liegen unter
`research/vde-scope-20261106/retained-qa-proofs/`, die produktive Prüfung unter
`research/vde-scope-20261106/production-proof.json`.

Dies ist eine Abnahme der Berichtskonsistenz. Eine vollständige erneute
Liveprüfung sämtlicher 14 Webseiten und ihrer Anmeldungen wurde dabei nicht
behauptet. Die zuvor unvollständigen Prüfungen bei Berliner Feuerwehr und
Stromnetz Berlin – Mittelspannung bleiben separat sichtbar.
