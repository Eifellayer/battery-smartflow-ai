# Vorschlag: Regelung gegen das Pendeln (Prinzipien aus Omnibattery, eigenständig umsetzen)

Bezug: `BUG-dispatch-hunting-low-load.md` (SolarFlow pendelt bei ca. 190 W Hauslast zwischen 0 W und
400–550 W, Netz zwischen +190 W Bezug und −350 W Einspeisung, Periode 20–40 s).

## Lizenzhinweis (wichtig)

Omnibattery (github.com/ffunes/omnibattery) steht unter **GPL-3.0**, dieser Fork unter **MIT**.
Deshalb **keinen Code, keine Dateistruktur und keine Funktionsabläufe aus Omnibattery übernehmen oder
abschreiben**. Allgemeine Regelprinzipien (PD-Regler, Totband, Hysterese, Haltezeiten) sind nicht
schutzfähig und dürfen nach der folgenden Beschreibung selbst neu umgesetzt werden. Bitte nach dieser
Beschreibung und den eigenen Tests implementieren, nicht nach dem Omnibattery-Quelltext.

## Ist-Stand in unserer Engine (gelesen in `regulation_power_controller.py`, `coordinator.py`)

- `_calculate_output` rechnet inkrementell: `raw_target = prev + Kp * error`. `prev` ist
  `last_set_output_w`, also der **letzte Befehl**, nicht die **gemessene Geräteleistung**.
- Verstärkung im Entladepfad: Kp hoch 0.65, Kp runter 0.90, max. Schritt 550 W hoch und 800 W runter,
  Totband 30 W. Kein D-Anteil, keine Richtungs- oder Nulldurchgangs-Haltezeit.
- Die Korrektur ist nicht zeitskaliert. Die Events sind zwar auf 3 s entprellt
  (`GRID_EVENT_REFRESH_MIN_INTERVAL_S`), aber jede Auslösung addiert `Kp * error` erneut.
- Die Geräte haben Totzeit: der SolarFlow meldet etwa alle 7 s, der Hyper stieg im Test erst nach etwa 60 s
  ein und lief nach Lastende noch etwa 36 s weiter.

**Hypothese (nicht bewiesen):** Wenn die Wirkung eines Befehls später am Netzsensor ankommt als der nächste
Regelzyklus, addiert der Regler die Korrektur mehrfach (Aufziehen/Windup) und überschwingt. Danach
wiederholt sich das in der Gegenrichtung.
Alternative Ursachen, die geprüft werden müssen: Aufteilung der Gruppenleistung zwischen den Geräten
(Allocator/Mode-Arbiter) und die Trägheit des Hypers.

## Prinzipien, die sich dafür bewährt haben (Größenordnungen aus der Omnibattery-Doku)

1. **Messwert-Verankerung (Anti-Windup):** liefert das Gerät den Befehl noch nicht (gemessene AC-Leistung
   weicht deutlich ab), setzt die nächste Korrektur auf der **gemessenen** Leistung auf, nicht auf dem
   letzten Befehl.
2. **Zeitskalierung:** Korrektur und Schrittbegrenzung hängen von der verstrichenen Zeit ab, damit ein
   schnellerer Netzsensor die Korrektur nicht vervielfacht.
3. **Niedrigere Verstärkung:** deren Standard ist Kp 0.35 (von 0.65 gesenkt wegen Überschwingen).
   Unsere Defaults 0.65/0.90 liegen darüber.
4. **Totband plus Richtungshysterese:** kleine Anforderungen in Gegenrichtung (Referenz 60 W) bleiben auf
   Leerlauf.
5. **Nulldurchgangs-Haltezeit:** ein Wechsel Entladen ↔ Laden (oder Entladen → Aus) wird nur zugelassen,
   wenn die Anforderung mindestens `max(5 s, 2 × langsamste Aktor-Latenz)` bestehen bleibt. Das verhindert,
   dass der Regler bei einer Lastabsenkung auf die kurze Einspeisung reagiert, solange das Gerät noch
   herunterrampt.
6. **Mindest-Einschaltzeit (Relay-Cooldown):** ein aktives Gerät bleibt eine konfigurierbare Zeit
   (0–60 s) auf einer kleinen Mindestleistung, bevor es auf 0 W zurückfällt.
7. **Regelgüte als Diagnose:** gleitendes 60-s-Fenster mit RMS-Abweichung und Vorzeichenwechseln pro Minute
   außerhalb des Totbands, daraus ein Zustand (stabil, pendelt, träge, begrenzt). Das hilft, Pendeln ohne
   Debug-Aufzeichnung zu sehen.
8. **Aktor-Latenz pro Gerät im Profil** (z. B. SolarFlow ca. 7 s Meldezyklus), als Eingang für Punkte 2 und 5.

D-Anteil (Ableitung mit Tiefpass) würde ich zuerst weglassen: bei Totzeiten von 7 s und mehr verstärkt er
eher Rauschen.

## Vorgeschlagene Reihenfolge (klein → groß, jeweils mit Test)

- **A (klein):** Punkt 1 und 2 in `_calculate_output` und `_calculate_input`.
- **B:** Punkt 3, 5 und 6, Defaults als Profil-Option.
- **C:** Punkt 7 als Diagnose-Sensor.
- **D:** Ursache in der Geräteaufteilung klären: Warum geht der SolarFlow auf 0 W, obwohl 190 W Last da sind
  und der Hyper nicht liefert? Dafür beim nächsten Pendeln 5 Minuten Debug-Aufzeichnung.

## Abnahme mit einem Simulationstest

Aktor mit Totzeit und Rampe (3–7 s Meldezyklus, 8–20 s Wirkungsverzug), Hauslast konstant 190 W:
Netz bleibt im Totband, keine Vorzeichenwechsel außerhalb des Totbands öfter als 1 pro Minute.
Lastsprung auf 1650 W: Summe der Geräte folgt innerhalb von etwa 10 s. Lastende: Rückregeln, ohne dass
Einspeisung über 100 W länger als etwa 10 s anliegt.
