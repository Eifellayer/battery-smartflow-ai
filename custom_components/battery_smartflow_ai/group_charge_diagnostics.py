"""Per-member view of a planned grid charge and the handover between slots."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

# A member counts as charging once its AC input is clearly above idle draw.
AC_INPUT_ACTIVE_W = 30.0
# Below this remaining time the running slot can no longer carry a command.
MIN_SLOT_SECONDS = 10.0
_MIN_WINDOW_KWH = 0.001

STATE_NOT_PLANNED = "not_planned"
STATE_PLANNED_LATER = "planned_later"
STATE_CHARGE_REQUESTED = "charge_requested"
STATE_CHARGING = "charging_confirmed"
STATE_STOP_REQUESTED = "stop_requested"
STATE_STOPPED = "stopped_confirmed"
STATE_FINISHED = "finished"


def active_dispatch_index(dispatch: Sequence[Any], *, min_seconds: float = MIN_SLOT_SECONDS) -> int:
    """Return the slot whose target applies now.

    The running slot shrinks to zero at each tariff boundary. Commanding
    0 W for its last seconds stops every member and restarts it a moment
    later. Hand over to the following slot instead.
    """

    if len(dispatch) > 1 and dispatch[0].slot.hours * 3600 < min_seconds:
        return 1
    return 0


def member_charge_windows(dispatch: Sequence[Any], device_id: str) -> list[dict[str, Any]]:
    """Merge a member's consecutive planned purchase slots into windows."""

    windows: list[dict[str, Any]] = []
    for entry in dispatch:
        energy = float(entry.grid_charge_by_device.get(device_id, 0.0))
        if energy < _MIN_WINDOW_KWH:
            continue
        start, end = entry.slot.start, entry.slot.end
        if windows and windows[-1]["end"] == start:
            windows[-1]["end"] = end
            windows[-1]["energy_kwh"] += energy
        else:
            windows.append({"start": start, "end": end, "energy_kwh": energy})
    return windows


def _signature(windows: Sequence[Mapping[str, Any]], now: datetime) -> tuple[str, ...]:
    """Identify a plan by the ends of its windows; a running start moves."""

    return tuple(window["end"].isoformat() for window in windows if window["end"] > now)


def evaluate_member(
    previous: Mapping[str, Any] | None, *, now: datetime,
    windows: Sequence[Mapping[str, Any]], target_w: float, measured_ac_input_w: float | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the member's diagnostic state and the memory for the next cycle.

    ``target_w`` is the charge power last requested from the member,
    ``measured_ac_input_w`` the fresh AC input measurement or ``None``.
    """

    previous = previous or {}
    requested = target_w > 0
    drawing = measured_ac_input_w is not None and measured_ac_input_w >= AC_INPUT_ACTIVE_W
    charged_before = bool(previous.get("charged"))
    future = [window for window in windows if window["end"] > now]

    if previous.get("state") == STATE_FINISHED and future:
        # A new plan after a completed charge starts a new run.
        charged_before = False
    if requested:
        state = STATE_CHARGING if drawing else STATE_CHARGE_REQUESTED
    elif drawing:
        state = STATE_STOP_REQUESTED
    elif future:
        paused = previous.get("state") in {
            STATE_CHARGING, STATE_CHARGE_REQUESTED, STATE_STOP_REQUESTED, STATE_STOPPED,
        }
        state = STATE_STOPPED if paused else STATE_PLANNED_LATER
    elif charged_before:
        state = STATE_FINISHED
    else:
        state = STATE_NOT_PLANNED

    signature = _signature(windows, now)
    old_signature = previous.get("signature")
    # Windows that merely ended are not a change of plan.
    still_valid = tuple(end for end in (old_signature or ()) if end > now.isoformat())
    changed_at = previous.get("plan_changed_at")
    if old_signature is not None and still_valid != signature:
        changed_at = now.isoformat()

    result: dict[str, Any] = {
        "state": state,
        "requested_w": round(max(0.0, target_w)),
        "ac_input_w": None if measured_ac_input_w is None else round(measured_ac_input_w),
        "windows": [
            {
                "start": window["start"].isoformat(), "end": window["end"].isoformat(),
                "energy_kwh": round(window["energy_kwh"], 3),
            }
            for window in future
        ],
    }
    if changed_at:
        result["plan_changed_at"] = changed_at
    memory = {
        "state": state,
        "charged": charged_before or (requested and drawing),
        "signature": signature,
        "plan_changed_at": changed_at,
    }
    return result, memory
