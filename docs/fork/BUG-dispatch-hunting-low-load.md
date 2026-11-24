# Befund 6.0.10-eifellayer.4: Lastsprung funktioniert, aber Leistung pendelt bei kleiner Last

Stand: 2026-11-24, gemessen 18:46–19:04 (Europe/Berlin), Version .4 seit HA-Neustart 18:55.
Setup: Gruppe "Hausspeicher" mit Hyper 2000 und SolarFlow 800 Plus, energy_weighted,
near_zero, Netzsensor `sensor.power2` (vorzeichenbehaftet: + Bezug, − Einspeisung).
Entscheidungsgrund und Engine-Status blieben die ganze Zeit `very_expensive_force_discharge` / `ok`.
Keine Fehler im Log. `pv_sensor_invalid` ist nach .3/.4 nicht mehr aufgetreten.

## 1. Pendeln bei konstanter, kleiner Hauslast (Hauptbefund)

Hauslast konstant etwa 190 W.

- 18:46–18:57:09: SolarFlow stabil bei 173–198 W, Netz um 0 W. Das ist gutes Verhalten.
- Ab 18:57:15 (rund 1,5 Minuten nach dem Neustart mit .4) wechselt der SolarFlow ohne Lastwechsel
  zwischen 0 W und 400–550 W (Entladeleistung der Batterie: 0 → 391 → 396 → 109 → 0 → 386 → 119 ...).
- Das Netz springt dabei zwischen etwa +190 W (Bezug, SolarFlow bei 0) und −220 bis −350 W
  (Einspeisung, SolarFlow bei 400–550). Periode etwa 20–40 Sekunden.
- Das hält nach dem Heißlüfter-Test an: 19:01:57 454 W, 19:02:03 523 W, 19:02:08 178 W, 19:02:14 0 W;
  Netz dazu +175 W Bezug und kurz danach −290 W Einspeisung.

Nicht belegt: warum es ab 18:57:15 beginnt. Der Hyper stand zu dieser Zeit auf 0 W.
Vermutung (ungeprüft): Zwei Geräte in der Regelung plus unterschiedliche Meldezyklen
(SolarFlow meldet etwa alle 7 s, der Hyper nur bei Änderung) lassen den near-zero-Regler schwingen.
Mögliche Maßnahmen: Totband um Netz = 0, Mindesthaltezeit pro Gerät, Rampenbegrenzung
beim Hochregeln, Sollwert nur nach frischer Messung des Geräts ändern.

## 2. Lastsprung (Heizlüfter 1650 W, 18:59:50–19:01:03) — funktioniert, mit Verzögerungen

- SolarFlow reagiert nach etwa 8 s und liefert 740–815 W (Gerätegrenze 800 W).
- Der Hyper steigt erst etwa 60 s nach Lastbeginn ein (19:00:51, 865 W). Summe etwa 1665 W bei 1650 W Last.
  Das ist ein großer Gewinn gegenüber vorher (der Hyper wurde gar nicht gesteuert).
- Nach dem Abschalten sinkt der SolarFlow in etwa 20 s auf 0 W. Der Hyper lief noch bis 19:01:39
  (rund 36 s) mit 865 W weiter, bei nur etwa 190 W Hauslast. Das war sehr wahrscheinlich Einspeisung.
  Die Netzwerte für dieses Fenster wurden nicht ausgewertet.

## Wunsch an den Fix

1. Pendeln bei kleiner Last beseitigen (Totband, Haltezeit, Rampe).
2. Hyper schneller einsteigen und schneller wieder zurücknehmen lassen.
3. Falls möglich, Diagnose-Attribute: letzter Sollwert pro Gerät und Zeitpunkt, damit man
   ohne Debug-Aufzeichnung sieht, wer wann gestellt hat.
