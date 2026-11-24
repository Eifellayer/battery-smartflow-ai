# Bug: Native Steuerung wird nach ~3 Minuten blockiert (HEMS „stale“) und steuert nur ein Gerät

Stand: 2026-11-24, ca. 17:55 Uhr (Home Assistant 2026.11, Fork `Eifellayer/battery-smartflow-ai`, Version `6.0.10-eifellayer.1`).
Ergänzt `BUG-device-profile-last-device.md` (Geräteprofil des zuletzt eingerichteten Geräts).

## Aufbau

- Hyper 2000 (`cloud_mqtt:Kdw0R821`) und SolarFlow 800 Plus (`cloud_mqtt:2TD42WSv`), Cloud-MQTT.
- Zendure-HEMS ist an beiden Geräten aus; beide melden `inactive`.
- Gruppe „Hausspeicher“ mit beiden Geräten angelegt, `control_enabled = true`, `energy_weighted`, `near_zero`.
- Netz: `sensor.power2` (signed), Preis: `sensor.octopus_a_656f9656_electricity_price`.

## Fehler 1: HEMS-Status wird `stale`, Steuerung des Hyper bleibt blockiert (Hauptfehler)

Beobachtung (Verlauf von `sensor.garten_hyper_2000_zendure_hems` und `..._steuerungsstatus`):

| Zeit | HEMS | Steuerungsstatus |
|---|---|---|
| 17:33:03 | inactive | eligible → active (17:33:31) |
| 17:35:58 | inactive | enabled |
| 17:35:59 | **stale** | **hems_blocked** (bleibt bis zum nächsten Neuladen, ca. 10 Minuten) |
| 17:46:12 (nach Neuladen) | inactive | eligible → active |
| 17:49:07 | **stale** | **hems_blocked** (ca. 175 s nach dem letzten HEMS-Wert) |

Ab `hems_blocked` entlädt der Hyper nicht mehr, obwohl der Entscheidungsgrund `very_expensive_force_discharge` ist und rund 230 W aus dem Netz bezogen werden (Entladeleistung beider Geräte 0 W).

Ursache im Code:

- `hardware/zendure/hems.py::evaluate_hems_status`: Ist der HEMS-Wert nicht mehr gültig (`ValueValidity.STALE`), wird `HemsStatus.STALE` geliefert.
- `ZendureHemsCommandGate.update`: Befehle sind nur bei `INACTIVE` oder `UNSUPPORTED` erlaubt (fail-closed). `STALE` blockiert also jede Steuerung.
- `native_zendure_runtime.py`: Für Hyper 2000/HUB 2000 gilt `LEGACY_GROUPED_CONTROL_MAX_DATA_AGE = 180.0` s. Der HEMS-Wert gilt nur 180 s als frisch.
- Der Hyper meldet die HEMS-Eigenschaft über Cloud-MQTT offenbar nur beim Verbinden oder bei einer Änderung, nicht periodisch. Nach 180 s ist der Wert deshalb immer `stale`, obwohl sich nichts geändert hat.
- Die Zahl der empfangenen nativen Nachrichten (`sensor.battery_smartflow_ai_steuerung_planung_empfangene_native_zendure_nachrichten`) bleibt nach dem Neuladen bei 30 stehen (zuletzt aktualisiert 17:46:12). Es kommen also keine neuen Eigenschaftsmeldungen mehr an.
- Beim Neuladen steht im Log jedes Mal: `Zendure Cloud MQTT wildcard subscription was not accepted (subscribe_rejected); falling back to device-scoped topics`.

Erwartetes Verhalten:

- Ein ereignisgesteuertes Merkmal wie HEMS (`inactive`) darf nicht allein durch Zeitablauf auf `stale` fallen, solange das Gerät online ist und seitdem keine Änderungsmeldung kam. Alternativ wird der Wert regelmäßig aktiv abgefragt (Eigenschaften lesen, z. B. alle 30–60 s) oder die Frische wird über das Online-/Heartbeat-Signal des Geräts bestimmt.
- Fail-closed bei `active`, `unknown` und `invalid` bleibt sinnvoll. Nur „zuletzt `inactive`, Gerät online, keine Änderung gemeldet“ sollte als gültig `inactive` gelten.

Vorschlag für Tests:

- Gerät meldet einmal `hems=inactive`, danach 10 Minuten lang nur andere Eigenschaften: Steuerung bleibt erlaubt.
- Gerät geht offline oder meldet `hems=active`: Steuerung wird sofort blockiert.
- Nach Neuladen und Wiederverbinden wird der Zustand korrekt neu aufgebaut.

## Fehler 2: Nur ein Gerät wird nativ gesteuert, der 800 Plus bleibt in `observation`

- `native_zendure_runtime.py::_hardware_control_state` wertet nur `self._selected_device` aus (`native_zendure_selected_device`, genau ein Gerät).
- Der SolarFlow 800 Plus steht trotz Gruppenmitgliedschaft und `control_enabled = true` dauerhaft auf `observation`, sein HEMS ist `inactive`. Er wird nie `eligible`/`enabled`/`active`.

Erwartetes Verhalten:

- Alle Mitglieder einer Gruppe mit nativer Identität werden einzeln gesteuert, jedes Gerät mit eigenem Steuerungsstatus.
- Die Gruppen-Obergrenze für die Entladeleistung ist die Summe der Mitglieder (1200 + 800 = 2000 W). Siehe `BUG-device-profile-last-device.md`.

## Weitere Beobachtungen (niedrigere Priorität)

1. Gruppen-Assistent, Schritt PV-Quellen: Eine Quelle „Solar“ im Modus `aggregate` mit `sensor.hyper_2000_pv_leistung` und `sensor.solarflow_800_plus_pv_leistung`, installierte Leistung 1800 Wp und beide Forecast.Solar-Einträge (`01M01GCEDHMHB2N8NPDTXD09PR`, `01M85DRZDDD0C7SANADQ733MK3`) führt beim Speichern zu `invalid_energy_scope` („PV and forecast sources are not assigned more than once“). Ohne PV-Quelle lässt sich die Gruppe speichern. Es ist unklar, welche Zuordnung doppelt ist. Die Fehlermeldung sollte die Quelle nennen.
2. `dashboard.py` Zeile ca. 457: `(frontend_dir / "hems-dashboard.js").read_bytes()` blockiert die Event-Loop (Home-Assistant-Warnung „Detected blocking call to open“). Besser per `hass.async_add_executor_job` lesen.
3. Beim Neuladen der Integration sind alle Geräte-Entitäten rund 20–60 s `unavailable`. Abgeleitete Helfer (z. B. Hausverbrauch aus `sensor.power2` plus AC-Ausgang minus AC-Eingang) zeigen kurz falsche Werte (25 W statt 230 W).
4. Beim Einrichten über den Options-Dialog entstehen die Entitäten mit Bereichspräfix `garten_`. Die Entitäts-IDs mussten von Hand auf die alten Namen zurückbenannt werden. Ein stabiler Weg (z. B. Hinweis oder Übernahme vorhandener IDs) wäre hilfreich.

## Wie ich es geprüft habe

- Verlauf von `sensor.garten_hyper_2000_zendure_hems`, `..._steuerungsstatus`, `sensor.garten_solarflow_800_plus_*`.
- Home-Assistant-Fehlerlog (Filter `battery_smartflow_ai`).
- Code: `hardware/zendure/hems.py`, `native_zendure_runtime.py` (`_hardware_control_state`, `LEGACY_GROUPED_CONTROL_MAX_DATA_AGE`).
