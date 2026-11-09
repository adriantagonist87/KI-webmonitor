# Telegram und SMTP

Beide Versandwege haben eigene Einträge in der Sidebar. Einstellungen lassen
sich in der Oberfläche oder über Portainer-Stackvariablen hinterlegen.
Gespeicherte Webwerte haben Vorrang. Ohne gespeicherten Wert verwendet die App
eine vorhandene Stack-Vorgabe und zeigt den Namen der Variable am bearbeitbaren
Feld an. Auch „Aus“ und ein ausgeschalteter SMTP-Schalter werden gespeichert.
Beim Entfernen eines gespeicherten Passworts oder Tokens gilt wieder ein im
Stack hinterlegter Wert.

## Telegram einrichten

1. Einen Bot über **@BotFather** erstellen und seinen Token übernehmen.
2. Den eigenen Bot in Telegram öffnen und **Starten** drücken. Bei Gruppen oder
   Kanälen muss der Bot Zugriff auf das gewünschte Ziel haben.
3. Unter **Telegram** Bot-Token und **Chat-ID (Empfänger)** eintragen und speichern.
   Telegram benötigt die Ziel-Chat-ID beziehungsweise einen @Kanalnamen; die
   Bot-ID ist keine Empfängeradresse. Die Chat-ID lässt sich beispielsweise aus
   einer Nachricht an den eigenen Bot über die offizielle `getUpdates`-Methode
   ermitteln. Tokens dabei nicht in öffentliche Webseiten oder fremde Bots eintragen.
4. **Verbindung testen** prüft den Bot und den Zielchat, ohne eine Nachricht zu
   senden. **Testnachricht senden** sendet ausdrücklich an dieses Ziel.

Die globale Versandvorgabe steht unter **Telegram** und zusätzlich unter
**Einstellungen**:

| Vorgabe | Wirkung |
| --- | --- |
| Aus · alle Projekte | Kein automatischer Telegram-Versand |
| Nach Projektvorgabe | Nur Projekte mit aktivierter Telegram-Checkbox |
| Ein · alle Projekte | Alle Projekte, unabhängig von ihrer Telegram-Checkbox |

Die Projektvorgabe befindet sich beim Bearbeiten unter **Mitteilungen**.
Automatische Prüfungen und Telegram sind getrennte Schalter: Ein manuell
geprüftes, sonst pausiertes Projekt kann ebenfalls eine Mitteilung erzeugen.

Nach Abschluss einer Prüfung enthält eine Meldung den Projektnamen, die
bestätigten Änderungen, die wesentliche KI-Auswertung und die Quelle.
Fehlgeschlagene oder unvollständige Prüfungen melden zusätzlich ihr technisches
Problem. Unveränderte Seiten, der erste Ausgangsstand sowie als irrelevant oder
unsicher bewertete Hintergrundänderungen erzeugen allein keine Änderungsmeldung.
Die Entscheidung entspricht derselben Priorisierung wie im Dashboard.

Eine Nachricht bleibt innerhalb des Telegram-Limits. Lange Auswertungen werden
gekürzt und verweisen auf die vollständige Auswertung im Webmonitor.
Es werden keine Dokumentdateien, Browserprofile oder Zugangsdaten verschickt.
Auf dem Handy wird der lokale SSH-Tunnel des Windows-PCs nicht automatisch
erreichbar; Quellenlinks führen deshalb auf die überwachte Webseite.

