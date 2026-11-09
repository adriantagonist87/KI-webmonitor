## Neu

- **Geschützter Export und Import.** Gespeicherte Webseitenzugänge und App-Zugangsdaten können verschlüsselt in JSON- und ZIP-Exporte aufgenommen werden. Beim Import lassen sich Projekt- und App-Zugänge getrennt übernehmen. Für geschützte Pakete muss auch die Zielinstallation dieses Exportformat unterstützen.
- **Wartung & Bereinigung.** Die Sidebar bündelt Speicherübersicht, belegten und freien Speicher sowie eine sortierbare Aufschlüsselung je Projekt. Originaldateien, Prüfverläufe, Protokolle und Berichtsarchive lassen sich gezielt oder global mit Vorschau bereinigen. Das Zurücksetzen auf eine Erstauswertung hat eine eigene Bestätigung.
- **Flexibler SMTP-Berichtszeitplan.** Uhrzeit, Wiederholung und Berichtszeitraum sind in der Weboberfläche konfigurierbar. Berichte können täglich, werktags, wöchentlich, monatlich oder in Stundenintervallen erstellt werden.
- **Browser-Makros je Projekt.** Navigationsschritte können im integrierten Brave aufgenommen, gespeichert und für spätere Prüfungen wiederverwendet werden. Die VDE-Normenbibliothek verwendet gezieltere Navigationsschritte und kann ihre Browsersitzung über andere Prüfungen hinweg wiederverwenden.

## Verbessert und korrigiert

- Dashboard-Filter lassen sich durch erneutes Anklicken zurücksetzen. Laufende Prüfungen stehen in der ungefilterten Ansicht oben. Offene Änderungen sind auf den Projektkarten markiert und lassen sich dort quittieren.
- Gespeicherte Web-Einstellungen für Telegram und SMTP haben Vorrang vor Stack-Vorgaben. Telegram-Auswertungen behalten ihre Absätze und Listen.
- Die E-Mail-Vorlage zeigt relevante Änderungen in einer kompakten Tabelle und gliedert die Quellen nach Änderungsstatus. Sie verwendet das neue lesende Maskottchen und verweist auf die lesbaren Originalseiten und überwachten Websites. Hinweise, Webmonitor-Links und direkte Dateidownloads entfallen im E-Mail-Bericht.
- Das globale Dateiverzeichnis zeigt Dateigrößen in MB. Erkannte Downloadlinks mit Leerzeichen im Pfad bleiben nutzbar. Die Desktop-Anmeldemaske ist kompakter und verwendet das neue Maskottchen mit transparentem Hintergrund.

## Installation und Update

Die fünf Images für Linux amd64 liegen unter `ghcr.io/adriantagonist87/ai-webmonitor-*`. App und Gateway verwenden dasselbe App-Image. Für einen reproduzierbaren Stand `VERSION=1.5.19` verwenden. Der Registry-Tag `latest` folgt dem neuesten veröffentlichten stabilen GitHub-Release, sobald die Aktion für alle fünf Images abgeschlossen ist.

Für `latest` in Dockhand `IMAGE_PREFIX=ghcr.io/adriantagonist87/ai-webmonitor`, `VERSION=latest` und **Re-pull images** setzen. [Dockhand-Anleitung](https://github.com/adriantagonist87/KI-webmonitor/blob/v1.5.19/docs/DOCKHAND.md)

[Änderungen seit v1.5.18](https://github.com/adriantagonist87/KI-webmonitor/compare/v1.5.18...v1.5.19)
