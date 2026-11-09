# Verteilerlisten und Empfängerberichte

## Bedienung

1. In der Sidebar „Verteilerlisten“ öffnen und „Neuer Verteiler“ wählen.
2. Alias und E-Mail-Adressen eingeben, eine Adresse pro Zeile.
3. Tags auswählen und bei Bedarf einzelne Projekte ergänzen.
4. Den wöchentlichen Versand aktivieren und speichern.
5. Über „Vorschau“ den gefilterten Bericht prüfen.

Die Auswahl ist eine Vereinigung: Tag-Auswahl plus einzeln gewählte Projekte.
Neu hinzukommende Projekte mit dem zugewiesenen Tag werden künftig automatisch
eingeschlossen. Eine leere Auswahl ist unzulässig und wird niemals als „alle“ behandelt.
Verteiler sind E-Mail-Empfänger; die Verwaltung der Web-App bleibt beim Admin.
Jede Adresse erhält eine eigene E-Mail mit genau ihrem Verteilerbericht.

## Inhalt und Trennung

Empfängermails enthalten nur „Relevante Änderungen“ und „Unverändert“.
Hinweisfälle, unvollständige Prüfungen, ungeprüfte Quellen und erste Ausgangsstände
bleiben im Adminbericht. Sie werden in Empfängermails nicht als unverändert ausgegeben.
Ist keine Quelle berichtsfähig, wird der leere Empfängerbericht mit Status
„Kein versandfähiger Inhalt“ archiviert und nicht versandt.

Bei einem Projekt mit mehreren Tags zeigt die Empfängerfassung ausschließlich die
freigegebenen Taggruppen. Einzelzuweisungen erscheinen unter „Ausgewählte Projekte“.
Fremde Gruppennamen, Fehlerhinweise und Adminlinks werden nicht übernommen.
Original-Quellenlinks und eingebettete Favicons bleiben verfügbar.

Die KI erstellt Kurztexte projektweise mit getrennten Anfragekontexten. Sie bekommt
für einen Eintrag keine Belege aus einem anderen Projekt. Die anschließende Auswahl
für die Verteiler erfolgt deterministisch im Programm. Pro Empfänger entsteht keine
zusätzliche KI-Anfrage; Dokumente werden weiterhin nur anhand von Hash und Metadaten geprüft.

## Zeitraum, Vorschau und Archiv

Der Standardzeitplan ist Montag, 08:00 Uhr, Europe/Berlin. Unter **SMTP-Mailserver**
lassen sich Versandzeit und Wiederholung ändern. Der Berichtszeitraum in Stunden
oder Tagen ist unabhängig davon und gilt auch für Vorschauen.
Ein globaler Adminbericht wird zusammen mit den einzelnen Empfängerfassungen erzeugt.
Die Fassungen verweisen intern auf ihren globalen Ursprungsbericht.

„Wochenberichte“ bietet Filter für global/Verteiler, eine Verteilerwahl und
chronologische Seiten. Im globalen Bericht sind die zugehörigen Empfängerfassungen
verlinkt. Beim Empfängerbericht stehen Adresse, damaliger Alias, gespeicherte Auswahl,
HTML-Vorschau und ein zeitlich geordnetes Versandprotokoll mit Sekundenangaben bereit.
Spätere Umbenennungen, Auswahländerungen oder das Löschen eines Verteilers verändern
bereits erstellte Berichte nicht. Gelöschte Verteiler bleiben als historische Zuordnung erhalten.

Vorschauen tragen einen Test-Zeitraumschlüssel. Ohne optionalen lokalen Test-SMTP-Server
werden sie nur gespeichert. Mit Mailpit gehen sie ausschließlich an diesen internen
Testserver. Die produktiven SMTP-Zugangsdaten werden für Vorschauen nicht verwendet.
Die allgemeinen SMTP-Verbindungs-/Testmail-Knöpfe bleiben unter „SMTP-Mailserver“.

## Versandprotokoll und Wiederholung

Die Anwendung beansprucht einen Versandauftrag vor dem SMTP-Aufruf atomar in SQLite.
Parallele Aufrufe und Neustarts erzeugen keine zusätzliche Zustellung eines bereits
bestätigten Auftrags. Das Gateway bleibt ein Transportdienst ohne Datenbank-Volume.

