# Installation mit Dockhand

[Dockhand](https://github.com/Finsys/dockhand) verwaltet den KI-Webmonitor als Docker-Compose-Stack. Die Anleitung setzt voraus, dass Dockhand läuft und der Linux-Docker-Host mit `amd64` als Environment verbunden ist. Die Images für `1.5.16` sind öffentlich abrufbar.

## Stack aus Git anlegen

1. In Dockhand das gewünschte Docker-Environment auswählen. Unter **Stacks → New stack → Git** einen Stack namens `ki-webmonitor` anlegen.
2. `https://github.com/adriantagonist87/KI-webmonitor.git` als Repository hinzufügen oder auswählen. Für das öffentliche Repository sind keine Git-Zugangsdaten nötig.
3. Als **Branch** `main` wählen. Bei **Compose file path** `compose.yaml` für Zugriff per SSH-Tunnel oder `portainer-lan-stack.yaml` für Zugriff im LAN angeben. Beide liegen im Hauptverzeichnis des Repositorys. **Context directory** und **Additional env file** leer lassen.
4. Im Bereich **Environment variables** die unten genannten Werte setzen. Für einen ersten Start kann **Auto-sync** aus bleiben. Die fertigen Images werden bei Bedarf heruntergeladen. **Build images on deploy** bleibt aus.
5. Den Stack speichern und **Deploy** ausführen. Nach dem Start sollten alle sechs Dienste im Stack laufen.

Die Git-Optionen und die Variablenverwaltung sind im [Dockhand-Handbuch](https://dockhand.pro/manual/#git-integration) beschrieben.

## Stack-Variablen

| Name | Wert |
| --- | --- |
| `IMAGE_PREFIX` | `ghcr.io/adriantagonist87/ai-webmonitor` |
| `VERSION` | `1.5.16` |
| `KIWM_ADMIN_USERNAME` | Eigener Adminname |
| `KIWM_ADMIN_PASSWORD` | Eigenes Passwort mit mindestens zwölf Zeichen. In Dockhand als **Secret** markieren. |
| `KIWM_LAN_IP` | Nur für den LAN-Stack. Private IPv4-Adresse des Ziel-Docker-Hosts. |
| `KIWM_LAN_SUBNET` | Nur für den LAN-Stack. Erlaubtes privates Subnetz, zum Beispiel `192.168.50.0/24`. |
| `KIWM_LAN_PORT` | Optional für den LAN-Stack. Standard ist `8765`. |

Die Compose-Dateien übernehmen die Variablen aus dem Dockhand-Editor. Die oMLX-Verbindung lässt sich nach der Anmeldung in der Weboberfläche hinterlegen. Alternativ können `KIWM_OMLX_URL`, `KIWM_OMLX_API_KEY` und `KIWM_MODEL` als Stack-Variablen gesetzt werden. Einen API-Token ebenfalls als **Secret** markieren. Nichtleere Stack-Werte haben Vorrang vor den Webeinstellungen. [Weitere Variablen](../.env.example)

Im LAN ist die App unter `http://<DOCKER-HOST>:8765/` erreichbar. Beim Standardstack die [SSH-Portweiterleitung](COMPOSE.md#zugriff-per-ssh-tunnel) öffnen und anschließend `http://localhost:8765/` aufrufen. Die Zieladresse ist der Docker-Host, auf dem der Stack läuft.

## Lokaler Stack im Editor

Für eine Installation mit einem festen Stand der Compose-Datei in **Stacks → New stack** den YAML-Editor öffnen und die Datei aus dem Release-Tag einfügen. Für SSH gilt [compose.yaml](https://github.com/adriantagonist87/KI-webmonitor/blob/v1.5.16/compose.yaml), für das LAN [portainer-lan-stack.yaml](https://github.com/adriantagonist87/KI-webmonitor/blob/v1.5.16/portainer-lan-stack.yaml). Die Variablen wie oben eintragen, **Deploy immediately** aktivieren und den Stack anlegen.

## Aktualisieren

Vor einem Update die Daten sichern. Für den Git-Stack die Änderungen an der Compose-Datei prüfen, `VERSION` auf den gewünschten veröffentlichten Image-Tag setzen und den vorhandenen Stack erneut deployen. Beim lokalen Stack zusätzlich den YAML-Inhalt aus dem dazugehörigen Release-Tag übernehmen. Der gleiche Stackname und dieselben Speicherzuordnungen sorgen dafür, dass Docker die bestehenden Volumes wieder einbindet. [Sicherung und Wiederherstellung](OPERATIONS.md)
