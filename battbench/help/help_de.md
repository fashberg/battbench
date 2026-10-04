## Anleitung

### Was BattBench macht
BattBench macht aus deinem Ladegerät einen Akku-Prüfstand. Es liest jeden Schacht jedes angeschlossenen Ladegeräts
einmal pro Sekunde aus und speichert jeden Messwert. Daraus erkennt es **Vorgänge** – eine Aufgabe eines Akkus in
einem Schacht, vom Einlegen bis zum Entnehmen oder bis eine andere Aufgabe startet – mit ihren Lade- und
Entlade**phasen**. Ein Vorgang mit gemessener Entladung bekommt eine **Bewertung**. Gibst du deinen Akkus
**Nummern**, lässt sich jeder Vorgang seinem Akku zuordnen, und du siehst über die Monate, wie jeder Akku altert.

BattBench verändert nie etwas am Ladegerät: Starten, Stoppen und Einstellungen machst du wie gewohnt am Gerät.

### Vor dem Start
- Das offizielle Update-Programm **ISD Go** schließen: Es belegt die Ladegeräte über USB exklusiv.
- Die Handy-App **ISD Link** beenden (oder Bluetooth am Handy ausschalten). Ein ISDT-Air-Ladegerät nimmt nur eine
  Bluetooth-Verbindung an und ist für andere unsichtbar, solange das Handy verbunden ist.
- Ein USB-Anschluss am PC liefert wenig Leistung. Der N8 lädt dann mit nur etwa 100 mA pro Schacht, wenn mehrere
  Schächte belegt sind; eine Analyse von 8 Akkus dauert so leicht zwei Tage. Ein QC-3.0- oder USB-PD-Netzteil
  vermeidet das.

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
2. Auf die Schacht-Kachel klicken: Diagramm und Ergebnis-Bereich zeigen den laufenden Vorgang.
3. Im Ergebnis-Bereich den **Akku** wählen (oder zuerst das Modell, um die Liste einzugrenzen). Jede Änderung wird
   sofort gespeichert. Ohne Akku ein Modell wählen oder die **Nennkapazität** eintragen (Vorschläge 500–3000 mAh,
   jeder Wert kann eingetippt werden).
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
- **Status** – *läuft* (mit *lädt* / *entlädt*), *fertig*, *entnommen* (vor dem Ende herausgenommen) oder
  *abgebrochen* (gestoppt, durch eine andere Aufgabe ersetzt oder die Daten brachen ab).

### Bewertung
Die Kapazität wird mit der Nennkapazität verglichen:

| Bewertung | Entladekapazität |
|---|---|
| sehr gut | ab 90 % |
| gut | ab 80 % |
| mäßig | ab 60 % |
| verbraucht | unter 60 % |

Auch der Innenwiderstand wird bewertet:

| Bewertung | NiMH / NiCd / NiZn | Li-Ion / LiFePO4 |
|---|---|---|
| sehr gut | unter 150 mΩ | unter 50 mΩ |
| gut | unter 300 mΩ | unter 100 mΩ |
| mittel | unter 450 mΩ | unter 150 mΩ |
| schlecht | unter 600 mΩ | unter 200 mΩ |
| sehr schlecht | ab 600 mΩ | ab 200 mΩ |

Ein sehr schlechter Innenwiderstand stuft eine (sehr) gute Kapazität auf *mäßig* herab. Ein Akku ohne
Kapazitätsmessung, aber mit sehr hohem Widerstand (ab 1000 mΩ NiMH / 400 mΩ Li-Ion) gilt als *verdächtig*.

### Bedienung
- **Ladegeräte-Reiter** oben: Name, Eingangsspannung, Verbindung (USB-/Bluetooth-Symbol) und eine LED pro Schacht –
  grau leer, orange lädt, pink entlädt, blau Analyse/Aktivierung/Zyklus, grün fertig, rot Fehler. Der Reiter „+“
  zeigt die unterstützten Ladegeräte, die Bluetooth-Suche und wie der A4 Air gelesen wird.
