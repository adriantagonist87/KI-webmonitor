## Änderungen erkennen und einordnen

- **Normen und Dokumente.** „Offene Änderungen“ und „Letzte Prüfung“ nennen die betroffene Norm mit Kennung, Titel und Katalogstatus. Bei geänderten Downloadadressen ordnet der Monitor gleich bezeichnete Vertragsunterlagen einander zu und benennt die betroffenen Dateien. Unvollständige Abrufe und offene Vergleiche bleiben sichtbar.
- **GitHub-Releases.** Enthält eine stabile Version ausschließlich Fehlerkorrekturen, erscheint unter der Version genau „nur Bugfixes“. Bei gemischten Releases nennt der Bericht die neuen Funktionen in deutschen Stichpunkten. Für Wartungsänderungen und fehlende Release Notes gibt es eigene Meldungen. Bei späteren Prüfungen führt ein Link zurück zum Bericht der zuletzt erkannten Version.
- **Neue Artikel.** Die mitgelieferten Projektvorlagen für elektro.net und IWW PBP melden neue Artikel mit ihrer verlinkten Überschrift. Bereits bekannte Artikel bleiben beim Wechsel der Übersichtsseite als bekannt erfasst.
- **VdS-Kategorien.** Der Monitor kann Publikationen über die aktualisierte Kategoriesuche lesen. Ein unvollständiger Abruf behält den letzten vollständigen Vergleichsstand bei.

## Benachrichtigungen und Bedienung

- **Telegram** zeigt bis zu drei konkrete Änderungsmeldungen, einen Quellenlink und bei längeren Listen die Anzahl weiterer Änderungen. Normbezeichnungen, Dokumenttitel und Artikelüberschriften sind direkt in der Nachricht lesbar.
- **Wochenberichte** berücksichtigen relevante Änderungen im gesamten gewählten Rückschauzeitraum. Ein Bericht für einen einzelnen Verteiler lässt sich auch zwischen zwei geplanten Versandterminen auslösen. In der internen Übersicht sind Tage ohne erfolgreiche Prüfung sichtbar.
- **Dashboard.** „Alle Projekte aktualisieren“ startet nach Bestätigung eine einmalige Prüfung einschließlich pausierter Projekte. Bereits wartende oder laufende Projekte werden dabei nur einmal eingeplant.

## Download und Installation

Der Quellcode steht als ZIP bereit. Für Docker Compose, Dockhand und Portainer sind fünf Container-Images für Linux AMD64 vorgesehen. Die feste Version lautet `1.5.20`, der Image-Präfix lautet `ghcr.io/adriantagonist87/ai-webmonitor`. [Quellcode herunterladen](https://github.com/adriantagonist87/KI-webmonitor/archive/refs/tags/v1.5.20.zip) · [Docker Compose](https://github.com/adriantagonist87/KI-webmonitor/blob/v1.5.20/docs/COMPOSE.md) · [Dockhand](https://github.com/adriantagonist87/KI-webmonitor/blob/v1.5.20/docs/DOCKHAND.md) · [Portainer](https://github.com/adriantagonist87/KI-webmonitor/blob/v1.5.20/docs/PORTAINER.md)

Für vorhandene Installationen vor dem Update die Datenvolumes sichern und laufende Prüfungen abschließen lassen. Eigene Projektaufträge lassen sich im Projekteditor an die aktualisierten Vorlagen anpassen. [Vollständiger Vergleich mit v1.5.19](https://github.com/adriantagonist87/KI-webmonitor/compare/v1.5.19...v1.5.20)
