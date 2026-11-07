# Cloudflare Tunnel · externer HTTPS-Zugang

Die App verwendet im LAN weiterhin HTTP. HTTPS endet bei Cloudflare.
Der bestehende LAN-Stack erhält eine ausdrücklich freigegebene externe Adresse.

## Portainer

`portainer-lan-stack.yaml` verwenden und zusätzlich setzen:

```dotenv
KIWM_PUBLIC_URL=https://webmonitor.example.org
```

Die Variable wird an App und Ingress übergeben. Erlaubt ist eine HTTPS-Adresse
mit vollständigem Hostnamen, ohne Port, Pfad, Zugangsdaten oder URL-Parameter.
Leerer Wert deaktiviert den externen Hostnamen. Die bestehenden LAN-, Speicher-,
Admin-, KI- und Versandvariablen beibehalten. Das Admin-Passwort bleibt ausschließlich
im App-Container; ein externer Zugriff ohne eingerichteten Admin ist gesperrt.

## Cloudflare

Eine veröffentlichte HTTP-Anwendung mit dem gewünschten Hostnamen anlegen.
Beispiel für die interne Service-URL

```text
http://192.168.50.10:8765
```

In Portainer muss `KIWM_PUBLIC_URL` zum öffentlichen Hostnamen passen.
Den HTTP-Host-Header in Cloudflare nicht auf localhost oder die LAN-IP umschreiben.
Der Connector muss das LAN-Ziel erreichen und aus dem erlaubten LAN-Subnetz
kommen. Es entstehen keine zusätzlichen öffentlichen Browsersteuerungsports.

Dashboard, Downloads und Brave verwenden denselben externen Hostnamen.
Der Browser liegt geschützt unter `/browser/vnc.html`; sein WebSocket unter
`/browser/websockify`. Unangemeldete Browserzugriffe werden zur App-Anmeldung
geführt. Externe Cookies sind `Secure`, `HttpOnly` und `SameSite=Strict`.
LAN-HTTP und der lokale SSH-Tunnel bleiben weiterhin nutzbar.

## Diagnose

Eine Anfrage mit nicht freigegebenem Hostnamen wurde vorher vom LAN-Ingress
mit nginx-Status 444 geschlossen. Cloudflare zeigte daraus einen 502-Fehler.
Mit konfiguriertem externem Hostnamen müssen `/login` und dessen Assets
antworten. Unbekannte Hostnamen und fremde Origins bleiben gesperrt.
Beliebige Forwarded-Header werden nicht zur Freigabe von Hosts verwendet.

Referenz: [Cloudflare Tunnel – Fehlerdiagnose](https://developers.cloudflare.com/tunnel/troubleshooting/).

Auf dem freigegebenen externen Host werden HTTP-GET und -HEAD auf HTTPS
umgeleitet. Ein über HTTP abgesendetes Loginformular startet die Anmeldung
über HTTPS neu. Sein Inhalt wird dabei verworfen.
