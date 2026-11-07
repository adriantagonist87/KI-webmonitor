# Visuelle Browserbedienung (Erprobung)

Unter **Projekt bearbeiten → Browserbedienung** lässt sich ein Projekt auf
**Visuell · Maus und Tastatur** umstellen. Standard bleibt für bestehende und
neue Projekte unverändert. Optional kann eine HTTPS-Hauptseite als Einstieg
angegeben werden; ihr Hostname muss im Projekt erlaubt sein. Die Projekt-URL
bleibt das Ziel der Analyse und die Identität des bisherigen Archivs.

## Ablauf

- Jede Prüfung öffnet die Einstiegsadresse einmal frisch über Braves Adresszeile.
  Ein vorhandener passender Tab und seine Sitzung werden dabei weiterverwendet;
  eine alte, noch geöffnete Seite gilt nicht als erneute Prüfung.
  Weiterleitungen laufen wie bei der manuellen Bedienung ab. Es wird keine
  separate SSO-Adresse aufgerufen und kein zusätzlicher Login-Tab geöffnet.
- Der lokale Anmeldecontroller prüft Herkunft, Formular, Felder und Absendeziel.
  Er trägt die geschützten Zugangsdaten mit echten Tastaturereignissen ein und
  klickt einmal auf den Anmeldeknopf. Währenddessen gibt es keine Modellanfrage
  oder Bildschirmaufnahme. Ein fehlgeschlagener Login wird nicht mehrfach versucht.
- Die KI wählt anhand des Screenshots einen sichtbaren Navigations- oder
  Hinweisknopf. Bei beschrifteten Knöpfen bestimmt der Controller dessen genaue
  Position; er bewegt die echte Maus dorthin und klickt. Optionale Bildkoordinaten
  werden gegen das sichtbare Ziel geprüft. Scrollen erfolgt mit dem Mausrad.
- Inhalte, Linkziele und Vergleichsnachweise werden weiterhin technisch aus der
  geladenen Seite gelesen. Die Bedienung ist kein reines OCR-System. Originaldateien
  werden weiterhin nur per SHA-256 und Metadaten geprüft.
- Bei datierten Listen nennt die KI nur den ausgewählten Filter und die sichtbare
  Datumsbeschriftung. Der Controller ermittelt wiederholte Einträge, verlinkte
  Überschriften und den zugehörigen Scrollbereich aus der geladenen Seite. Er
  verwendet dafür beschriftete Datumsfelder (`dt`/`dd`) und verlangt eine eindeutige
  Zuordnung. Nicht unterstützte oder mehrdeutige Listen führen zu einer benannten
  Prüflücke, nicht zu erfundenen Selektoren oder einer behaupteten Vollständigkeit.

Der vorhandene private noVNC-Kanal trägt die Eingaben. Es gibt dafür weder einen
zusätzlichen öffentlichen Port noch einen Docker-Socket, Shellzugriff oder ein
neues privilegiertes Browserprogramm. Brave Shields bleibt eingeschaltet.

## Grenzen und Sicherheit

Der visuelle Modus lässt den normalen Browser-Netzwerkablauf einschließlich
Cache und JavaScript-Anfragen bestehen. Die HTTP-Methodenfilter des
Standardmodus werden in diesem Modus nicht eingesetzt. Die vorhandene
Netzwerkisolation des Stacks bleibt bestehen. Die Eingabesteuerung erlaubt nur
geprüfte Navigation, Hinweisquittierung und den konfigurierten Login; Käufe,
Kontoeinstellungen und beliebige Modell-Tastatureingaben sind nicht freigegeben.

Automatischer Login unterstützt zunächst den allgemeinen Webseitenzugang mit
Benutzername und Passwort. Zweifaktor-Anmeldung oder CAPTCHA können weiterhin
manuelles Eingreifen erfordern. Bot-Schutz beendet die aktuelle Prüfung und
unterbindet deren zusätzliche Wiederholung. Das Projekt bleibt für den nächsten
regulären Zyklus aktiviert. Es werden keine CAPTCHAs gelöst oder Schutzmechanismen
umgangen. Ein nativer Klick garantiert nicht, dass ein Anbieter Automatisierung erlaubt.

Der Browser muss mit normalem Desktopzoom betrieben werden. Nicht unterstützte
Fenstergeometrie führt zu einem Fehler statt zu unkontrollierten Klicks. Während
einer Analyse nicht gleichzeitig im gemeinsamen Brave klicken oder tippen.

## Cookies und Passwörter

Cookies und Browserprofile bleiben auf dem persistenten Volume erhalten.
Der Anbieter entscheidet, wann eine Sitzung abläuft. Das Speichern eines
Passworts verlängert sie nicht. Der Brave-Passwortmanager bleibt deaktiviert;
für automatische Anmeldungen wird der geschützte Webseitenzugang der App verwendet.
Zugangsdaten gehören niemals in Projekt-Prompts.

## Zurückwechseln und Übertragen

Im Projekt wieder **Standard** auswählen. Prompts, Webseitenzugänge und Archive
werden dadurch nicht entfernt. Der JSON-/ZIP-Export enthält den gewählten Modus
und die Einstiegsadresse. Alte Exporte ohne diese Felder behalten beim Reimport
die vorhandene Einstellung. Neue importierte Projekte bleiben standardmäßig
im Standardmodus, sofern kein visueller Modus ausdrücklich exportiert wurde.

## Tests

`visual_browser_checks.py` verwendet ausschließlich künstliche Webseiten und
Zugangsdaten in einem separaten Browser. Ausführung nur mit `KIWM_NATIVE_TEST=1`
und `BROWSER_CDP_URL` des isolierten QA-Browsers. Der Test darf nicht mit dem
produktiven Browserprofil ausgeführt werden. `visual_config_checks.py` prüft
den Export/Reimport mit einer temporären Datenbank.