Grundlage: [Offizielle Bot-API](https://core.telegram.org/bots/api#sendmessage),
[Bot-Prüfung](https://core.telegram.org/bots/api#getme),
[Chat-Prüfung](https://core.telegram.org/bots/api#getchat).

## SMTP einrichten

Unter **SMTP-Mailserver** Server, Port, Verschlüsselung, Benutzername, Passwort,
Absender und Empfänger speichern. Typische Kombinationen sind Port 587 mit
STARTTLS oder Port 465 mit TLS/SSL. Zertifikate und Hostname werden geprüft;
Anmeldedaten werden erst nach dem TLS-Aufbau übertragen.

- **Verbindung testen** prüft Verbindung, TLS und gegebenenfalls Anmeldung per
  SMTP-NOOP, ohne eine Mail zu versenden.
- **Testmail senden** sendet eine kleine HTML-Mail mit Textalternative an den
  gespeicherten Empfänger, auch wenn der automatische Versand noch aus ist.
- **Wochenberichte per E-Mail versenden** aktiviert den bestehenden konsolidierten
  Bericht montags um 08:00 Uhr Europe/Berlin. Ohne Aktivierung bleibt er Vorschau.

Änderungen zuerst speichern; die Testknöpfe verwenden gespeicherte beziehungsweise
effektive ENV-Werte. „Vom SMTP-Server angenommen“ bestätigt die Übergabe, nicht
die spätere Zustellung im Posteingang. Der optionale lokale Mailpit-Testmodus
bleibt unabhängig davon verfügbar.

## Portainer-Variablen

Alle Werte sind optional. Sie dienen als Vorgaben für Felder ohne gespeicherten Webwert.

| Variable | Wert |
| --- | --- |
| `KIWM_TELEGRAM_BOT_TOKEN` | Bot-Token, geheim |
| `KIWM_TELEGRAM_CHAT_ID` | Numerische Chat-ID, Gruppen-ID oder @Kanalname |
| `KIWM_TELEGRAM_MODE` | `off`, `projects` oder `all` |
| `KIWM_SMTP_ENABLED` | `true` oder `false` |
| `KIWM_SMTP_HOST` | Hostname oder IPv4-Adresse, ohne URL |
| `KIWM_SMTP_PORT` | Portnummer, beispielsweise `587` |
| `KIWM_SMTP_TLS` | `starttls` oder `ssl` |
| `KIWM_SMTP_USER` | SMTP-Benutzername |
| `KIWM_SMTP_PASSWORD` | SMTP-Passwort, geheim |
| `KIWM_SMTP_FROM` | Absenderadresse |
| `KIWM_SMTP_TO` | Empfängeradresse |

Die Variablen gelangen ausschließlich zu App und Gateway. Das Gateway spricht
die feste HTTPS-Adresse `api.telegram.org` an; es gibt keine frei einstellbare
Bot-API-Proxyadresse und keine zusätzlichen veröffentlichten Ports.
Tokens und Passwörter stehen in `/secrets/credentials.json` oder in privaten
ENV-Werten; sie werden weder als ausgefüllte Passwortfelder zurückgegeben noch
in den normalen Projekt-/Konfigurationsexport übernommen. Ein Export mit
ausdrücklich eingeschlossenen Geheimnissen sowie vollständige Backups sind
entsprechend vertraulich. ENV-Zugangsdaten bleiben für Portainer-Administratoren
sichtbar und gehören nicht ins Git-Repository.

## Nachrichtenausgang und Wiederholungen

Der Ausgang wird in SQLite gespeichert und ist unter **Telegram → Letzte
Mitteilungen** sichtbar. Er erzeugt höchstens eine Mitteilung je abgeschlossenem
Prüflauf. Alte Prüfungen werden beim Update nicht nachträglich versandt.

Nicht zustande gekommene Verbindungen und ausdrücklich abgewiesene temporäre
API-Anfragen werden insgesamt höchstens dreimal versucht. Telegrams
`retry_after` wird berücksichtigt. Bei unterbrochener Übertragung oder Neustart
während des Versands bleibt die Zustellung als **unklar** markiert; es wird
nicht automatisch eine mögliche Doppelmeldung gesendet. Ein Versandfehler
ändert nicht das fachliche Ergebnis einer Webseitenprüfung und startet keine
zusätzliche Webseitenprüfung.

Beim Import alter Exporte bleibt Telegram deaktiviert. Neue Exporte erhalten
die Projektvorgabe und die Webeinstellungen; ohne ausdrückliche Aktivierung wird
die globale Versandvorgabe ebenfalls auf „Aus“ gesetzt. Ausstehende Mitteilungen
werden nicht durch einen Projektimport erneut abgespielt. Vollständige Backups
sichern den SQLite-Ausgang mit. Beim Löschen eines Projekts entfallen seine
zugehörigen Mitteilungen automatisch.
