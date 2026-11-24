# Bug: device profile is taken from the LAST configured native device, so the discharge/charge ceiling of a mixed group is wrong

Fork: Eifellayer/battery-smartflow-ai, base `6c59d9f` (6.0.10-eifellayer.1)
Found: 2026-11-24, fresh install of the fork, native Zendure (Cloud MQTT + ZenSDK)

## Setup where it happens

- Account contains two Zendure systems:
  - `cloud_mqtt:Kdw0R821` = Hyper 2000 (PV connected, max output 1200 W)
  - `cloud_mqtt:2TD42WSv` = SolarFlow 800 Plus (no PV, max output 800 W)
- Both are configured one after the other in the native setup flow (`native_device` step).
- Active controller selected afterwards: `cloud_mqtt:Kdw0R821` (Hyper 2000).
- Wanted: both systems in one group (`default`), discharge up to 1200 + 800 = 2000 W.

## Observed

- `sensor.battery_smartflow_ai_steuerung_planung_aktives_gerateprofil` = `SF800Plus`, although the controlling device is the Hyper 2000.
- `number.battery_smartflow_ai_steuerung_planung_max_entladeleistung` can only be set to 0..800 W:
  `Value 1200.0 ... is outside valid range 0.0 - 800.0`. It starts at 700 W.
- The Hyper 2000 therefore can never be told to discharge more than 800 W.

## Cause (read from the code)

`config_flow.py`, native device setup (around lines 490, 685 and 717):

```python
profile = resolve_zendure_device(selected.candidate.identity) if selected else None
...
self._user_input.setdefault(CONF_DEVICE_PROFILE, profile.profile_key)   # ~685
...
self._user_input[CONF_DEVICE_PROFILE] = profile.profile_key             # ~717, in the branch where setup_is_complete()
```

`profile` is a loop/step variable. When the last device of the setup finishes, `CONF_DEVICE_PROFILE` is overwritten with the profile of THAT device (here the SolarFlow 800 Plus, set up second). It is not related to the later chosen active controller or to the group.

`number.py::_power_limit_maximum()` then reads only `coordinator.device_profile_key` (one profile) and uses its `MAX_OUTPUT_W` / `MAX_INPUT_W`:

- `HYPER2000_PROFILE`: `MAX_OUTPUT_W` 1200, `MAX_INPUT_W` 1200 (1800 with PV)
- `SF800PLUS_PROFILE`: `MAX_OUTPUT_W` 800, `MAX_INPUT_W` 1000

So the ceiling follows whichever device was configured last, whatever is actually controlled.

## Expected

1. `CONF_DEVICE_PROFILE` follows the **active controller** (`native_zendure_active_device`), and is updated when the active controller changes (native_zendure_controller step) without re-running the whole setup.
2. When group regulation is active, the ceiling for `max_discharge` / `max_charge` is the **sum over the enabled group members** (Hyper 2000 + SF800 Plus = 2000 W output), not the profile of a single device.
3. Per-member limits (1200 W / 800 W) stay in force when the group power is split.

## Suggested fix

- In `config_flow.py` stop writing `CONF_DEVICE_PROFILE` from the loop variable; derive it from the active device after the controller step, or from the first successfully set up device with `control_enabled=True`.
- Add a helper (for example `group_power_ceiling(entry, key)`) that returns the sum of `MAX_OUTPUT_W` / `MAX_INPUT_W` of the controlled members, and use it in `number.py::_power_limit_maximum()`.
- Cover with tests: two devices (Hyper 2000 first, SF800 Plus second, and the reverse order), active controller = Hyper, group with both members -> `max_discharge` ceiling 2000 W, single Hyper -> 1200 W.

## Other things seen in the same install (lower priority)

- The options menu (`init`) has no entry for groups. The group editor is only reachable through the `dashboard` step with the private key `_portal_action: "systems_groups"`. Before the `HEMS dashboard` there is no visible way to edit groups from the normal menu.
- The group is auto-created as `default` with only the Hyper 2000 as member. The SF800 Plus has to be added by hand, and the wizard then asks for `control_enabled` and strategy again.
- Right after setup `systemstatus` showed `sensor_invalid` for about a minute until sensors were read.
