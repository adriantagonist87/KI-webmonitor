# Darstellung 1.4.1

## Dateisymbole

Die Dateiliste zeigt farbige Blattsymbole mit großer Dateiendung. PDF ist rot,
Word blau, Tabellen grün, Präsentationen orange. Weitere Formate und unbekannte
Dateien erhalten ebenfalls farbige Symbole. Dateityp, Dateiname, Metadaten,
Sortierung, Filter, Download und ZIP-Export funktionieren wie bisher.

Verwendet wird die lokal mitgelieferte CSS-Bibliothek Drawbotics/file-icons,
Commit `1bf12626ca659666277c1748621d355cda7354ad`, MIT-Lizenz. Es gibt keine
CDN-Abhängigkeit, externen Fonts, React- oder NPM-Laufzeit. Original-CSS,
Lizenz und Herkunft liegen in `app/static/vendor/file-icons/`. Die Größen,
zusätzlichen Typfarben und Anpassungen für die Dateiliste stehen in `ui.css`.
Die Symbole sind in heller und dunkler Darstellung sowie bei 50–150 % geprüft.

## Animationen

Die Anwendung startet mit eingeschalteten Animationen. Die explizite Einstellung
über den Animationsschalter wird im jeweiligen Browser gespeichert und gilt
unabhängig von Windows-/Browser-Vorgaben für reduzierte Bewegung. Die bisherige
automatische Abschaltung durch `prefers-reduced-motion` wurde im Dashboard entfernt.
Auch Fehler-/Hinweisblinken und Würfelanimationen folgen derselben App-Einstellung.
„Animationen aus“ beendet weiterhin Übergänge, Hover-Zoom, Blinken und Klickwellen.
Ist der lokale Browserspeicher gesperrt, bleiben Umschalten und Bedienung in der
aktuellen Seite möglich; beim erneuten Öffnen gilt ohne Speicher wieder der Standard.

## Nachweise

95 gezielte Prüfungen in der isolierten Brave-Umgebung bestanden, keine ausgelassen:
Dateiliste, alle unterstützten Dateiendungen, Dashboard-Steuerung, Oberflächen- und
Icon-Regressionstests. Zusätzlich bestanden sechs Prüfungen in einem grafischen
Firefox unter Xvfb. Geprüft wurden reduzierte Bewegung, Wechsel der Systemvorgabe,
ein/aus und gespeicherte Wahl, gesperrter Browserspeicher, alle Typen/Farben und
Zoomstufen. Die Testdaten und Browserprofile sind vom produktiven Bestand getrennt.
Hell-/Dunkel-Screenshots wurden visuell kontrolliert.

Der grafische Firefox ist ausschließlich Testzubehör. Produktiv bleibt Brave im
bestehenden Stack; es wurden keine Windows-/Firefox-Einstellungen des Nutzers und
keine Sicherheitsvorgaben der LXC verändert.
