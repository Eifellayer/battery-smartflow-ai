# Fehler 6.0.10-eifellayer.8: "Max. Entladeleistung" begrenzt nur das ausgewählte Gerät, Gruppe bleibt bei Lastspitzen unter ihrer Kapazität

Stand: 2026-11-24, gemessen 20:59–21:02 (Europe/Berlin), Version .8 seit HA-Neustart 20:55.
Setup: Gruppe "Hausspeicher" mit Hyper 2000 (ausgewähltes Gerät, "Aktives Geräteprofil: Hyper 2000")
und SolarFlow 800 Plus, near_zero, Netzsensor `sensor.power2` (+ Bezug, − Einspeisung).
Einstellung: `number.battery_smartflow_ai_steuerung_planung_max_entladeleistung` = 700 W
(Bereich 0–2000 W, Schritt 50).

## Beobachtung (Lastsprung mit Heizlüfter 1650 W, Hausgrundlast etwa 180 W, Summe etwa 1830 W)

- Ab etwa 20:59:45: SolarFlow folgt bis 814 W (seine Gerätegrenze 800 W) um 21:00:30.
- Hyper steigt bei 21:00:31 ein und meldet 637 W Batterie-Entladeleistung (vorher 0 W).
- Danach (21:00:33–21:01:12) bleibt der Netzbezug bei +427 bis +445 W stehen, also etwa 40 s lang.
  Rechnung: 1830 W Last − 800 W (SolarFlow) − etwa 640 W (Hyper) ≈ 390–430 W Bezug. Passt.
- Der Hyper hat also bei etwa 640–700 W aufgehört, obwohl er nach Herstellerangabe 1200 W AC kann.
- Lastende um 21:01:15 verlief gut (Einspeisung nur etwa 12 s, danach wieder um 0 W).

## Vermutete Ursache (im Quelltext gelesen, nicht live bewiesen)

`coordinator.py`, `_native_group_power_limit_w` (in .8 etwa Zeile 947): Die Gruppengrenze ist die Summe der
Entladegrenzen aller Mitglieder, aber nur beim **ausgewählten Gerät** wird sie mit dem Nutzerwert
begrenzt (`if member == selected_id: limit = min(limit, selected_limit_w)`). Der SolarFlow nutzt seine
eigene Grenze (800 W). Mit 700 W am Hyper ergibt das etwa **1500 W** Gruppenkapazität.

Das ist verwirrend, weil die Einstellung "Max. Entladeleistung" für den Nutzer nach einer
**Gesamt**-Grenze aussieht (Bereich bis 2000 W), tatsächlich aber nur das ausgewählte Gerät begrenzt.
Zusätzlich bestimmt die Wahl des "aktiven Geräts" damit stillschweigend, welches Gerät gedrosselt wird.

Nicht geprüft: die Live-Grenze des Hyper aus `shadow_allocation_snapshots`, ob zusätzlich
eine gespeicherte Gruppengrenze (`SETTING_MAX_DISCHARGE` in den Optionen) wirkt, und die AC-Ausgangsleistung
des Hyper im Test (gelesen wurde nur die Batterie-Entladeleistung, also DC-Seite).

## Erwartetes Verhalten

1. Die Bedeutung von "Max. Entladeleistung" muss eindeutig sein: entweder
   a) **Gesamtgrenze der Gruppe** (dann wirkt sie über `limit_group_power` auf die Summe, und die
      Mitglieder behalten ihre eigenen Profilgrenzen), oder
   b) **Grenze pro Gerät** (dann für alle Mitglieder gleich angewendet, nicht nur für das ausgewählte).
2. Die Beschriftung und der Bereich der Einstellung müssen dazu passen (Gruppe: Summe der Profilgrenzen,
   hier 1200 + 800 = 2000 W).
3. Eine Diagnose-Ausgabe der wirksamen Grenzen pro Gerät und für die Gruppe, damit man ohne Quelltext sieht,
   wer warum begrenzt wird.

## Sofort-Workaround

"Max. Entladeleistung" auf 1200 W setzen. Dann ergibt sich eine Gruppenkapazität von etwa 2000 W.
Das wird nach dem Update getestet.

## Getrennt davon: langsames Hochrampen des Hyper

Der Hyper stieg erst etwa 45–50 s nach Lastbeginn ein. Das ist unabhängig von der Grenze und weiter offen.

## Abnahme

Last größer als die Summe der Mitglieder-Grenzen (z. B. 1830 W bei 2000 W Gruppenkapazität):
Beide Geräte erreichen ihre eigenen Grenzen, der Netzbezug sinkt auf etwa 0 W. Test mit
`max_entladeleistung` kleiner als eine Mitgliedergrenze, damit die gewählte Semantik (a) oder (b) belegt ist.
