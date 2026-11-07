# LAN-Zugang

Beispiel für die LAN-Adresse **http://192.168.50.10:8765/**.
Die App läuft im LAN über HTTP. Externes HTTPS übernimmt später ein Reverse
Proxy. Ein eigenes Loginformular schützt Dashboard, Dateien und Brave.

## Einrichtung

Auf dem Docker-Host die LAN-Werte vorbereiten:

```sh
python3 scripts/lan-setup.py --ip 192.168.50.10 --subnet 192.168.50.0/24
```

In Portainer den bestehenden Stack mit `portainer-lan-stack.yaml` verwenden
und folgende Environment-Werte setzen. Speicher-/KI-/SMTP-Werte erhalten.

| Variable | Beispiel |
|---|---|
| `KIWM_LAN_IP` | `192.168.50.10` |
| `KIWM_LAN_PORT` | `8765` |
| `KIWM_LAN_SUBNET` | `192.168.50.0/24` |
| `KIWM_ADMIN_USERNAME` | `admin` |
| `KIWM_ADMIN_PASSWORD` | Eigenes Passwort ab 12 Zeichen |
| `KIWM_SESSION_HOURS` | `168` |

Details zur Anmeldung: [ANMELDUNG.md](ANMELDUNG.md).
Die frühere Variable `KIWM_LAN_CONFIG` und die nginx-Kennwortdatei werden
nicht mehr verwendet. Admin-Zugangsdaten erhalten ausschließlich App-ENV.

Compose verwendet dieselben Werte aus der privaten `.env`:

```sh
docker compose -f compose.yaml -f compose.lan.yaml up -d
```

Die fünf Images müssen in der konfigurierten Version verfügbar sein.
[Portainer und Release-Images](PORTAINER.md)

## Betrieb

Zugelassen ist ausschließlich das angegebene LAN-Subnetz. Browsersteuerung
9223 und noVNC 6080 sind nicht im LAN freigegeben. Brave nutzt den geschützten
Pfad `/browser/vnc.html` auf derselben Adresse. Die Hilfeseite `/browser`
bleibt erhalten. Die bisherigen SSH-Tunnel auf 8765/8766 funktionieren weiter
und verwenden bei gesetztem Admin-ENV ebenfalls die Anmeldung.

Host- und Origin-Prüfungen bleiben aktiv. Für Cloudflare die externe Adresse
mit `KIWM_PUBLIC_URL` ausdrücklich freigeben: [CLOUDFLARE.md](CLOUDFLARE.md).
Es wird kein lokales HTTPS eingerichtet.

Zum Deaktivieren des LAN-Zugangs im bestehenden Stack wieder
`portainer-stack.yaml` bzw. nur `compose.yaml` verwenden. Vor Verwendung von
`scripts/redeploy.py` dazu die drei LAN-Variablen bewusst entfernen.
