"""Least-cost use of a bounded battery across the available tariff horizon."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
import math


# Until measured AC efficiencies are available, use a conservative estimate
# on each side of the battery (81% round trip).
ASSUMED_CONVERSION_EFFICIENCY = 0.90
_EPSILON = 1e-8


@dataclass(frozen=True)
class EconomicSlot:
    start: datetime
    end: datetime
    price: float
    net_load_kwh: float

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


@dataclass(frozen=True)
class EconomicDispatch:
    slot: EconomicSlot
    grid_charge_kwh: float
    discharge_kwh: float
    grid_charge_by_device: dict[str, float] = field(default_factory=dict)
    discharge_by_device: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class EconomicSchedule:
    dispatch: tuple[EconomicDispatch, ...]
    stored_for_later_kwh: float
    available_now_kwh: float

    @property
    def grid_top_up_kwh(self) -> float:
        return sum(item.grid_charge_kwh for item in self.dispatch)


@dataclass(frozen=True)
class EconomicBattery:
    device_id: str
    usable_capacity_kwh: float
    stored_kwh: float
    max_input_w: float
    max_output_w: float
    round_trip_efficiency: float
    minimum_spread_pct: float
    allow_grid_charge: bool


@dataclass
class _Edge:
    target: int
    capacity: float
    cost: float
    reverse: int


def _add_edge(graph, source: int, target: int, capacity: float, cost: float) -> _Edge:
    forward = _Edge(target, capacity, cost, len(graph[target]))
    graph[target].append(_Edge(source, 0.0, -cost, len(graph[source])))
    graph[source].append(forward)
    return forward


def _send_profitable_flow(graph, source: int, sink: int) -> None:
    """Route usable kWh along cheapest paths, including revisions to earlier choices."""

    while True:
        distances = [math.inf] * len(graph)
        previous: list[tuple[int, int] | None] = [None] * len(graph)
        queued = {source}
        queue = deque([source])
        distances[source] = 0.0
        while queue:
            node = queue.popleft()
            queued.remove(node)
            if node == sink:
                continue
            for index, edge in enumerate(graph[node]):
                distance = distances[node] + edge.cost
                if (
                    edge.target == source or edge.capacity <= _EPSILON
                    or distance >= distances[edge.target] - _EPSILON
                ):
                    continue
                distances[edge.target] = distance
                previous[edge.target] = (node, index)
                if edge.target not in queued:
                    queue.append(edge.target)
                    queued.add(edge.target)
        if distances[sink] >= -_EPSILON:
            return

        path = []
        node = sink
        while node != source:
            origin, index = previous[node]
            path.append((origin, graph[origin][index]))
            node = origin
        amount = min(edge.capacity for _, edge in path)
        for origin, edge in path:
            edge.capacity -= amount
            graph[edge.target][edge.reverse].capacity += amount


def plan_economic_dispatch(
    slots: Sequence[EconomicSlot],
    *,
    usable_capacity_kwh: float,
    stored_kwh: float,
    max_input_w: float,
    max_output_w: float,
    export_price: float,
    margin_pct: float,
    allow_grid_charge: bool = True,
) -> EconomicSchedule:
    """Minimize modeled purchase costs plus the selected margin on new purchases.

    Internal flow uses AC-deliverable kWh. The time edges cap stored energy;
    the source and load edges cap charge power, discharge power and local demand.
    No grid energy is purchased unless it can displace higher-priced local load.
    """

    efficiency = ASSUMED_CONVERSION_EFFICIENCY
    round_trip = efficiency * efficiency
    capacity = usable_capacity_kwh * efficiency
    available = min(capacity, max(0.0, stored_kwh * efficiency))
    if not slots or capacity <= 0:
        return EconomicSchedule((), 0.0, 0.0)

    source = len(slots)
    sink = source + 1
    graph: list[list[_Edge]] = [[] for _ in range(sink + 1)]
    # The initial inventory occupies storage even if no demand uses all of it.
    # First route it to demand or to the terminal inventory, then add optional
    # purchases and PV. Residual edges can revise that initial allocation.
    _add_edge(graph, source, 0, available, -1.0)
    terminal = _add_edge(graph, len(slots) - 1, sink, capacity, 0.0)
    demand_edges = []
    for index, slot in enumerate(slots):
        if index + 1 < len(slots):
            _add_edge(graph, index, index + 1, capacity, 0.0)
        ac_discharge_limit = max(0.0, max_output_w * slot.hours / 1000.0)
        demand = min(ac_discharge_limit, max(0.0, slot.net_load_kwh)) if slot.price > 0 else 0.0
        demand_edges.append(_add_edge(graph, index, sink, demand, -slot.price))
    _send_profitable_flow(graph, source, sink)
    # Optional inflows must reach local demand, including at negative prices.
    terminal.capacity = 0.0

    purchase_edges = []
    solar_edges = []
    margin = max(0.0, margin_pct) / 100.0
    for index, slot in enumerate(slots):
        ac_charge_limit = max(0.0, max_input_w * slot.hours / 1000.0)
        surplus = min(ac_charge_limit, max(0.0, -slot.net_load_kwh))
        # Only firm PV surplus enters storage. Prevent competing grid and PV
        # commands in the same interval and account for forfeited feed-in revenue.
        solar_edges.append(_add_edge(
            graph, source, index, surplus * round_trip, max(0.0, export_price) / round_trip,
        ))
        grid_limit = min(ac_charge_limit * round_trip, capacity) if allow_grid_charge and not surplus else 0.0
        purchase_price = (slot.price + max(0.0, slot.price) * margin) / round_trip
        purchase_edges.append(_add_edge(graph, source, index, grid_limit, purchase_price))

    _send_profitable_flow(graph, source, sink)
    dispatch = tuple(
        EconomicDispatch(
            slot,
            graph[index][purchase_edges[index].reverse].capacity / round_trip,
            graph[sink][demand_edges[index].reverse].capacity,
        )
        for index, slot in enumerate(slots)
    )
    required = 0.0
    for index in range(len(dispatch) - 1, 0, -1):
        solar = graph[index][solar_edges[index].reverse].capacity
        item = dispatch[index]
        required = max(0.0, required + item.discharge_kwh - item.grid_charge_kwh * round_trip - solar)
    first = dispatch[0]
    current_solar = graph[0][solar_edges[0].reverse].capacity
    reserved = min(
        max(0.0, available - first.discharge_kwh),
        max(0.0, required - first.grid_charge_kwh * round_trip - current_solar),
    )
    return EconomicSchedule(dispatch, reserved, max(0.0, available - reserved))


def plan_split_economic_dispatch(
    slots: Sequence[EconomicSlot], batteries: Sequence[EconomicBattery], *,
    max_input_w: float, max_output_w: float, export_price: float,
) -> EconomicSchedule:
    """Plan each battery's AC purchases against the same household demand."""

    if not slots or not batteries:
        return EconomicSchedule((), 0.0, 0.0)

    count = len(slots)
    grid = len(batteries) * count
    solar = grid + count
    demand = solar + count
    source, sink = demand + count, demand + count + 1
    graph: list[list[_Edge]] = [[] for _ in range(sink + 1)]
    for index, slot in enumerate(slots):
        load = min(max_output_w * slot.hours / 1000, max(0.0, slot.net_load_kwh))
        _add_edge(graph, demand + index, sink, load if slot.price > 0 else 0.0, -slot.price)

    terminal_edges = []
    available = []
    outputs = []
    for offset, battery in enumerate(batteries):
        base = offset * count
        efficiency = math.sqrt(battery.round_trip_efficiency)
        capacity = battery.usable_capacity_kwh * efficiency
        stored = min(capacity, max(0.0, battery.stored_kwh * efficiency))
        available.append(stored)
        _add_edge(graph, source, base, stored, -1.0)
        terminal_edges.append(_add_edge(graph, base + count - 1, sink, capacity, 0.0))
        outputs.append([
            _add_edge(graph, base + index, demand + index, battery.max_output_w * slot.hours / 1000, 0.0)
            for index, slot in enumerate(slots)
        ])
        for index in range(count - 1):
            _add_edge(graph, base + index, base + index + 1, capacity, 0.0)

    _send_profitable_flow(graph, source, sink)
    for edge in terminal_edges:
        edge.capacity = 0.0

    # The common input edges are in AC-deliverable kWh. Convert the shared
    # input ceiling using the least efficient participant so it cannot be exceeded.
    solar_eta = min(battery.round_trip_efficiency for battery in batteries)
    grid_eta = min(
        (battery.round_trip_efficiency for battery in batteries if battery.allow_grid_charge),
        default=0.0,
    )
    purchases = [[] for _ in batteries]
    solar_edges = [[] for _ in batteries]
    for index, slot in enumerate(slots):
        input_limit = max_input_w * slot.hours / 1000
        surplus = min(input_limit, max(0.0, -slot.net_load_kwh))
        _add_edge(graph, source, solar + index, surplus * solar_eta, 0.0)
        _add_edge(graph, source, grid + index, 0.0 if surplus else input_limit * grid_eta, 0.0)
        for offset, battery in enumerate(batteries):
            node = offset * count + index
            eta = battery.round_trip_efficiency
            room = battery.usable_capacity_kwh * math.sqrt(eta)
            limit = min(room, battery.max_input_w * slot.hours * eta / 1000)
            solar_edges[offset].append(_add_edge(
                graph, solar + index, node, limit, max(0.0, export_price) / eta,
            ))
            cost = slot.price / eta
            if slot.price > 0:
                # At the chosen spread a purchase is eligible if it still saves
                # money after physical losses. Do not count those losses twice.
                cost = max(cost, slot.price * (1 + battery.minimum_spread_pct / 100) - 2 * _EPSILON)
            purchases[offset].append(_add_edge(
                graph, grid + index, node, limit if battery.allow_grid_charge else 0.0, cost,
            ))
    _send_profitable_flow(graph, source, sink)

    dispatch = []
    reserved = 0.0
    for offset, battery in enumerate(batteries):
        required = 0.0
        for index in range(count - 1, -1, -1):
            node = offset * count + index
            solar_kwh = graph[node][solar_edges[offset][index].reverse].capacity
            grid_kwh = graph[node][purchases[offset][index].reverse].capacity
            output_kwh = graph[demand + index][outputs[offset][index].reverse].capacity
            required = max(0.0, required + output_kwh - grid_kwh - solar_kwh)
        first_output = graph[demand][outputs[offset][0].reverse].capacity
        reserved += min(max(0.0, available[offset] - first_output), max(0.0, required - first_output))

    for index, slot in enumerate(slots):
        charge_by_device = {}
        discharge_by_device = {}
        for offset, battery in enumerate(batteries):
            node = offset * count + index
            charge_by_device[battery.device_id] = (
                graph[node][purchases[offset][index].reverse].capacity / battery.round_trip_efficiency
            )
            discharge_by_device[battery.device_id] = graph[demand + index][outputs[offset][index].reverse].capacity
        dispatch.append(EconomicDispatch(
            slot, sum(charge_by_device.values()), sum(discharge_by_device.values()),
            charge_by_device, discharge_by_device,
        ))
    return EconomicSchedule(tuple(dispatch), reserved, max(0.0, sum(available) - reserved))
