# Bug: Nativer PV-Wert wird nach ca. 3 Minuten `stale`, Engine steht auf `pv_sensor_invalid` und entlädt nicht

Stand: 2026-11-24, 18:30 Uhr (Home Assistant 2026.11).
Getestet mit Version `6.0.10-eifellayer.2` (Release aus PR #1, Branch `fix/active-controller-profile`).
Ergänzt `BUG-device-profile-last-device.md` und `BUG-native-control-hems-stale-single-device.md`.

## Was in Version `.2` jetzt funktioniert (Live-Befund)

- Aktives Geräteprofil: `Hyper 2000`, Entladegrenze `0–2000 W` (vorher `SF800Plus`, `0–800 W`).
- HEMS-Status des Hyper bleibt auch nach mehr als 3 Minuten `inactive` (vorher nach ca. 175 s `stale` und `hems_blocked`).
- Beide Geräte erreichen `enabled`: Hyper 2000 sofort, SolarFlow 800 Plus ca. 1 Minute nach dem Neuladen (vorher blieb der 800 Plus dauerhaft in `observation`).

## Was weiterhin nicht funktioniert

Die Engine fällt immer rund 3 Minuten nach dem Laden auf `pv_sensor_invalid` und entlädt dann nicht mehr.

`sensor.battery_smartflow_ai_steuerung_planung_engine_status` (Verlauf, Europe/Berlin):

| Neuladen / Engine `ok` | Wechsel auf `pv_sensor_invalid` | Abstand |
|---|---|---|
| 17:33:03 | 17:36:01 | 178 s |
| 17:46:16 | 17:49:14 | 178 s |
| 18:03:53 | 18:06:54 | 181 s |
| 18:17:06 | 18:20:00 | 174 s |

Dabei sind die Home-Assistant-Sensoren `sensor.hyper_2000_pv_leistung` und `sensor.solarflow_800_plus_pv_leistung` gültig (`0.0 W`, es ist dunkel). Das Netz (`sensor.power2`) liegt bei ca. 165 W Bezug, der Strompreis bei ca. 0,58 €/kWh, der Entscheidungsgrund war zuletzt `very_expensive_force_discharge`. Beide Akkus geben 0 W ab. Der Hyper hat zuletzt um 17:46 mit ca. 222 W entladen, danach nie mehr.

## Ursache (vermutet, passt zu den Zeitabständen)

Im `coordinator.py` ist `pv_sensor_valid = pv is not None`. `pv` kommt aus dem konfigurierten PV-Sensor, sonst aus `native_state.pv_power_w`, und der ist nur gültig, solange der Wert frisch ist.

Bei konstant `0 W` (Nacht) meldet das Gerät `pv_power_w` über Cloud-MQTT nur einmal beim Verbinden. Nach ca. 180 s (Frische-Grenze) wird der Wert ungültig, `pv` ist `None`, `engine_health` wird `pv_sensor_invalid`. Es ist derselbe Mechanismus wie beim HEMS-Wert (ereignisgesteuerte Eigenschaft läuft nach Zeit ab). Der Fix für HEMS aus PR #1 (`hems_activity.py`) deckt `pv_power_w` nicht ab.

Das globale `pv_entity` ist in diesem Setup nicht gesetzt. Der Gruppen-Assistent hat beim Speichern einer PV-Quelle (zwei Leistungssensoren plus zwei Forecast.Solar-Einträge) in der Vorversion `invalid_energy_scope` gemeldet, deshalb gibt es keine HA-Entität als Rückfall.

## Erwartetes Verhalten

1. Ein nativer PV-Wert von `0 W`, der seit Minuten unverändert ist, während das Gerät online ist und andere Eigenschaften meldet, ist weiterhin ein gültiger Wert. Er darf nicht allein durch Zeitablauf ungültig werden. Das gilt für alle ereignisgesteuerten nativen Eigenschaften (PV, HEMS, ggf. weitere), idealerweise über eine gemeinsame Regel („zuletzt gültig, Gerät online, keine widersprüchliche Meldung“) oder durch regelmäßiges aktives Abfragen.
2. Ist kein PV-Sensor nötig (reiner Entlade-/Preisbetrieb), darf ein fehlender PV-Wert die Engine nicht komplett stoppen. Mindestens sollte `pv_w = 0` bei gültiger Gerätekommunikation angenommen werden oder der Status `pv_sensor_invalid` darf Entladen bei sehr hohem Preis nicht verhindern.

## Weitere Beobachtungen in `.2`

1. Gruppen-Assistent (Optionen → Systeme & Gruppen → Gruppe bearbeiten → Strategie): Im Schritt „Stromkreise“ gibt es nur noch „Stromkreis hinzufügen“ und „Zurück“, kein „Weiter“. Der Assistent lässt sich nicht ohne Stromkreis fortsetzen (in `.1` gab es „Weiter zu Bereichen“). Wer keine Stromkreisgrenzen hinterlegen will, kommt so nicht zu Netzmessung, Preis und PV-Quellen. Bitte „Weiter“ auch ohne Stromkreis anbieten oder PV-Quellen direkt erreichbar machen (laut PR-Text vorgesehen, im Menü aber nicht sichtbar).
2. Bei jedem Neuladen der Integration sind alle Geräte-Entitäten kurz `unavailable` (ca. 15–60 s). Der Hausverbrauch (Helfer aus `sensor.power2` plus AC-Ausgang minus AC-Eingang) zeigt dann kurz falsche Werte.

## Prüfungen, die helfen würden

- Gerät meldet einmal `pv_power_w = 0` und danach 10 Minuten lang nur andere Eigenschaften: Engine bleibt `ok`.
- Gerät geht offline oder meldet einen widersprüchlichen Wert: PV gilt als ungültig.
- Native Steuerung ohne konfigurierten PV-Sensor bleibt nach dem Neuladen länger als 5 Minuten `ok`.

## Wie ich es geprüft habe

- Verlauf von `engine_status`, `sensor.garten_hyper_2000_steuerungsstatus`, `sensor.garten_solarflow_800_plus_steuerungsstatus`, `sensor.hyper_2000_ac_ausgangsleistung`.
- Code: `coordinator.py` (`pv_sensor_valid`, `engine_health`) in Branch `fix/active-controller-profile`.
