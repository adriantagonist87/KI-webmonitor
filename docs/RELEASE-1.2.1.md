# Release 1.2.1 – Dashboard-Bedienung

## Änderungen

- Der Fehlerfilter blendet bei mindestens einem Fehler seinen roten Hintergrund
  weich im Sekundentakt ein und aus. Text und Zähler bleiben lesbar. Bei
  ausgeschalteten Animationen oder reduzierter Bewegung bleibt er statisch rot.
- Der Automatik-Button steht weiterhin rechts unten und entspricht der Größe
  der anderen Kartenbuttons. Play bedeutet aktiv; ein Klick pausiert die
  Zeitsteuerung. Pause mit gelbem Hintergrund bedeutet pausiert; ein weiterer
  Klick aktiviert sie. Der Schalter funktioniert auch ohne JavaScript per Formular.
- Der neue Filter **Pausiert** zeigt deaktivierte Projekte. Sobald die zur Suche
  und Tagwahl passenden pausierten Projekte vorhanden sind, ist er gelb.
- Hinweise stehen violett hervorgehoben neben dem letzten Prüfzeitpunkt.
  Die Zeilenhöhe ist reserviert, damit das Erscheinen von Hinweisen die Karte
  nicht vergrößert. In schmalen Karten bleiben Symbol und Anzahl sichtbar;
  die vollständige Bedeutung ist als Beschriftung und Tooltip verfügbar.

## Zeitsteuerung

Der Server setzt einen gewünschten Zustand, anstatt ihn unkontrolliert zu
invertieren. Ein wiederholtes Senden desselben Zustands ist idempotent. Beim
Pausieren entfallen wartende automatische Prüfungen und geplante Wiederholungen.
Laufende Analysen und manuell gestartete Prüfungen werden nicht abgebrochen.
Beim Aktivieren wird der nächste zukünftige Termin anhand des bestehenden
Zeitplans berechnet; verpasste Termine werden nicht nachgeholt.

Scheduler und Startfreigabe prüfen den Aktivierungsstatus nochmals innerhalb
ihrer Datenbanktransaktion. Eine ältere Statusantwort kann einen gerade
ausgeführten Schaltvorgang nicht in der Oberfläche zurücksetzen. Bei einem
Verbindungsfehler bleiben der letzte bestätigte Zustand und eine Fehlermeldung
sichtbar. Prompts, Tags, Farben und Anmeldeprofile werden durch den Schalter
nicht bearbeitet.

## Abnahme am 7. November 2026

192 gezielte Anwendungstests in zehn Suiten bestanden, ohne ausgelassene Tests.
Dazu kommen 65 Prüfungen der administrativen Werkzeuge und die Compose-Prüfung.
Die vollständige Basisabnahme mit 608 Tests gehört zu
[Release 1.2.0](RELEASE-1.2.md); sie wurde für diesen UI-Patch nicht erneut komplett
ausgeführt.

Geprüft wurden insbesondere:

- Tatsächliche Klicks im isolierten Brave auf Pause und Aktivieren.
- Play-/Pause-Symbol, Zähler und Farbwechsel ohne Seitenneuladen.
- Pausenfilter zusammen mit Suche; Fehlerfade und ausbleibende Animation bei null Fehlern.
- Reduzierte Bewegung und ausgeschaltete Animationen.
- Unveränderte Kartenhöhe mit und ohne Hinweis bei 50, 100 und 150 Prozent Schriftgröße.
- Gleich ausgerichteter Prüfzeitpunkt und Hinweischip sowie Light-/Darkmode-Screenshots.
- Abgewiesene CSRF-/Fremdursprungsanfragen und ungültige Zustandswerte.
- Weiterlaufende Analysen, verworfene Warteschlangen und unterbundene Scheduler-Rennen.
- Idempotente POSTs, fehlgeschlagene Schaltversuche und verspätete Statusantworten.
- Bestehende Projektverwaltung, Wiederholungen, Export und Telegram-/SMTP-Konfiguration.

Die Tests verwendeten synthetische Daten und einen eigenen Browser. Es wurden
keine Anbieter-Webseiten abgefragt und keine vorhandenen Projekte für Tests
pausiert. Der Portainer-Stack wurde aktualisiert; private Konfiguration,
Projektdefinitionen, Aktivierungen und Zeitpläne blieben bei der Installation
erhalten. Die konsistente Sicherung davor liegt auf der LXC unter
`backups/ki-webmonitor-20261107T003811Z.tar.gz`.

Anwendungscode des getesteten Images: `fc954c1`.
Ein öffentlicher GitHub-/Registry-Release wurde nicht ausgeführt.
