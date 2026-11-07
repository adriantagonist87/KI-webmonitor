# Release 1.2.0 – Telegram und SMTP

Abnahme am 7. November 2026. Anwendungscode der ausgelieferten Images:
`ec61e15bc3f93eb87e049ee14f6e8fd94c041cf9`. Nachfolgende Dokumentationscommits
ändern diesen getesteten Anwendungscode nicht.

## Verhalten

- Eigene Sidebar-Seiten **Telegram** und **SMTP-Mailserver**.
- Webkonfiguration und Portainer-ENV mit sichtbaren, schreibgeschützten Overrides.
- Telegram-Verbindungstest und ausdrücklich ausgelöste Testnachricht.
- SMTP-Verbindungstest mit TLS/Anmeldung und echte HTML-Testmail mit Textalternative.
- Telegram je Projekt; globale Auswahl Aus / Projektvorgaben / Alle Projekte.
- Fertige wesentliche KI-Auswertung, konkrete Änderungen und Prüfprobleme als Meldung.
- Keine Änderungsmeldung für unveränderte Seiten, Ausgangsstände und irrelevante Hinweise.
- Dauerhafter Nachrichtenausgang; begrenzte Wiederholungen und ehrliche unklare Zustellungen.
- Exportformat 7 mit Rückwärtskompatibilität zu den bisherigen Versionen.
- Fünf Betriebsdienste und unveränderte private Netzwerk-/Portgrenzen.

## Funktionsnachweise

| Prüfung | Ergebnis |
| --- | --- |
| Anwendung aus finalem Release-Image | 608 Tests in 40 Suiten bestanden; keine ausgelassen |
| Administrative Werkzeuge | 65 Tests bestanden |
| Telegram-Konfiguration | ENV-Vorrang, Geheimnisschutz, CSRF und globale/projectweise Vorgaben bestanden |
| Telegram-Sendepfad | Echter App-Ausgang → Gateway-API → simulierte Bot-API erfolgreich |
| Nachrichtenausgang | Deduplizierung, 429/Retry-After, maximale Versuche und Neustartzustand geprüft |
| Meldeauswahl | Änderungen, Fehler und unvollständige Läufe gemeldet; unveränderte/baseline Quellen still |
| SMTP-Verbindung | Echter STARTTLS- und SSL/TLS-Aufbau mit vertrauenswürdigem Testzertifikat und Anmeldung |
| SMTP-Testmails | Zwei Nachrichten vom Testserver empfangen, HTML und Text enthalten |
| Wochenbericht | Zusätzlich ein echter Wochenbericht über TLS beim Testserver eingegangen |
| Grafische Bedienung | Sidebar, gespeicherte Chat-ID, SMTP-Testknöpfe, Projekt-Checkbox und globaler Schalter in Brave bedient |
| Darstellung | Telegram im Darkmode und SMTP im Lightmode visuell geprüft |
| Export/Import | Projektvorgabe und optionale Geheimnisse erhalten; alte Versionen importierbar; Import versendet nichts nachträglich |
| Compose und Portainer | Identische Dateien; Versandvariablen ausschließlich in App und Gateway |
| Image-Auslieferung | Vier Images gespeichert, Archiv geprüft, erneut geladen und Image-IDs abgeglichen |

Die TLS-SMTP-Prüfung lief ausschließlich gegen einen eigenen isolierten
Testserver, mit synthetischen Zugangsdaten und `.test`-Adressen. Der Browser hat
danach ebenfalls Verbindungstest und Testmail per Klick ausgelöst. Kein externer
Empfänger erhielt Testmails. Telegram wurde mit einer simulierten offiziellen
API-Antwort geprüft; ohne die später einzutragenden echten Bot-Zugangsdaten ist
ein realer Telegram-Empfangstest nicht möglich. Fehlende Zugangsdaten werden in
der Oberfläche ausdrücklich als Fehler angezeigt, nicht als Erfolg.

Die Anbieter-Webseiten wurden für diese Erweiterung nicht erneut abgefragt.
Die KI-Inferenz bleibt auf dem vorhandenen lokalen oMLX-Server. Es wurden keine
Dokumenttextanalyse, OCR, Modelle oder neuen produktiven Container ergänzt.

## Installation und Betrieb

Der vorhandene Portainer-Stack auf der LXC wurde auf Version 1.2.0 aktualisiert.
Alle fünf Dienste sind gesund; alle 14 vorhandenen Projekte, Aktivierungen,
Prüftermine und privaten Konfigurationsdateien blieben erhalten. Daten- und
Browserpfade wurden weiterverwendet. Telegram bleibt zunächst ausgeschaltet;
vorhandene SMTP-Werte werden übernommen.

Die konsistente private Sicherung vor dem Update liegt auf der LXC unter
`backups/ki-webmonitor-20261107T000702Z.tar.gz`. Nachweise stehen unter
`research/communications-*`; diese privaten Laufzeitdaten gehören nicht ins Git.
Die vollständige Backup-/Restore-Grundabnahme ist zusätzlich in
[Release 1.1](RELEASE-1.1.md) dokumentiert.

Ein öffentlicher GitHub-/GHCR-Release wurde nicht veröffentlicht. Die Auslieferung
enthält die fertigen lokalen Images, den Quellcode und die Git-Historie.
Einrichtung und Variablennamen: [Telegram und SMTP](KOMMUNIKATION.md).
