"""Independent, hardware-neutral feedback for near-zero grid regulation."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class GroupFeedback:
    input_w: float
    output_w: float
    elapsed_s: float


def feedback_target(
    *, previous_w: float, measured_w: float, error_w: float,
    elapsed_s: float, deadband_w: float, gain: float = 0.35,
    reference_s: float = 7.0, max_step_up_w: float = 550.0,
    max_step_down_w: float = 800.0,
) -> tuple[float, str]:
    """Calculate a bounded target from actual AC flow and contemporaneous grid error.

    Positive error requests more active-side power. Pending output increases
    are not integrated repeatedly. Decreases to avoid unwanted grid flow
    are never delayed by the timing scale.
    """
    prev = max(0.0, previous_w)
    measured = max(0.0, measured_w)
    band = max(1.0, deadband_w)
    physical_goal = max(0.0, measured + error_w)
    scale = min(1.0, max(0.0, elapsed_s / max(1.0, reference_s)))
    pending = abs(measured - prev) > band
    if abs(error_w) <= band:
        if pending and physical_goal < prev - band:
            return physical_goal, "cancel_pending_overshoot"
        return prev, "inside_deadband"
    if error_w < 0.0:
        # A real export/import error in the wrong direction is protective.
        return min(prev, physical_goal), "measured_fast_reduction"
    if pending:
        return min(prev, physical_goal), "awaiting_actuator"
    factor = 1.0 if error_w >= 300.0 or prev <= band else min(1.0, max(0.05, gain))
    target = min(physical_goal, measured + error_w * factor * scale)
    target = min(target, prev + max(0.0, max_step_up_w) * scale)
    return max(prev, target), "measured_increase"


class RegulationQuality:
    """60-second sample window; sign reversals ignore the deadband."""
    def __init__(self) -> None:
        self.samples = deque()

    def update(self, now_s: float, error_w: float | None, *, deadband_w: float,
               limited: bool = False) -> dict:
        if error_w is None or not math.isfinite(error_w):
            self.samples.clear()
            return {"state": "unavailable", "sample_count": 0}
        if self.samples and now_s < self.samples[-1][0]:
            self.samples.clear()
        if not self.samples or now_s > self.samples[-1][0]:
            self.samples.append((now_s, error_w))
        while self.samples and self.samples[0][0] < now_s - 60.0:
            self.samples.popleft()
        values = [v for _, v in self.samples]
        signs = [1 if v > deadband_w else -1 for v in values if abs(v) > deadband_w]
        changes = sum(a != b for a, b in zip(signs, signs[1:]))
        rms = math.sqrt(sum(v*v for v in values) / len(values))
        span = self.samples[-1][0] - self.samples[0][0]
        state = ("warming_up" if span < 30 else "hunting" if changes > 1
                 else "stable" if rms <= deadband_w else "limited" if limited
                 else "slow")
        return {"state": state, "rms_error_w": round(rms, 1),
                "sign_changes": changes, "window_seconds": round(span, 1),
                "sample_count": len(values), "deadband_w": deadband_w}