- **Schacht-Kacheln**: Werte des Schachts; der Balken zeigt den Fortschritt des Ladegeräts, seine Pfeile laufen beim
  Laden Richtung 100 % und beim Entladen Richtung 0 %. Ein Klick zeigt den Vorgang des Schachts.
- **Diagramm**: Maus auf eine Kurve, einen Legenden-Eintrag oder eine Achse hebt diesen Wert hervor; ein Klick auf
  Legende oder Achse blendet ihn aus oder ein. Ein Klick auf eine farbige Phase (oder eine Zeile der Phasenliste)
  zeigt nur diese Phase. Ziehen zieht einen Rahmen und zoomt darauf; ist hineingezoomt, scrollt Ziehen in den unteren
  zwei Dritteln durch die Zeit (im oberen Drittel weiterhin Rahmen). Ein Rechtsklick zoomt heraus; das Haus-Symbol
  (unten links) zeigt wieder alles.
- **Tabellen** (unten): Klick auf einen Spaltentitel sortiert; der Trichter im Spaltentitel filtert wie in einer
  Tabellenkalkulation. *Vorgänge* listet alle Vorgänge, *Akkus* deine Akkus mit ihrem Verlauf rechts, *Modelle* die
  Modellliste, *Ladegeräte* die bekannten Ladegeräte (hier umbenennen – der Name wird nur in BattBench gespeichert).
- **Tasten in den Tabellen**: Pfeiltasten bewegen, Enter bearbeitet einen Akku / ein Modell (bei *Vorgänge*: springt
  ins Akku-Feld), Entf löscht.
- **Löschen**: Rechtsklick (oder Entf) auf Vorgänge, Akkus oder Modelle (Strg / Umschalt wählt mehrere). Gelöschte Einträge werden
  nur ausgeblendet; in den *Einstellungen* lassen sie sich wieder anzeigen (grau) – erneutes Löschen entfernt sie
  endgültig.

### Unterstützte Ladegeräte
| Ladegerät | Verbindung | Schächte | Getestet |
|---|---|---|---|
| ISDT **N8** | USB | 8 | ✔ |
| ISDT **A4 Air** | Bluetooth (empfohlen) oder USB | 4 | ✔ |
| ISDT N16 / N24 | USB | 16 / 24 | – |
| ISDT C4, C4 EVO, A4, UC4 | USB | 4 | – |
| ISDT A8 Air, C4 Air | Bluetooth | 8 / 6 | – |
| SkyRC MC3000, MC5000 | Bluetooth | 4 | – |

Mit „–“ markierte Ladegeräte werden laut Protokoll unterstützt, wurden aber noch nicht am echten Gerät ausprobiert.

### Deine Daten
Alle Messwerte, Vorgänge, Akkus und Modelle liegen in einer Datenbankdatei (SQLite) auf deinem Rechner, normalerweise
`%LOCALAPPDATA%\BattBench\battbench.db`. Der Reiter *Info* zeigt, welche Datei verwendet wird, die *Einstellungen*
zeigen Größe und Anzahl der Einträge. Es wird nichts irgendwohin gesendet.
- **Sicherungen**: Beim Beenden legt BattBench eine komprimierte Kopie neben die Datenbank
  (`battbench.db-JJJJMMTT-HHMMSS.gz`); die letzten 10 bleiben erhalten. Zum Zurückspielen eine entpacken (z. B. mit
  7-Zip) und `battbench.db` ersetzen, während BattBench geschlossen ist.
- **Alte Messwerte komprimieren** (*Einstellungen*, standardmäßig an): Messwerte, die älter als zwei Wochen sind,
  werden automatisch auf einen pro Minute reduziert – pro Minute der Median von Spannung, Strom, Innenwiderstand und
  Temperatur und die letzten Zählerstände; Vorgänge und Bewertungen bleiben unverändert, nur die Kurven alter Vorgänge
  werden gröber. *Jetzt alle Messwerte komprimieren* macht das für alles außer der letzten Stunde und laufenden
  Vorgängen. Ein Tag mit acht belegten Schächten schrumpft auf etwa ein Dreißigstel.
- **Datenbank optimieren** (*Einstellungen*) schreibt die Datei ohne ungenutzten Platz neu.
