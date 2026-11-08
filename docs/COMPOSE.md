# Installation mit Docker Compose

Die Befehle laufen auf einem Linux-Docker-Host mit `amd64`, Docker Engine, dem Compose-Plugin und Git. Mit `docker compose version` lässt sich prüfen, ob das Plugin verfügbar ist. [Installation des Compose-Plugins](https://docs.docker.com/compose/install/linux/)

## Projekt und `.env` vorbereiten

```sh
git clone --branch v1.5.17 --depth 1 https://github.com/adriantagonist87/KI-webmonitor.git ki-webmonitor
cd ki-webmonitor
umask 077
cp .env.example .env
chmod 600 .env
```

In der privaten `.env` die folgenden Einträge setzen. Für das Passwort einen eigenen Wert mit mindestens zwölf Zeichen eintragen. Einfache Anführungszeichen halten Zeichen wie `$` im Passwort unverändert.

```dotenv
IMAGE_PREFIX=ghcr.io/adriantagonist87/ai-webmonitor
VERSION=1.5.17
KIWM_ADMIN_USERNAME=admin
KIWM_ADMIN_PASSWORD=''
```

Die oMLX-Adresse, den API-Token und das Modell nach dem Start in der Weboberfläche eintragen oder vorab `KIWM_OMLX_URL`, `KIWM_OMLX_API_KEY` und `KIWM_MODEL` in `.env` setzen. Nichtleere Werte aus `.env` haben Vorrang vor den Webeinstellungen. [Variablen in Docker Compose](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)

## Starten

```sh
docker compose config --quiet
docker compose pull
docker compose up -d --wait
docker compose ps
```

Docker lädt die fünf veröffentlichten Images und startet sechs Dienste. Die Volumes für Daten, Konfiguration, Zugangsdaten und Browserprofil legt Compose beim ersten Start an.

## Zugriff per SSH-Tunnel

Der Standardstack bindet die App und Brave an `127.0.0.1` des Docker-Hosts. Auf dem eigenen Rechner einen Tunnel öffnen und während der Nutzung laufen lassen. `BENUTZER` und `DOCKER-HOST` durch den SSH-Benutzer und den Hostnamen oder die IP-Adresse ersetzen.

```sh
ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 -L 127.0.0.1:8766:127.0.0.1:8766 BENUTZER@DOCKER-HOST
```

Im Browser `http://localhost:8765/` öffnen, mit den Admin-Daten aus `.env` anmelden und unter **Einstellungen** die KI-Verbindung testen.

## Alternativ im LAN starten

In `.env` zusätzlich die private IPv4-Adresse des Docker-Hosts und das erlaubte Netz eintragen. Das Beispiel an das eigene LAN anpassen. Für den LAN-Zugriff müssen Adminname und Passwort gesetzt sein.

```dotenv
KIWM_LAN_IP=192.168.50.10
KIWM_LAN_SUBNET=192.168.50.0/24
KIWM_LAN_PORT=8765
```

```sh
docker compose -f compose.yaml -f compose.lan.yaml config --quiet
docker compose -f compose.yaml -f compose.lan.yaml pull
docker compose -f compose.yaml -f compose.lan.yaml up -d --wait
docker compose -f compose.yaml -f compose.lan.yaml ps
```

Die App öffnen, im Beispiel unter `http://192.168.50.10:8765/`. Für weitere Compose-Befehle dieselben beiden Dateien angeben. [Details zum LAN-Zugang](LAN-ZUGANG.md)

## Aktualisieren und beenden

Vor einem Update die Daten sichern. Im selben Projektverzeichnis `git fetch --tags` ausführen und mit `git switch --detach vX.Y.Z` den gewünschten Release-Tag auswählen. `VERSION` in der vorhandenen `.env` auf den passenden Image-Tag ohne `v` setzen und die Startbefehle für die gewählte Zugriffsart wiederholen. Compose verwendet die bestehenden Volumes weiter.

`docker compose stop` hält den Stack an. `docker compose down` entfernt die Container und Stack-Netze und bewahrt die benannten Daten-Volumes auf. [Sicherung, Wiederherstellung und Deinstallation](OPERATIONS.md)