Bekannte Ablehnungen werden nach 30 Minuten erneut versucht, maximal drei automatische
Versuche insgesamt. Verbindungsabbrüche nach möglicher Annahme oder ein unterbrochener
Versand werden als „Zustellung unbestätigt“ aufgezeichnet. Diese werden nicht automatisch
wiederholt. Der Admin kann nach Prüfung einen erneuten Versand beauftragen und muss
bei unklarer Zustellung die Möglichkeit einer doppelten E-Mail ausdrücklich bestätigen.
„Versandt“ bedeutet: Der SMTP-Server hat die Nachricht angenommen.

Vor jedem Versuch wird die aktuelle Freigabe erneut geprüft. Entfernte Empfänger,
pausierte/gelöschte Verteiler, geänderte Auswahlen oder entzogene Tagzugehörigkeit
heben eine noch nicht versandte alte Fassung auf. Ein Fehler bei einem Empfänger
verhindert den Versand der anderen Berichte nicht.

## Speicherung und Übertragbarkeit

- `distributions`: Alias, Adressen, Projekt-/Tag-IDs, Status und Änderungszeitpunkte.
- `reports`: globaler Bericht und je Adresse eine unveränderliche Empfängerfassung,
  mit Ursprungsbericht, damaligem Alias, Adresse, Auswahl und Versandstatus.
- `report_mail_events`: Erzeugung, Versandversuche, Annahme, Fehler und manuelle Freigaben.
- `data/reports/<id>.html`, `.txt`, `.images.json`: tatsächliche Ausgabe, Bildbestand
  und eingefrorenes Berichtsmodell für die identische Vorschau.

Exportformat 8 nimmt Verteiler in die Konfiguration und Versandprotokolle in das
Verlaufsarchiv auf. Frühere Exportversionen bleiben lesbar. Importierte Versandaufträge
werden nicht erneut abgespielt; Verteiler sind beim normalen Import zunächst deaktiviert.
Die vorhandene vollständige Volume-Sicherung umfasst auch diese Tabellen und Dateien.

Alle neuen Verwaltungs-, Berichts-, Bild- und EML-Routen verwenden die vorhandene
Adminanmeldung und CSRF-Prüfung. Es entstehen keine zusätzlichen Ports, Zugangslinks
für Empfänger oder öffentlich lesbaren Archive.

## Funktionsnachweis 1.5.0 · 07.11.2026

222 Tests in zehn isolierten Anwendungssuiten bestanden, ohne ausgelassene Tests.
Geprüft wurden unter anderem Mehrfachzuweisung, private Vergleichsdaten,
Hinweisfilterung, getrennte KI-Kontexte, Import alter Archive, paralleler Versand,
unbestätigte SMTP-Annahme und Widerruf einer noch nicht versandten Freigabe.

Der echte Versand über App, Gateway und lokalen Mailpit wurde mit drei getrennten
Empfängeradressen und einem globalen Adminbericht geprüft. Die empfangenen
HTML-Mails stimmen mit den gespeicherten Fassungen überein. Fremde Projekte,
fremde Tagbezeichnungen und Adminhinweise fehlen in den jeweiligen Empfängerfassungen.
Nach einem tatsächlichen Containerneustart erfolgte kein erneuter Versand.

Die Brave-Abnahme über den echten Anmeldeweg umfasst Anlegen und Speichern eines
Verteilers, die gefilterte HTML-Vorschau, Ursprungsbericht, Versandprotokoll und
Historienfilter. Die Testumgebung verwendet ausschließlich synthetische Adressen;
es wurden für diese Erweiterung keine produktiven Testmails versandt.

Weitere 49 Browsertests für Dashboard, bestehende Oberflächenfunktionen und
Dateiverzeichnis bestanden ebenfalls ohne ausgelassene Tests: insgesamt 271 Tests.
Die Live-Abnahme nach dem Portainer-Update auf 1.5.0 bestätigt alle fünf Container
als gesund, die geschützte Verteilerverwaltung über die öffentliche HTTPS-Adresse,
bestehende HTML-/EML-Archive, Anmeldung/Abmeldung und Brave-WebSocket.
Die Datenbank wurde vor der Migration gesichert; vorhandene Berichte und die
bisherigen ENV-Werte wurden beibehalten, abgesehen von der Releaseversion.
Eine zusätzliche Vorschau mit allen 14 produktiven Projekten wurde erfolgreich mit
dem lokalen Modell erzeugt und ausschließlich als Vorschau gespeichert.
