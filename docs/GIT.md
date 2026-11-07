# Git und private Daten

Das Repository enthält den Quellcode, Containerdefinitionen, sichere Vorlagen,
Prompts und technische Dokumentation. Es enthält keine produktive Konfiguration,
Browserprofile, Zugangsdaten, Datenbank, Dokumentenarchive oder Sicherungen.
Ein externer Git-Dienst ist für den Betrieb nicht erforderlich. Es wurde kein
öffentliches Repository und kein externer Remote angelegt.

| Zweck | Verfahren |
| --- | --- |
| Änderungen am Programm nachvollziehen | Lokales Git-Repository |
| Projekte, Prompts und Einstellungen übertragen | Export/Import in der App; Exportdatei vertraulich behandeln |
| Betrieb einschließlich Verlauf und Browseranmeldungen wiederherstellen | Vollständige private Sicherung gemäß README |

Ein Git-Checkout allein stellt keine vollständige laufende Installation wieder her.
Insbesondere müssen die privaten Laufzeitverzeichnisse aus einer Sicherung
wiederhergestellt oder über die Installation neu angelegt werden.

## Vor jedem Commit

Die Befehle im Projektverzeichnis ausführen:

```sh
git status --short
git add -A
git diff --cached --stat
python3 scripts/git_guard.py
git diff --cached
git commit -m "Beschreibung der Änderung"
```

Unter Windows kann `python` statt `python3` verwendet werden. Den Diff lokal
prüfen; seinen Inhalt nicht ungeprüft in Chats oder Tickets kopieren. Der Guard
liest alle im Index vorgemerkten Dateien, auch wenn die Arbeitskopie bereits
geändert wurde. Er blockiert private Laufzeitpfade, Schlüsseldateien, Archive,
Datenbanken, Symlinks, Submodule, große Dateien und einige bekannte Tokenmuster.
Fundmeldungen nennen nur Dateiname und Regel, keine erkannten Geheimnisse.
Die vorhandenen URL-Sicherheitstests verwenden wenige ausdrücklich im Guard
aufgeführte Platzhalter wie `user:password` auf `example.org`. Nur diese exakten
Werte sind zugelassen; Testdateien insgesamt werden nicht ausgenommen.

Diese Regeln sind eine zusätzliche Sicherung. Frei gewählte Passwörter ohne
bekanntes Muster kann der Guard nicht allgemein erkennen. Auch Texte in Prompts,
Screenshots, Statusnotizen oder Dokumentationsdateien müssen daher vor dem Commit
inhaltlich geprüft werden. Produktive Daten niemals mit `git add -f` hinzufügen.

Der lokale Commit-Hook lässt sich einmalig aktivieren:

```sh
git config --local core.hooksPath .githooks
```

Hooks werden von Git nicht automatisch bei einem neuen Checkout aktiviert. Nach
einem neuen Checkout den obigen Befehl erneut ausführen und den Guard manuell
testen. Der Hook benötigt Git und Python 3; er verändert keine globale
Git-Konfiguration. Die Tests sind isoliert und benötigen keine Verbindung zur LXC:

Wenn Git Bash unter Windows bei `python3` nur den Windows-Store-Alias findet,
den installierten Interpreter für den aktuellen PowerShell-Prozess ausdrücklich
setzen. Dabei den tatsächlichen absoluten Pfad zur eigenen Python-Installation
eintragen; der Hook führt ihn mit sicherer Pfadquotierung aus:

```powershell
$env:KIWM_PYTHON = 'C:\Pfad\zur\Python-Installation\python.exe'
git commit -m "Beschreibung der Änderung"
```

Diese Prozessvariable gehört nicht in die Docker-`.env` und umgeht den Guard
nicht. Ohne `KIWM_PYTHON` versucht der Hook `python3`, anschließend `python`.
Die eigenständigen Guardtests ausführen mit:

```sh
python3 scripts/git_guard_checks.py
```

## Erstmalige lokale Versionierung

Nur wenn das Projekt noch kein eigenes Repository enthält:

```sh
git init -b main
git config --local user.name "Lokaler Betreiber"
git config --local user.email "lokal@localhost"
git config --local core.hooksPath .githooks
```

Danach den Ablauf „Vor jedem Commit“ verwenden. Keinen bestehenden Git-Verlauf
zurücksetzen oder neu initialisieren. Name und E-Mail gelten ausschließlich für
dieses Repository und können später lokal geändert werden.

`.gitattributes` sorgt für Linux-Zeilenenden bei Shellskripten und für
Windows-Zeilenenden bei CMD-Dateien. Beim Übertragen auf Linux die ausführbaren
Rechte der Shellskripte beibehalten; Compose kann sie sonst gegebenenfalls nicht
starten. Die Installationsanleitung nennt die benötigten Rechte.

## Private Dateien und SSH

Die `.gitignore` schließt insbesondere `data/`, `secrets/`, `backups/`, `research/`,
`.env`, `config/settings.json` und `scripts/known_hosts` aus. `config/nginx.conf`
mit seiner `nginx.conf.template` und ausdrücklich als `.example` bezeichnete Vorlagen dürfen versioniert werden.
Private Einstellungen gehören in die App oder in die dokumentierte lokale
Konfiguration, Zugangsdaten ausschließlich in die private Ablage.

Für den Windows-SSH-Starthelfer muss `scripts/known_hosts` lokal vorhanden sein.
`scripts/known_hosts.example` erklärt das Format. Start mit
`scripts\open-webmonitor.cmd benutzer@host`. Den Fingerabdruck der LXC über
einen bereits vertrauenswürdigen Zugang prüfen und anschließend den bestätigten
Hostschlüssel übernehmen. `StrictHostKeyChecking=yes` beibehalten. Die Ausgabe
von `ssh-keyscan` allein bestätigt die Identität des Servers nicht.

## Updates und Rückkehr zu einer Version

Vor einem Programmupdate eine vollständige Sicherung erstellen und aktive
Prüfungen beenden lassen. Den gewünschten Git-Stand auschecken und anschließend
die in der README beschriebene Build-/Stack-Prozedur ausführen. Eine einzelne
Installation über genau einen Weg verwalten: entweder Portainer oder die
dokumentierte Compose-CLI-Prozedur. Die Änderung des Quellcodes aktualisiert
laufende Container noch nicht.

Ein älterer Git-Stand setzt weder Datenbank noch Einstellungen zurück. Nach einer
Änderung des Datenformats können alte Programmversionen inkompatibel sein; für
eine vollständige Rückkehr die passende private Sicherung wiederherstellen.
Kein `git clean -fdx` im Installationsverzeichnis ausführen: Dadurch würden auch
ignorierte Browserprofile, Zugangsdaten und Datenbanken gelöscht.

Bei versehentlich eingecheckten Zugangsdaten den betroffenen Schlüssel bzw. das
Passwort ersetzen. Das Löschen in einem späteren Commit entfernt die alte
Fassung nicht aus dem Git-Verlauf. Eine Bereinigung des Verlaufs gesondert planen;
keinen Verlauf ungeprüft veröffentlichen oder überschreiben.
