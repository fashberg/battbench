## Anleitung

### Was BattBench macht
BattBench macht aus deinem Ladegerät einen Akku-Prüfstand. Es liest jeden Schacht jedes angeschlossenen Ladegeräts
einmal pro Sekunde aus und speichert alle 10 Sekunden einen Messwert (jeden Wechsel von Aufgabe oder Stromrichtung
sofort). Daraus erkennt es **Vorgänge** – eine Aufgabe eines Akkus in
einem Schacht, vom Einlegen bis zum Entnehmen oder bis eine andere Aufgabe startet – mit ihren Lade- und
Entlade**phasen**. Ein Vorgang mit gemessener Entladung bekommt eine **Bewertung**. Gibst du deinen Akkus
**Nummern**, lässt sich jeder Vorgang seinem Akku zuordnen, und du siehst über die Monate, wie jeder Akku altert.

ISDT-Ladegeräte werden nur gelesen: Starten, Stoppen und Einstellungen machst du wie gewohnt am Gerät. Ein SkyRC
MC5000 lässt sich auch aus BattBench steuern (siehe *Profile und Ladegerätsteuerung*).

### Vor dem Start
- Das offizielle Update-Programm **ISD Go** schließen: Es belegt die Ladegeräte über USB exklusiv.
- Die Handy-App beenden – **ISD Link** bei ISDT-Air-Ladegeräten, die **SkyRC**-App beim MC3000 / MC5000 – oder
  Bluetooth am Handy ausschalten. Ein Bluetooth-Ladegerät nimmt nur eine Verbindung an und ist für andere
  unsichtbar, solange das Handy verbunden ist.
- Ladegeräte mit Strom über USB (z. B. der N8): siehe *Stromversorgung* unten.

### Stromversorgung
Ein über USB versorgtes Ladegerät (ISDT N8, C4 EVO, A4 Air, A8 Air, C4 Air – auch wenn BattBench es per Bluetooth liest)
bekommt von PC oder Laptop **zu wenig Leistung**: Deren USB-Anschlüsse liefern 5 V mit 0,5–1,5 A (höchstens 10 W), ohne USB
Power Delivery. Der N8 lädt dann mit nur etwa 100 mA pro Schacht, sobald etwa vier Schächte laden (Entladen ist nicht
betroffen); eine Analyse von 8 Akkus dauert so leicht zwei Tage.
- **USB-C-Hubs**, die USB Power Delivery an einen *Geräte*-Anschluss weitergeben, sind selten und teuer.
- Die meisten **Power-Injektoren** und PD-Hubs laden den *Host* – das ist der PC, nicht das Ladegerät.
- Laden mit 5 V / 10 W bleibt langsam, egal mit welchem Kabel.

Ladegeräte mit Netz- oder DC-Versorgung sind nicht betroffen, und ein SkyRC MC5000 braucht gar kein Kabel zum PC (Bluetooth
oder ein BattBench-ESP).

### Akkus nummerieren und Modelle
- Schreib eine **Nummer** auf jeden Akku (z. B. mit einem Lackstift) und leg ihn im Reiter *Akkus* mit
  *Neuer Akku …* unter derselben Nummer an. Mit *Anzahl* legst du mehrere gleiche Akkus mit fortlaufenden Nummern auf
  einmal an.
- Wähle das **Modell** (z. B. „Panasonic eneloop pro AA“): Es liefert Hersteller, Typ und **Nennkapazität**. Tippen
  in der Liste filtert sie, z. B. „ene pro aa“. BattBench bringt eine Liste gängiger Akkus mit; eigene Modelle legst
  du im Reiter *Modelle* oder mit *Neu …* im Akku-Dialog an. Eine Änderung an einem Modell gilt für alle seine Akkus,
  ihre Vorgänge werden neu bewertet.
- Bei einem Akku ohne Modell trägst du Hersteller, Typ und Kapazität von Hand ein.

### Messen
1. Akku einlegen und am Ladegerät eine Aufgabe starten. Eine Kapazität messen **Analyse** (laden – entladen – laden),
   **Entladen** und **Zyklus**; reines Laden ergibt keine Kapazität.
2. Auf die Schacht-Kachel klicken (oder mit den Pfeiltasten Links / Rechts wechseln: der Cursor steht dann in der
   Modellsuche, bei einem Schacht mit Akku im Akku-Feld): Diagramm und Ergebnis-Bereich zeigen den laufenden Vorgang.
