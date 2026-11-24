from collections import deque
from support import bootstrap
bootstrap()

from custom_components.battery_smartflow_ai.group_feedback import (
    GroupFeedback, RegulationQuality, feedback_target,
)
from custom_components.battery_smartflow_ai.regulation_power_controller import RegulationPowerController
from custom_components.battery_smartflow_ai.regulation_models import (
    GridHistoryState, ModeArbiterResult, StrategyIntent,
)


def target(prev, actual, error, dt=7):
    return feedback_target(previous_w=prev, measured_w=actual,
                           error_w=error, elapsed_s=dt, deadband_w=30)[0]


def test_pending_command_cannot_wind_up():
    assert target(200, 0, 190) == 190
    assert target(190, 0, 190) == 190
    assert target(190, 190, 0) == 190
    assert target(500, 190, 0) == 190
    assert target(500, 500, -310, 0) == 190


def test_time_scaled_and_large_load():
    assert target(190, 190, 100, 3) < target(190, 190, 100, 7)
    assert target(190, 190, 1460, 7) == 740


def test_feedback_respects_arbiter_and_fixed_modes():
    controller = RegulationPowerController()
    grid = GridHistoryState(grid_now_w=190)
    intent = StrategyIntent("arbitrage_discharge", "output", 1000, "test")
    arbiter = ModeArbiterResult("output", "output", True, "test")
    kwargs = dict(intent=intent, arbiter=arbiter, grid=grid,
                  previous_output_w=190, group_max_output_w=100,
                  group_feedback=GroupFeedback(0, 0, 7))
    assert controller.calculate(**kwargs).final_power_w == 100
    arbiter.allowed = False
    assert controller.calculate(**kwargs).final_power_w == 0
    fixed = StrategyIntent("planned_charge", "input", 700, "test")
    result = controller.calculate(intent=fixed, arbiter=ModeArbiterResult("input", "input", True, "test"),
                                  grid=grid, group_feedback=GroupFeedback(0, 0, 3))
    assert result.final_power_w == 700


def test_constant_load_with_delayed_actuator():
    for delay in (3, 7):  # 9 and 21 seconds at 3s sample interval
        pending = deque([0.0] * delay)
        command = actual = 0.0
        errors = []
        for i in range(80):
            actual = pending.popleft()
            error = 190 - actual
            command = target(command, actual, error, 3)
            pending.append(command)
            if i > 35:
                errors.append(error)
        assert max(abs(x) for x in errors) <= 30
        assert all(x >= -30 for x in errors)


def test_quality_counts_only_outside_deadband():
    q = RegulationQuality()
    for time, val in [(0, 50), (10, -50), (20, 5), (30, 50), (40, -50)]:
        result = q.update(time, val, deadband_w=30)
    assert result["state"] == "hunting"
    assert result["sign_changes"] == 3
    assert q.update(41, None, deadband_w=30)["state"] == "unavailable"