3. Im Ergebnis-Bereich den **Akku** wählen (oder zuerst das Modell, um die Liste einzugrenzen); seine Nummer und
   Enter wählen ihn direkt („32“ → #32, auch in der Modellsuche getippt). Jede Änderung wird sofort gespeichert.
   Ohne Akku ein Modell wählen oder die **Nennkapazität** eintragen (Vorschläge 500–3000 mAh, jeder Wert kann
   eingetippt werden). Eine **Notiz** (z. B. „neue Kontakte“) bleibt beim Vorgang und steht in den Vorgangslisten.
4. Ist die Entladung vorbei, wird der Vorgang bewertet. Solange sie läuft, zeigt der Bereich z. B. „Analyse läuft“.

Der N8 unterscheidet AA und AAA nicht; seine Vorgänge werden deshalb erst bewertet, wenn Akku, Modell oder
Nennkapazität gesetzt sind.

### Die Werte
- **Entladekapazität** – was der Akku bei der (letzten) Entladung abgegeben hat. Im Vergleich zur Nennkapazität
  ergibt sie die Bewertung.
- **Ladekapazität** – was danach eingeladen wurde. Sie ist höher als die Entladekapazität, weil Laden Verluste hat;
  Entladung ÷ Ladung ist der Ladewirkungsgrad (bei NiMH typisch 70–90 %).
- **Innenwiderstand** – wie ihn das Ladegerät misst; **niedriger ist besser**. Er steigt mit Alter und Verschleiß,
  ein hoher Wert lässt die Spannung unter Last einbrechen. Die Ladegeräte messen den Kontaktwiderstand mit, die Werte
  liegen daher höher als bei einem echten Messgerät. *min.* ist der kleinste Wert des Vorgangs, erster und letzter die
  Werte an Anfang und Ende.
- **Temperatur** – die höchste Temperatur des Vorgangs. NiMH-Akkus werden gegen Ende des Ladens warm; ab etwa 45 °C
  wird der Akku belastet.
- **Programm** – MC5000: das Profil, mit dem der Vorgang lief, mit allen Werten (die Ströme zuerst). Am Ladegerät
  selbst gestartet, sind nur Akkutyp, Vorgang und die Ströme (in 100-mA-Schritten) bekannt.
- **Mittlerer Strom** – beim Laden und beim Entladen, aus den Messwerten berechnet (Pausen und Ruhezeiten nicht
  mitgezählt); bei Ladegeräten ohne Profile sind das die Ströme des Vorgangs.
- **Status** – *läuft* (mit *lädt* / *entlädt*), *fertig*, *entnommen* (vor dem Ende herausgenommen) oder
  *abgebrochen* (gestoppt, durch eine andere Aufgabe ersetzt oder die Daten brachen ab).

### Bewertung
Ein Vorgang mit gemessener Entladung (Analyse, Entladen, Zyklus) bekommt einen **Gesundheitsindex** von 0 bis 100 und
daraus eine **Kategorie**. Der Index setzt sich aus vier Teilnoten von je 0–100 zusammen:

| Teilnote | Gewicht | Aus |
|---|---|---|
| Kapazität | 40 % | Entladekapazität in % der Nennkapazität |
| Innenwiderstand | 30 % | kleinster Innenwiderstand des Vorgangs |
| Spannung unter Last | 20 % | Spannungsverlauf der (letzten) Entladung |
| Ladeeffizienz | 10 % | Entladekapazität ÷ danach eingeladene Ladung |

**Kapazität** – SoH = Entladekapazität ÷ Nennkapazität × 100:

| SoH | Teilnote |
|---|---|
| ab 90 % | 100 |
| 70–90 % | 100 − (90 − SoH) × 2,5, also 50 … 100 |
| 50–70 % | 50 − (70 − SoH) × 2, also 10 … 50 |
| unter 50 % | 0 |

**Innenwiderstand** – so, wie das Ladegerät ihn misst. Kontakte und Leitungen sind mit drin, ein gesunder NiMH-AA zeigt
am N8 etwa 150–220 mΩ, wo ein 4-Leiter-Messgerät 20–30 mΩ zeigt. Die üblichen Stufen für 4-Leiter-Werte (30 / 60 /
120 / 250 mΩ) sind deshalb auf die Skala der Ladegeräte übertragen; dazwischen fällt die Teilnote gleichmäßig:

| NiMH / NiCd / NiZn | Li-Ion / LiFePO4 | Teilnote | Angezeigt als |
|---|---|---|---|
| unter 150 mΩ | unter 50 mΩ | 100 | sehr gut |
| 150–300 mΩ | 50–100 mΩ | 100 → 75 | gut |
| 300–450 mΩ | 100–150 mΩ | 75 → 40 | mittel |
| 450–600 mΩ | 150–200 mΩ | 40 → 0 | schlecht |
| ab 600 mΩ | ab 200 mΩ | 0 | sehr schlecht |

**Spannung unter Last** (NiMH) – aus den Messwerten der letzten Entladung. Die Positionen sind Anteile der entnommenen
Ladung (aus dem Strom aufsummiert), ein langsames Ende der Entladung verschiebt sie also nicht:
- *V5* = Spannung nach 5 %: unter 1,15 V kostet (1,15 V − V5) × 200 Punkte,
- *Vmid* = mittlere Spannung von 20 bis 80 % (das Plateau): unter 1,20 V kostet (1,20 V − Vmid) × 150 Punkte,
- *früher Einbruch* = unter 1,0 V vor 80 %: kostet 30 Punkte.

Teilnote = 100 minus diese Punkte (mindestens 0). Die Grenzen gelten für einen Entladestrom von etwa 0,2 C (etwa
500 mA bei einem AA, der Strom des N8). Das Diagramm hinterlegt das Plateau der letzten Entladung dunkler und markiert
beide Werte.

**Ladeeffizienz** – η = Entladekapazität ÷ nach der Entladung eingeladene Ladung; sie zählt, sobald diese Ladung
fertig ist:

| η | Teilnote |
|---|---|
| 75–85 % | 100 (normal für NiMH) |
| 65–75 % | 100 − (75 − η) × 3 |
| unter 65 % | 70 − (65 − η) × 4, mindestens 0 (Verluste, Wärme) |
| über 85 % | 50 (Ladung womöglich zu früh beendet) |

Der N8 meldet oft eine Ladung nur wenig über der Entladung (90–100 %); bei 10 % Gewicht kostet das höchstens 5 Punkte.

**Gesundheitsindex** = 0,4 × Kapazität + 0,3 × Widerstand + 0,2 × Spannung + 0,1 × Effizienz. Eine Teilnote, die sich
nicht bestimmen lässt, fällt weg, die Gewichte der anderen werden auf 100 % hochgerechnet: keine Ladung nach der
Entladung (Aufgabe *Entladen*, oder lädt noch), kein Spannungsverlauf, kein Widerstand – oder nur ein geschätzter: Der
A4 Air meldet über USB keinen, BattBench schätzt ihn aus seinen Ladepausen (≈ in der Schacht-Kachel) und bewertet ihn
nicht.

| Kategorie | Bedingung | Geeignet für |
|---|---|---|
| A · hohe Last | Index ab 85 und Widerstand unter 300 mΩ (Li-Ion: 100 mΩ) | Blitzgeräte, RC-Modelle, motorisiertes Spielzeug |
| B · mittlere Last | Index 70–85 | LED-Taschenlampen, Computermäuse, Fahrradlichter |
| C · geringe Last | Index 50–70 | Fernbedienungen, Wanduhren, Solarleuchten |
| D · Recycling | Index unter 50, Kapazität unter 70 % oder Widerstands-Teilnote 0 | nicht mehr verwendbar |

Die Tabellen zeigen die Bewertung als Index · Kategorie, z. B. *98 · A · hoch*, und sortieren nach dem Index (beim
ersten Klick die besten zuerst); die Schacht-Kacheln zeigen sie hinter dem Akku, z. B. *Slot 4 – #5 · 78 · B · mittel*.

Ohne Nennkapazität gibt es keine Bewertung (Akku zuordnen oder eintragen). Ein Akku ohne Kapazitätsmessung, aber mit
sehr hohem Widerstand (ab 1000 mΩ NiMH / 400 mΩ Li-Ion) gilt als *verdächtig*. Vorgänge, die mit einer älteren
Version gemessen wurden, werden beim Start aus ihren gespeicherten Messwerten ausgewertet.

Die Grenzen folgen IEC 61951-2 (Entladeschluss 1,0 V bei 0,2 C, Ladeeffizienz von NiMH), den Datenblättern von
Panasonic eneloop, GP und Varta (Innenwiderstand) und den 80-%- / 50-%-Kapazitätsgrenzen von Ladegeräten wie SkyRC
MC3000 und Maha MH-C9000.

### Bedienung

#### Ladegeräte-Reiter und Kacheln
- **Reiter** oben: Name, Eingangsspannung, Verbindungssymbol und eine LED pro Schacht – grau leer, orange bereit
  (Akku eingelegt, kein Vorgang), gelb lädt, rot entlädt, blau Analyse/Aktivierung/Zyklus, grün fertig, rot blinkend
  Fehler. Der Reiter „+“ zeigt die unterstützten Ladegeräte und die Verbindungseinstellungen.
- **Kacheln**: Werte und Zustand jedes Schachts. Ein Klick auf eine Kachel zeigt ihren Vorgang und, wenn ein Akku
  zugeordnet ist, seinen Verlauf.
- **Akku vor dem Start**: Schacht ohne Vorgang anklicken und den Akku wählen – der nächste Vorgang in diesem Schacht
  bekommt ihn (Kachel-Titel *Slot 4 → #5*). Ein gestoppter und neu gestarteter (oder der nächste) Vorgang behält den
  Akku des Vorgangs davor, solange der Schacht dazwischen nicht leer war.
- **Offline**: Ist ein Ladegerät abgesteckt oder 30 Sekunden still, liegt ein roter Hinweis über seinen Kacheln.
  BattBench versucht es weiter, und der Hinweis verschwindet von selbst; bleibt er, das USB-Kabel neu einstecken.

#### Diagramm
- Maus auf eine Kurve, einen Legenden-Eintrag oder eine Achse hebt den Wert hervor; ein Klick auf Legende oder Achse
  blendet ihn aus oder ein.
- Ein Klick auf eine farbige Phase (oder eine Zeile der Phasenliste) zeigt nur diese Phase; ein weiterer Klick wieder
  alles.
- Rahmen ziehen zoomt hinein, Rechtsklick zoomt heraus, das Haus-Symbol (unten links) zeigt alles.
- Die letzte Entladung zeigt ihr Plateau (20–80 %) dunkler, die mittlere Spannung dort (gestrichelt) und die Spannung
  nach 5 % (Punkt).
- Wo Messwerte fehlen, reißen die Kurven ab. Bei einem laufenden MC5000-Vorgang füllt der Spannungsverlauf, den das
  Gerät von der aktuellen Phase speichert, die Lücke (gestrichelt, nur Spannung).

#### Tabellen
- Klick auf einen Spaltentitel sortiert; der Trichter im Spaltentitel filtert wie in einer Tabellenkalkulation.
- *Vorgänge* zeigt alle Vorgänge, *Akkus* deine Akkus mit ihrem Verlauf, *Modelle* die Modellliste, *Ladegeräte* die
  bekannten Ladegeräte (hier umbenennen).
- Enter bearbeitet, Entf löscht (Strg / Umschalt wählt mehrere). Gelöschte Einträge sind nur ausgeblendet –
  *Einstellungen* zeigt sie wieder, nochmal löschen entfernt sie endgültig.

#### Berichte
*Bericht exportieren …* (Knopf über der Bewertung, oder Rechtsklick auf einen Vorgang): Bewertung, Akku, Ladegerät,
Diagramm, Details, ein Diagramm für jede Lade-/Entladephase und die Erläuterungen der Werte – als HTML-Seite oder PDF
(DIN A4), in der Sprache der App oder einer anderen.

#### Profile und Ladegerätsteuerung (SkyRC MC5000)
1. **Profil anlegen** im Reiter *Profile* mit *Neu*: Akkutyp, Vorgang (Laden, Entladen, Zyklus …), Ströme, Grenzen.
   Jeder Wert kommt aus den Stufen der SkyRC-App, ein neuer Typ oder Vorgang setzt dessen Vorgaben. Oder einen
   laufenden Schacht als Vorlage nehmen: *Profil → Neu aus aktuellen Einstellungen* in der Ladegerätsteuerung.
2. **Schächte wählen**: Kachel anklicken; für mehrere Strg + Klick auf Kacheln oder die Schacht-Nummern im Feld
   *Ladegerätsteuerung* anklicken.
3. **Starten oder stoppen**: Profil im Feld wählen und *Start* drücken – oder *Aktuelle Einstellungen*, um die
   Schächte mit ihrem eigenen Programm neu zu starten – oder *Stoppen*. BattBench fragt vorher; ein laufender Vorgang
   im Schacht wird ersetzt und beginnt von vorn.

- Das Feld zeigt, was in den gewählten Schächten mit welchem Profil läuft. Bei Vorgängen, die BattBench nicht selbst
  gestartet hat, zeigt es, was das Ladegerät meldet (Typ, Vorgang, Ströme), und die passenden Profile.
- Das Zahnrad im Feld öffnet die Einstellungen des Ladegeräts (Töne, Licht, Bildschirm-Timeout) und seine
  **Bluetooth-Verbindung**: Das Gerät nimmt nur eine Verbindung an, *5 min pausieren* oder *Ausschalten* geben es für
  die SkyRC-App frei, *Einschalten* verbindet wieder – für diesen PC und ein BattBench-ESP zugleich. Eine Pause steht
  mit Restzeit und *Jetzt fortsetzen* auf den Kacheln.
- Wie SkyRC-Ladegeräte verbunden werden, steht im Reiter „+“ (*SkyRC-Ladegeräte verbinden*): automatisch, nur über
  ein BattBench-ESP oder nur über Bluetooth dieses PCs.
- Profile starten und stoppen ist das einzige, womit BattBench ändert, was ein Ladegerät tut, und nur nach deiner
  Bestätigung.

### Unterstützte Ladegeräte
| Ladegerät | Schächte | Stromversorgung | Datenverbindung | Entladen / Analyse | Getestet |
|---|---|---|---|---|---|
| ISDT **N8** | 8 | USB-C – vom PC zu wenig, siehe *Stromversorgung* | USB | ja | ✔ |
| ISDT **A4 Air** | 4 | USB-C (ältere Modelle: Micro-USB) – vom PC zu wenig, siehe *Stromversorgung* | Bluetooth (empfohlen) oder USB | nein | ✔ |
| SkyRC **MC5000** | 4 | Netz | Bluetooth des PCs oder ein BattBench-ESP | ja | ✔ |
| SkyRC MC3000 | 4 | DC | Bluetooth des PCs oder ein BattBench-ESP | ja | – |
| ISDT N16 / N24 | 16 / 24 | DC | USB | ja | – |
| ISDT C4 | 4 | DC | USB | ja | – |
| ISDT C4 Air | 6 | USB-C – vom PC zu wenig, siehe *Stromversorgung* | Bluetooth | nein | – |
| ISDT C4 EVO | 4 | USB-C – vom PC zu wenig, siehe *Stromversorgung* | USB | ja | – |
| ISDT UC4 | 4 | siehe Anleitung des Ladegeräts | USB | ja | – |
| ISDT A8 Air | 8 | USB-C (ältere Modelle: Micro-USB) – vom PC zu wenig, siehe *Stromversorgung* | Bluetooth | nein | – |

Mit „–“ markierte Ladegeräte werden laut Protokoll unterstützt, wurden aber noch nicht am echten Gerät ausprobiert.

Ein **BattBench-ESP** (https://github.com/fashberg/battbench-esp) neben einem SkyRC-Ladegerät hält dessen Bluetooth-Verbindung und puffert die Messwerte,
solange BattBench nicht läuft; BattBench holt sie beim Start ab. Bis es aufgeholt hat, zeigt ein Hinweis über den
Schächten den Stand (Prozent und erreichte Uhrzeit): Die Schächte zeigen diesen Zeitpunkt, nicht die Gegenwart.

### Deine Daten
Alle Messwerte, Vorgänge, Akkus und Modelle liegen in einer Datenbankdatei (SQLite) auf deinem Rechner, normalerweise
`%LOCALAPPDATA%\BattBench\battbench.db`. Der Reiter *Info* zeigt, welche Datei verwendet wird, die *Einstellungen*
zeigen Größe und Anzahl der Einträge. Es wird nichts irgendwohin gesendet.
- **Sicherungen**: BattBench legt eine komprimierte Kopie neben die Datenbank (`battbench.db-JJJJMMTT-HHMMSS.gz`), beim
  Beenden und, solange es läuft, alle 12 Stunden. Erhalten bleiben die letzten 10 Sicherungen und dazu die neueste von
  jedem der letzten 20 Tage, 8 Wochen und 24 Monate; das alles lässt sich in den *Einstellungen* ändern. Zum
  Zurückspielen eine entpacken (z. B. mit 7-Zip) und `battbench.db` ersetzen, während BattBench geschlossen ist.
- **Log-Datei**: `battbench.log` neben der Datenbank (Link im Reiter *Info*) hält Verbindungen, Ladegeräte, die nicht
  mehr antworten, Neuverbindungen und Fehler fest – hilfreich, wenn etwas schiefging. Mit `--debug` gestartet steht
  dort jede einzelne unbeantwortete Abfrage.
- **Alte Messwerte komprimieren** (*Einstellungen*, standardmäßig an): Messwerte, die älter als zwei Wochen sind,
  werden automatisch auf einen pro Minute reduziert – pro Minute der Median von Spannung, Strom, Innenwiderstand und
  Temperatur und die letzten Zählerstände; Vorgänge und Bewertungen bleiben unverändert, nur die Kurven alter Vorgänge
  werden gröber. *Jetzt alle Messwerte komprimieren* macht das für alles außer der letzten Stunde und laufenden
  Vorgängen. Ein Tag mit acht belegten Schächten schrumpft auf etwa ein Dreißigstel.
- **Datenbank optimieren** (*Einstellungen*) schreibt die Datei ohne ungenutzten Platz neu.
