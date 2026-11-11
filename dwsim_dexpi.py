# Copyright 2026 Daniel Wagner O. de Medeiros
#
# dwsim-dexpi is free software: you can redistribute it and/or modify it under the terms of the
# GNU Affero General Public License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version.
#
# dwsim-dexpi is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without
# even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License along with this
# program. If not, see <http://www.gnu.org/licenses/>.

"""DEXPI -> DWSIM drawing reading.

Reads a DEXPI file (2.0 / 2.0.1 XML or JSON, or Proteus XML) with pyDEXPI and writes the JSON
"reading" that DWSIM's drawing import opens (DrawingReading.FromJson in
DWSIM.Extensions.SolidCraft). A process model becomes a "pfd" reading, a plant model (P&ID) a
"pid" reading. Several files give one reading: the sheets of a P&ID are joined at their off-page
connectors, and a process model of the same plant gives its stream data and equipment data to
the items and lines of the P&ID with the same tags.

    dwsim-dexpi model.xml -o model.reading.json

Values are written in C, bar (absolute) and kg/h.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

from pydexpi import get_graph, load_json_file, load_xml_file
from pydexpi.toolkits import process_toolkit as pt
from pydexpi.toolkits.diagram_toolkit import Bounds, object_extents
from pydexpi.toolkits.display_name import get_tag
from pydexpi.toolkits.model_toolkit import get_all_instances_in_model

__version__ = "0.9.0"

ITEM, PASS, END = "item", "pass", "end"

# DEXPI process step class -> the reading's category, the first class of the MRO that is listed
# wins. None: a step that carries the flow through (contracted when it has one line in and one
# out, a junction otherwise). END: where a line starts or ends.
STEP_CATEGORIES: dict[str, str | None] = {
    "Source": END, "Sink": END, "Emitting": END, "Flaring": END,
    "Pumping": "Pump",
    "Compressing": "Compressor",
    "DrivingByTurbine": "Expander",
    "ExchangingThermalEnergy": "Exchanger",
    "RemovingThermalEnergy": "Cooler", "Cooling": "Cooler",
    "HeatingInFurnace": "Heater", "HeatingElectrical": "Heater", "SupplyingThermalEnergy": "Heater",
    "Boiling": "Heater", "Evaporating": "Heater", "GeneratingSteam": "Heater",
    "Distilling": "Column", "Absorbing": "Column", "Adsorbing": "Separator",
    "ReactingChemicals": "Reactor",
    "Filtering": "Filter", "Sieving": "Filter",
    "SeparatingByFlash": "Vessel", "SeparatingByGravity": "Vessel", "SeparatingByPhaseSeparation": "Vessel",
    "Separating": "Separator",
    "RegulatingFlow": "Valve", "LimitingFlow": "Orifice",
    "RelievingOverpressure": "Relief", "RelievingVacuum": "Relief", "RelievingVacuumAndOverpressure": "Relief",
    "BlowingDown": "Relief",
    "StoringInTank": "Tank", "StoringInPressureVessel": "Vessel", "StoringFluids": "Tank",
    "Mixing": None, "SplittingMaterial": None,
    "ShuttingOffFlow": None, "PreventingBackflow": None, "SteeringFlow": None,
    "TransportingFluids": None, "TransportingMaterial": None, "Draining": None,
}

# DEXPI plant classes (any class of the hierarchy, in this order) -> category.
EQUIPMENT_CATEGORIES = [
    ("Pump", "Pump"), ("Compressor", "Compressor"), ("Blower", "Compressor"), ("Fan", "Compressor"),
    ("Turbine", "Expander"),
    ("AirCoolingSystem", "AirCooler"), ("HeatExchanger", "Exchanger"),
    ("Furnace", "Heater"), ("Boiler", "Heater"), ("Heater", "Heater"), ("Burner", "Heater"),
    ("Column", "Column"), ("Tank", "Tank"), ("Vessel", "Vessel"),
    ("Filter", "Filter"), ("Sieve", "Filter"), ("Separator", "Separator"), ("Centrifuge", "Separator"),
]
EQUIPMENT_BASES = {"Equipment", "ProcessEquipment", "TaggedPlantItem"}
FLOW_EDGES = {"Pipe", "DirectPipingConnection"}

ATM_BAR = 1.01325
TO_BAR = {"Bar": 1.0, "Kilopascal": 0.01, "Megapascal": 10.0, "Millibar": 0.001, "Pascal": 1e-5,
          "PoundForcePerInchSquared": 0.0689475729}
TO_KGH = {"KilogramPerHour": 1.0, "KilogramPerMinute": 60.0, "KilogramPerSecond": 3600.0,
          "PoundMassPerHour": 0.45359237, "PoundMassPerMinute": 27.2155422, "PoundMassPerSecond": 1632.932532}
TO_FRACTION = {"Fraction": 1.0, "Percent": 1e-2, "PartsPerThousand": 1e-3, "PartsPerMillion": 1e-6,
               "PartsPerBillion": 1e-9}


# -- values ---------------------------------------------------------------------------------

# The case to read where a model gives its values for several cases (--case), and the cases met.
CASE = ""
CASES: set[str] = set()


def _quantity(values):
    """The value of a list of QualifiedValue that holds a number (or a vector) with a unit: the one
    of the case asked for, else the first one that names no case, else the first one."""
    usable = []
    for qualified in values or []:
        value = getattr(qualified, "value", None)
        if value is None or getattr(value, "unit", None) is None:
            continue
        if getattr(value, "value", None) is not None or getattr(value, "values", None):
            case = getattr(qualified, "case", None)
            case = case.strip() if isinstance(case, str) else ""
            if case:
                CASES.add(case)
            usable.append((case, value))
    if not usable:
        return None
    if CASE:
        for case, value in usable:
            if case.lower() == CASE.lower():
                return value
    for case, value in usable:
        if not case:
            return value
    return usable[0][1]


def _unit(quantity) -> tuple[str, str]:
    return type(quantity.unit).__name__, quantity.unit.value


def temperature_c(values) -> float | None:
    q = _quantity(values)
    if q is None or getattr(q, "value", None) is None:
        return None
    unit = _unit(q)[1]
    v = float(q.value)
    return {"DegreeCelsius": v, "Kelvin": v - 273.15, "DegreeFahrenheit": (v - 32.0) / 1.8}.get(unit)


def pressure_bar(values) -> float | None:
    q = _quantity(values)
    if q is None or getattr(q, "value", None) is None:
        return None
    enum, unit = _unit(q)
    if unit not in TO_BAR:
        return None
    return float(q.value) * TO_BAR[unit] + (ATM_BAR if "Gauge" in enum else 0.0)


def massflow_kgh(values) -> float | None:
    q = _quantity(values)
    if q is None or getattr(q, "value", None) is None:
        return None
    unit = _unit(q)[1]
    return float(q.value) * TO_KGH[unit] if unit in TO_KGH else None


TO_WATT = {"Watt": 1.0, "Kilowatt": 1e3, "Megawatt": 1e6}
TO_M2 = {"MetreSquared": 1.0, "CentimetreSquared": 1e-4, "MillimetreSquared": 1e-6, "FootSquared": 0.09290304,
         "InchSquared": 6.4516e-4, "YardSquared": 0.83612736}
TO_M3 = {"MetreCubed": 1.0, "DecimetreCubed": 1e-3, "Litre": 1e-3, "CentimetreCubed": 1e-6, "FootCubed": 0.028316846592,
         "UsGallon": 3.785411784e-3, "UsFluidOunce": 2.95735295625e-5}
TO_W_M2K = {"WattPerMetreSquaredKelvin": 1.0, "KilowattPerMetreSquaredKelvin": 1e3}
TO_M = {"Metre": 1.0, "Millimetre": 1e-3, "Centimetre": 1e-2, "Kilometre": 1e3, "Foot": 0.3048, "Inch": 0.0254}

# the design data of a P&ID's equipment that the reading's specs take: spec name, attribute, unit table
EQUIPMENT_SPECS = [
    ("duty", "designHeatFlowRate", TO_WATT),
    ("area", "designHeatTransferArea", TO_M2),
    ("head", "designPressureHead", TO_M),
    ("power", "designShaftPower", TO_WATT),
    ("volume", "nominalCapacityVolume", TO_M3),
]


def equipment_specs(equipment) -> dict[str, float]:
    """The design data a P&ID gives for an item, in SI units, under the reading's spec names."""
    specs = {}
    for key, attribute, table in EQUIPMENT_SPECS:
        quantity = getattr(equipment, attribute, None)
        unit = getattr(getattr(quantity, "unit", None), "value", None)
        value = getattr(quantity, "value", None)
        if unit in table and isinstance(value, (int, float)) and not isinstance(value, bool):
            specs[key] = float(value) * table[unit]
    specs.update(design_limits(equipment))
    return specs


def plain_pressure_pa(quantity) -> float | None:
    """A pressure given as one quantity (gauge or absolute), in Pa absolute."""
    unit = getattr(getattr(quantity, "unit", None), "value", None)
    value = getattr(quantity, "value", None)
    if unit not in TO_BAR or not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    gauge = "Gauge" in type(quantity.unit).__name__
    return (float(value) * TO_BAR[unit] + (ATM_BAR if gauge else 0.0)) * 1e5


def plain_temperature_k(quantity) -> float | None:
    unit = getattr(getattr(quantity, "unit", None), "value", None)
    value = getattr(quantity, "value", None)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    v = float(value)
    return {"DegreeCelsius": v + 273.15, "Kelvin": v, "DegreeFahrenheit": (v - 32.0) / 1.8 + 273.15}.get(unit)


def design_limits(equipment) -> dict[str, float]:
    """The design pressure and temperature limits of an item: the widest of its chambers, so that a
    stream on either side of an exchanger is checked against a limit that applies to some side."""
    found: dict[str, list[float]] = {}
    for part in [equipment, *(getattr(equipment, "chambers", None) or [])]:
        for key, attribute, convert in (
            ("design_pressure_max", "upperLimitDesignPressure", plain_pressure_pa),
            ("design_pressure_min", "lowerLimitDesignPressure", plain_pressure_pa),
            ("design_temperature_max", "upperLimitDesignTemperature", plain_temperature_k),
            ("design_temperature_min", "lowerLimitDesignTemperature", plain_temperature_k),
        ):
            value = convert(getattr(part, attribute, None))
            if value is not None:
                found.setdefault(key, []).append(value)
    return {key: (max(values) if key.endswith("max") else min(values)) for key, values in found.items()}


def vapor_fraction(stream) -> float | None:
    """The vapour's share of a stream's mass flow, from the phases of its material state; None when they do not tell."""
    state = getattr(stream, "materialStateReference", None)
    phases = getattr(state, "phases", None) or []
    flows = []
    for phase in phases:
        name = " ".join(str(getattr(phase, a, "") or "") for a in ("label", "identifier")).lower()
        flows.append(("vap" in name or "gas" in name, massflow_kgh(phase.massFlow)))
    if not flows or any(flow is None for _, flow in flows):
        return None
    total = sum(flow for _, flow in flows)
    return sum(flow for vapor, flow in flows if vapor) / total if total > 0 else None


def _scaled(values, table) -> float | None:
    q = _quantity(values)
    if q is None or getattr(q, "value", None) is None:
        return None
    unit = _unit(q)[1]
    return float(q.value) * table[unit] if unit in table else None


def _number(values, fraction=False) -> float | None:
    """A dimensionless value; with *fraction*, a percentage is turned into a fraction."""
    for qualified in values or []:
        value = getattr(qualified, "value", None)
        number = getattr(value, "value", value)
        if isinstance(number, (int, float)) and not isinstance(number, bool):
            unit = getattr(getattr(value, "unit", None), "value", None)
            return float(number) * (TO_FRACTION.get(unit, 1.0) if fraction else 1.0)
    return None


def step_specs(step) -> dict[str, float]:
    """What a process step gives for itself, in SI units, under the reading's spec names."""
    def kelvin(values):
        t = temperature_c(values)
        return None if t is None else t + 273.15

    def pascal(values):
        b = pressure_bar(values)
        return None if b is None else b * 1e5

    sources = [
        ("duty", "duty", lambda v: _scaled(v, TO_WATT)),
        ("temperature", "temperature", kelvin),
        ("pressure", "pressure", pascal),
        ("polytropic_efficiency", "polytropicEfficiency", lambda v: _number(v, fraction=True)),
        ("pressure_ratio", "compressionRatio", _number),
        ("reflux_ratio", "refluxRatio", _number),
        ("area", "area", lambda v: _scaled(v, TO_M2)),
        ("u", "heatTransferCoefficient", lambda v: _scaled(v, TO_W_M2K)),
        ("volume", "volume", lambda v: _scaled(v, TO_M3)),
        ("head", "head", lambda v: _scaled(v, TO_M)),
    ]
    specs = {}
    for key, attribute, convert in sources:
        values = getattr(step, attribute, None)
        if isinstance(values, list) and values:
            value = convert(values)
            if value is not None:
                specs[key] = value
    return specs


# -- the network both models are reduced to -------------------------------------------------

class Network:
    """Nodes (items, ends, pass-through pieces) and directed lines between them."""

    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self.notes: list[str] = []
        self.controllers: list[dict] = []
        self.aliases: dict[str, list[str]] = {}
        # where a piece taken out went: ("node", key) or ("edge", line), and the lines joined into others
        self._went: dict[str, tuple] = {}
        self._joined: dict[int, dict] = {}
        self._gone: list[dict] = []

    def node(self, key, kind, tag="", category="", cls="", **more):
        self.nodes.setdefault(key, {"kind": kind, "tag": tag, "category": category, "cls": cls, **more})

    def locate(self, key):
        """What is left where a piece of the model was: ("item", key), ("line", line) or None."""
        for _ in range(10000):
            if key in self.nodes:
                return ("item", key) if self.nodes[key]["kind"] == ITEM else None
            went = self._went.get(key)
            if went is None:
                return None
            if went[0] == "node":
                key = went[1]
                continue
            line = went[1]
            while id(line) in self._joined:
                line = self._joined[id(line)]
            return ("line", line) if any(e is line for e in self.edges) else None
        return None

    def edge(self, u, v, **data):
        self.edges.append({"u": u, "v": v, **data})

    def _ins(self, n):
        return [e for e in self.edges if e["v"] == n]

    def _outs(self, n):
        return [e for e in self.edges if e["u"] == n]

    def _join(self, a, b, between, name=None):
        """One line for the line *a* into the pieces *between* and the line *b* out of them; the pieces go."""
        merged = dict(b)
        merged.update({k: v for k, v in a.items() if v not in (None, "", {})})
        merged["u"], merged["v"] = a["u"], b["v"]
        merged["from_port"], merged["to_port"] = a.get("from_port", ""), b.get("to_port", "")
        merged["from_level"], merged["to_level"] = a.get("from_level"), b.get("to_level")
        merged["from_stage"], merged["to_stage"] = a.get("from_stage"), b.get("to_stage")
        merged["through"] = a.get("through", []) + ([name] if name else []) + b.get("through", [])
        self.edges = [e for e in self.edges if e is not a and e is not b]
        self._gone.extend((a, b))
        if merged["u"] != merged["v"]:
            self.edges.append(merged)
            self._joined[id(a)] = self._joined[id(b)] = merged
            went = ("edge", merged)
        else:
            went = ("node", merged["u"])
        for n in between:
            self._went[n] = went
            del self.nodes[n]

    def sheet(self, index):
        """Marks every piece with its sheet and makes the keys unique among the sheets."""
        def key(k):
            return f"s{index}:{k}"

        self.nodes = {key(k): {**d, "sheet": index, "own": k} for k, d in self.nodes.items()}
        for e in self.edges:
            e["u"], e["v"] = key(e["u"]), key(e["v"])
        for loop in self.controllers:
            loop["valve"], loop["sense"] = key(loop["valve"]), key(loop["sense"])
        self._went = {key(k): (("node", key(w[1])) if w[0] == "node" else w) for k, w in self._went.items()}
        return self

    def absorb(self, other):
        self.nodes.update(other.nodes)
        self.edges.extend(other.edges)
        self.controllers.extend(other.controllers)
        self.notes.extend(other.notes)

    def stitch(self):
        """Joins the sheets: a line that leaves one at an off-page connector goes on where it enters another.

        The connectors are paired by the reference one makes to the other's number, else by a
        number they share, else by the line number of the lines they end, and only when one pair
        fits.
        """
        def ends(flow):
            found = []
            for k, d in self.nodes.items():
                if d["kind"] == END and d.get("flow") == flow:
                    lines = self._ins(k) if flow == "out" else self._outs(k)
                    if len(lines) == 1:
                        found.append((k, d, lines[0]))
            return found

        rules = [
            lambda o, i: bool(o[1].get("refers")) and o[1]["refers"] == i[1].get("number")
            or bool(i[1].get("refers")) and i[1]["refers"] == o[1].get("number"),
            lambda o, i: bool(o[1].get("number")) and o[1]["number"] == i[1].get("number"),
            lambda o, i: bool(o[2].get("number")) and o[2]["number"] == i[2].get("number"),
        ]
        joined = 0
        for rule in rules:
            for o in ends("out"):
                fits = [i for i in ends("in") if i[1]["sheet"] != o[1]["sheet"] and rule(o, i)]
                others = [x for x in ends("out") if x[1]["sheet"] != fits[0][1]["sheet"] and rule(x, fits[0])] if len(fits) == 1 else []
                if len(fits) == 1 and len(others) == 1:
                    self._join(o[2], fits[0][2], [o[0], fits[0][0]])
                    joined += 1
        if joined:
            self.notes.append(f"{joined} lines joined between the sheets at their off-page connectors")
        return joined

    def reduce(self):
        """Leaves items, ends and junctions: drops dead-end pieces, joins the lines through the rest."""
        pruned = Counter()
        joined = Counter()
        changed = True
        while changed:
            changed = False
            for n, d in list(self.nodes.items()):
                if d["kind"] != PASS:
                    continue
                ins, outs = self._ins(n), self._outs(n)
                if not ins or not outs:
                    # a dead end hangs on what it was piped to
                    for e in ins + outs:
                        self._went[n] = ("node", e["u"] if e["v"] == n else e["v"])
                        break
                    self._gone.extend(ins + outs)
                    self.edges = [e for e in self.edges if e["u"] != n and e["v"] != n]
                    del self.nodes[n]
                    pruned[d["cls"]] += 1
                    changed = True
                elif len(ins) == 1 and len(outs) == 1:
                    self._join(ins[0], outs[0], [n], d["tag"] or d["cls"])
                    joined[d["cls"]] += 1
                    changed = True
        count = 0
        for n, d in self.nodes.items():
            if d["kind"] != PASS:
                continue
            count += 1
            d["kind"] = ITEM
            d["category"] = "Mixer" if len(self._ins(n)) > 1 and len(self._outs(n)) == 1 else "Splitter"
            d["tag"] = d["tag"] or f"J-{count}"
        if joined:
            self.notes.append("lines joined through: " + ", ".join(f"{k} x{v}" for k, v in sorted(joined.items())))
        if pruned:
            self.notes.append("dead ends left out: " + ", ".join(f"{k} x{v}" for k, v in sorted(pruned.items())))

    def unique_tags(self):
        seen: Counter = Counter()
        by_class: Counter = Counter()
        for d in self.nodes.values():
            if d["kind"] != ITEM:
                continue
            if not d["tag"]:
                by_class[d["cls"]] += 1
                d["tag"] = f"{d['cls']}-{by_class[d['cls']]}"
            seen[d["tag"].lower()] += 1
            if seen[d["tag"].lower()] > 1:
                d["tag"] = f"{d['tag']}_{seen[d['tag'].lower()]}"


# -- process model (PFD) --------------------------------------------------------------------

def _step_category(step) -> tuple[str, str]:
    for cls in type(step).__mro__:
        if cls.__name__ in STEP_CATEGORIES:
            category = STEP_CATEGORIES[cls.__name__]
            if category is END:
                return END, ""
            return (PASS, "") if category is None else (ITEM, category)
    return ITEM, "Generic"


def _tag(obj) -> str:
    try:
        tag = get_tag(obj)
    except Exception:  # noqa: BLE001 - a tag is a convenience, the id is the fallback
        tag = None
    for candidate in (tag, getattr(obj, "identifier", None), getattr(obj, "label", None)):
        if isinstance(candidate, str) and candidate.strip() and candidate.strip() != "None":
            return candidate.strip()
    return ""


def _first(value):
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _components(template) -> list[str]:
    listing = getattr(template, "listOfComponents", None)
    names = []
    for holder in vars(listing).values() if listing is not None else []:
        if isinstance(holder, list) and holder and hasattr(holder[0], "identifier"):
            for c in holder:
                label = getattr(c, "label", None)
                names.append(label if isinstance(label, str) and label else (c.identifier or c.id))
            break
    return names


def _role(port) -> str:
    """The first role a port performs (TopProduct, BottomProduct, Vapour, Feed...), "" for none."""
    for role in getattr(port, "performedRoles", None) or []:
        name = getattr(role, "name", None)
        if isinstance(name, str) and name not in ("Inlet", "Outlet"):
            return name
    return ""


def _mole_fractions(comp, names) -> dict[str, float]:
    fractions = _quantity(comp.moleFractions)
    if fractions is not None and getattr(fractions, "values", None) and len(fractions.values) == len(names):
        factor = TO_FRACTION.get(_unit(fractions)[1], 1.0)
        return {n: float(x) * factor for n, x in zip(names, fractions.values, strict=True)}
    flows = [getattr(getattr(q, "value", None), "value", None) for q in comp.moleFlow]
    if len(flows) == len(names) and all(isinstance(f, (int, float)) for f in flows) and sum(flows) > 0:
        return {n: float(f) / sum(flows) for n, f in zip(names, flows, strict=True)}
    return {}


def _composition(stream, w_kgh, notes) -> tuple[dict[str, float], float | None, dict[str, float]]:
    """Mass flow of each compound in kg/h, the stream's mass flow when only the state gives it,
    and the mole fractions when the composition is given in moles only."""
    state = getattr(stream, "materialStateReference", None)
    overall = getattr(state, "state", None)
    template = getattr(stream, "materialTemplateReference", None)
    names = _components(template) if template is not None else []
    if overall is None:
        return {}, None, {}
    total = massflow_kgh(overall.massFlow)
    comp = overall.composition
    if comp is None or not names:
        return {}, total, {}
    flows = [massflow_kgh([q]) for q in comp.massFlow]
    if len(flows) == len(names) and all(f is not None for f in flows):
        return dict(zip(names, flows, strict=True)), total, {}
    fractions = _quantity(comp.massFractions)
    basis = w_kgh if w_kgh is not None else total
    if fractions is not None and getattr(fractions, "values", None) and len(fractions.values) == len(names) and basis is not None:
        factor = TO_FRACTION.get(_unit(fractions)[1], 1.0)
        return {n: float(x) * factor * basis for n, x in zip(names, fractions.values, strict=True)}, total, {}
    return {}, total, _mole_fractions(comp, names)


def process_network(model) -> tuple[Network, list[str]]:
    pmod = model.conceptualModel
    net = Network()
    owners = pt.port_owners(pmod)
    steps = {id(step): (step, parent) for step, parent in pt.iter_process_steps(pmod)}

    def owner_step(port):
        hit = owners.get(id(port))
        if hit is None:
            return None
        owner = hit[0]
        if id(owner) not in steps:
            owner = pt.step_holding(pmod, owner)
        if owner is None or id(owner) not in steps:
            return None
        # a step that only groups others hands the line to the sub step its port delegates to
        while getattr(owner, "subProcessSteps", None) and _step_category(owner)[0] != ITEM:
            delegate = _first(getattr(port, "subReference", None))
            below = owners.get(id(delegate)) if delegate is not None else None
            if below is None or id(below[0]) not in steps:
                break
            port, owner = delegate, below[0]
        return owner

    def add(step):
        kind, category = _step_category(step)
        trays = sum(1 for detail in getattr(step, "processStepDetails", None) or [] if type(detail).__name__ == "ContactingOnTray")
        net.node(step.id, kind, _tag(step), category, type(step).__name__,
                 specs=step_specs(step) if kind == ITEM else {}, stages=trays if trays >= 2 else None)
        return step.id

    compounds: list[str] = []
    skipped = Counter()
    for conn in pmod.processConnections:
        if type(conn).__name__ != "Stream":
            skipped[type(conn).__name__] += 1
            continue
        src, dst = owner_step(conn.source), owner_step(conn.target)
        if src is None or dst is None:
            net.notes.append(f"stream {_tag(conn) or conn.id}: an end names no process step, left out")
            continue
        w = massflow_kgh(conn.massFlow)
        comp, total, moles = _composition(conn, w, net.notes)
        for name in list(comp) + list(moles):
            if name not in compounds:
                compounds.append(name)
        label = conn.label if isinstance(conn.label, str) else ""
        def tray(port):
            """The tray of a column a stream ends on: the number of the tray that owns the port, or a delegate of it."""
            for delegate in [port, *(getattr(port, "subReference", None) or [])]:
                hit = owners.get(id(delegate))
                number = getattr(hit[0], "number", None) if hit and type(hit[0]).__name__ == "ContactingOnTray" else None
                if isinstance(number, int) and not isinstance(number, bool) and number >= 1:
                    return number
            return None

        net.edge(add(src), add(dst), id=conn.identifier or "", name=label, line_number="",
                 from_stage=tray(conn.source), to_stage=tray(conn.target),
                 T=temperature_c(conn.temperature), P=pressure_bar(conn.pressure),
                 W=w if w is not None else total, compounds=comp, mole_fractions=moles, vf=vapor_fraction(conn),
                 from_port=_role(conn.source), to_port=_role(conn.target))
        net._went.setdefault(conn.id, ("edge", net.edges[-1]))
    def inside_item(step):
        parent = steps[id(step)][1]
        while parent is not None and id(parent) in steps:
            if _step_category(parent)[0] == ITEM:
                return True
            parent = steps[id(parent)][1]
        return False

    for step, _parent in steps.values():
        if not getattr(step, "subProcessSteps", None) and _step_category(step)[0] == ITEM and not inside_item(step):
            add(step)
    if skipped:
        net.notes.append("connections that are not material streams left out: " +
                         ", ".join(f"{k} x{v}" for k, v in sorted(skipped.items())))
    if not compounds:
        compounds = [c.label if isinstance(c.label, str) and c.label else c.identifier
                     for c in pmod.materialComponents if (c.label or c.identifier)]
    net.controllers = _process_loops(pmod, owners, steps)
    net.aliases = _compound_aliases(pmod)
    return net, compounds


MEASURED = {"pressure": "P", "temperature": "T", "level": "L", "flow": "F"}


def _process_loops(pmod, owners, steps) -> list[dict]:
    """The control loops of a process model: a measuring activity, the controlling activity it informs and the step that one acts on."""
    flows = [c for c in pmod.processConnections if type(c).__name__ == "InformationFlow"]

    def owner(port):
        hit = owners.get(id(port))
        return hit[0] if hit else None

    loops = []
    for system in getattr(pmod, "instrumentationSystemActivities", None) or []:
        activities = list(getattr(system, "instrumentationActivities", None) or [])
        measuring = [a for a in activities if type(a).__name__ == "MeasuringProcessVariable"]
        controlling = [a for a in activities if type(a).__name__ == "ControllingProcessVariable"]
        if not measuring or not controlling:
            continue
        meter = measuring[0]
        quantity = str(getattr(getattr(meter, "measuredVariable", None), "value", "") or "").lower()
        variable = next((letter for word, letter in MEASURED.items() if word in quantity), "")
        stream = getattr(meter, "connectionReference", None)
        step = getattr(meter, "processStepReference", None)
        if not variable:
            # the quantity list of the standard does not have temperature: the value measured tells, by the attribute that holds it
            measured = getattr(meter, "measuredVariableReference", None)
            for holder in (stream, step):
                for name, values in (vars(holder).items() if holder is not None and measured is not None else []):
                    if isinstance(values, list) and any(v is measured for v in values):
                        variable = next((letter for word, letter in MEASURED.items() if word in name.lower()), variable)
        targets = [owner(f.target) for f in flows if any(owner(f.source) is c for c in controlling)]
        valves = [t for t in targets if t is not None and id(t) in steps]
        detail = getattr(meter, "processStepDetailReference", None)
        if step is None and detail is not None:
            step = pt.step_holding(pmod, detail)
        sense = stream.id if stream is not None else step.id if step is not None else None
        if not variable or not valves or sense is None:
            continue
        tag = next((t for t in (getattr(system, "identifier", None), getattr(controlling[0], "identifier", None)) if isinstance(t, str) and t.strip()), "")
        loops.append({"tag": tag.strip(), "variable": variable, "valve": valves[0].id, "sense": sense})
    return loops


def _compound_aliases(pmod) -> dict[str, list[str]]:
    """Other names each compound goes by in the model: its identifier, its IUPAC and ChEBI identifiers and a CAS number in its description."""
    aliases = {}
    for c in pmod.materialComponents:
        name = c.label if isinstance(c.label, str) and c.label else c.identifier
        if not isinstance(name, str) or not name:
            continue
        others = []
        for value in (c.identifier, getattr(c, "iUPAC_identifier", None), getattr(c, "chEBI_identifier", None)):
            if isinstance(value, str) and value.strip() and value.strip() != name:
                others.append(value.strip())
        description = getattr(c, "description", None)
        texts = [getattr(t, "value", "") for t in getattr(description, "singleLanguageStrings", None) or []] if description is not None else []
        for text in texts:
            others += re.findall(r"(?<!\d)\d{2,7}-\d{2}-\d(?!\d)", str(text))
        if others:
            aliases[name] = list(dict.fromkeys(others))
    return aliases


# -- plant model (P&ID) ---------------------------------------------------------------------

def _equipment_category(classes: list[str]) -> str:
    for needle, category in EQUIPMENT_CATEGORIES:
        if any(needle in c for c in classes):
            return category
    return "Generic"


def _nozzle_levels(model, extents) -> dict[str, tuple[str, float]]:
    """Each drawn nozzle's equipment and height on it, 0 at the top to 1 at the bottom."""
    from pydexpi.pydexpi_classes.plant import processEquipment

    levels = {}
    for equipment in get_all_instances_in_model(model, processEquipment.TaggedPlantItem):
        box = extents.get(equipment.id)
        if box is None or box.max_y <= box.min_y:
            continue
        for nozzle in getattr(equipment, "nozzles", None) or []:
            at = extents.get(nozzle.id)
            if at is not None:
                level = ((at.min_y + at.max_y) / 2 - box.min_y) / (box.max_y - box.min_y)
                levels[nozzle.id] = (equipment.id, min(1.0, max(0.0, level)))
    return levels


TOP_BOTTOM = {"Vessel", "Tank", "Column", "Separator"}


def _control_loops(g, model) -> list[dict]:
    """The loops of a P&ID: a function that controls, what its transmitter senses and the valve it moves."""
    from pydexpi.pydexpi_classes.plant import instrumentation

    sensed = {}
    for function in get_all_instances_in_model(model, instrumentation.ProcessSignalGeneratingFunction):
        location = getattr(function, "sensingLocation", None)
        if getattr(location, "id", None):
            sensed[function.id] = location.id
    loops = []
    for n, d in g.nodes(data=True):
        if d.get("label") != "ProcessInstrumentationFunction" or "C" not in (d.get("processInstrumentationFunctions") or ""):
            continue
        variable = (d.get("processInstrumentationFunctionCategory") or "")[:1].upper()
        valves = [v2 for _, a, e in g.out_edges(n, data=True) if e.get("label") == "SignalConveyingFunction"
                  for _, v2, e2 in g.out_edges(a, data=True) if e2.get("label") == "OperatedValveReference"]
        senses = [(sensed[s], None) for s, _, e in g.in_edges(n, data=True) if e.get("label") == "MeasuringLineFunction" and s in sensed]
        senses += [(s2, e2.get("stitched_from")) for s, _, e in g.in_edges(n, data=True) if e.get("label") == "MeasuringLineFunction"
                   for _, s2, e2 in g.out_edges(s, data=True) if e2.get("label") == "reference"]
        if variable not in ("P", "T", "F", "L") or not valves or not senses:
            continue
        number = d.get("processInstrumentationFunctionNumber") or ""
        loops.append({"tag": (variable + (d.get("processInstrumentationFunctions") or "") + " " + number).strip(),
                      "variable": variable, "valve": valves[0], "sense": senses[0][0]})
    return loops


def plant_network(model, extents) -> tuple[Network, list[str]]:
    net = Network()
    g = get_graph(model, graph_level="conceptual")
    levels = _nozzle_levels(model, extents)
    net.controllers = _control_loops(g, model)
    # a file written by DWSIM has only the valves of the flowsheet, and each one is a pressure letdown
    from_dwsim = str(getattr(model, "originatingSystemName", "") or "").strip().upper() == "DWSIM"
    from pydexpi.pydexpi_classes.plant import processEquipment

    equipment = {e.id: e for e in get_all_instances_in_model(model, processEquipment.TaggedPlantItem)}
    from pydexpi.pydexpi_classes.plant import piping

    def text(value):
        return value.strip() if isinstance(value, str) else ""

    fittings = {v.id: v for v in get_all_instances_in_model(model, piping.SafetyValveOrFitting)}
    connectors = {}
    for c in get_all_instances_in_model(model, piping.PipeOffPageConnector):
        reference = getattr(c, "connectorReference", None)
        other = getattr(reference, "referencedConnector", None)
        connectors[c.id] = {
            "flow": "in" if type(c).__name__.startswith("FlowIn") else "out" if type(c).__name__.startswith("FlowOut") else "",
            "number": text(getattr(c, "pipeConnectorNumber", None)),
            "refers": text(getattr(reference, "referencedConnectorNumber", None)) or text(getattr(other, "pipeConnectorNumber", None)),
        }
    controlled = {}
    for u, v, d in g.edges(data=True):
        if d.get("label") == "OperatedValveReference":
            name = g.nodes[u].get("displayName", "")
            controlled[v] = name[name.rfind("(") + 1:name.rfind(")")] if "(" in name else ""
    piped = set()
    for u, v, d in g.edges(data=True):
        if d.get("label") in FLOW_EDGES:
            piped.update((u, v))
    for n, d in g.nodes(data=True):
        classes = d.get("labels", "").split(":")
        cls = classes[0]
        if EQUIPMENT_BASES & set(classes):
            net.node(n, ITEM, d.get("tagName") or "", _equipment_category(classes), cls,
                     specs=equipment_specs(equipment[n]) if n in equipment else {})
        elif n not in piped:
            continue
        elif "PipeOffPageConnector" in classes:
            net.node(n, END, cls=cls, **connectors.get(n, {}))
        elif n in controlled or (from_dwsim and "OperatedValve" in classes):
            net.node(n, ITEM, d.get("pipingComponentName") or controlled.get(n, ""), "Valve", cls)
        elif "SafetyValveOrFitting" in classes:
            valve = fittings.get(n)
            set_pressure = plain_pressure_pa(getattr(valve, "setPressureHigh", None))
            net.node(n, ITEM, text(getattr(valve, "positionNumber", None)) or d.get("pipingComponentName") or "", "Relief", cls,
                     specs={"set_pressure": set_pressure} if set_pressure is not None else {})
        else:
            # a piece DWSIM wrote carries the tag of the flowsheet's object (a mixer, a splitter)
            net.node(n, PASS, tag=(d.get("pipingComponentName") or "") if from_dwsim else "", cls=cls)
    for u, v, d in g.edges(data=True):
        if d.get("label") not in FLOW_EDGES or u not in net.nodes or v not in net.nodes:
            continue
        parts = [d.get("nominalDiameterRepresentation"), d.get("fluidCode"), d.get("lineNumber"), d.get("pipingClassCode")]
        ends = {}
        owner, level = levels.get(d.get("stitched_from"), (None, None))
        for side, key in (("from", u), ("to", v)):
            if owner == key:
                ends[side + "_level"] = round(level, 3)
                if net.nodes[key]["category"] in TOP_BOTTOM:
                    ends[side + "_port"] = "top" if level <= 0.15 else "bottom" if level >= 0.85 else ""
        net.edge(u, v, id=d.get("collapsed_node_id") or "", name="",
                 line_number="-".join(str(p).replace(" ", "") for p in parts if p) if d.get("lineNumber") else "",
                 T=None, P=None, W=None, compounds={}, number=str(d.get("lineNumber") or ""), **ends)
        # what is sensed on a segment or a pipe is sensed on this line
        for piece in str(d.get("collapsed_from") or "").split(",") + [str(d.get("collapsed_node_id") or "")]:
            if piece:
                net._went.setdefault(piece, ("edge", net.edges[-1]))
    for nozzle, (owner, _level) in levels.items():
        if owner in net.nodes:
            net._went.setdefault(nozzle, ("node", owner))
    return net, []


# -- the reading ----------------------------------------------------------------------------

OVERVIEW_PIXELS = 2400


def diagram_svg(model) -> tuple[str, Bounds] | None:
    """The model's diagram as SVG and the part of the sheet it shows; None when the model has no diagram."""
    if model.diagram is None:
        return None
    try:
        from pydexpi.exports.svg_export import DrawDiagram

        svg = DrawDiagram(model.diagram, padding=5.0).draw_svg(return_element=False, background=True)
        box = re.search(r'viewBox="([^"]+)"', svg)
        x, y, width, height = (float(v) for v in box.group(1).split())
        if width <= 0 or height <= 0:
            return None
        return svg, Bounds(x, y, x + width, y + height)
    except Exception as ex:  # noqa: BLE001 - the picture is a convenience, the reading does not depend on it
        print(f"  - no picture of the diagram: {ex}", file=sys.stderr)
        return None


def picture(svg: str, width: float, height: float) -> str:
    """An SVG as a PNG (base64); "" when pyDEXPI's PNG export (the resvg package) is not installed."""
    try:
        from pydexpi.exports.png_export import svg_to_png

        with tempfile.TemporaryDirectory() as folder:
            png = Path(folder) / "overview.png"
            svg_to_png(svg, png, scale=OVERVIEW_PIXELS / max(width, height))
            return base64.b64encode(png.read_bytes()).decode("ascii")
    except Exception as ex:  # noqa: BLE001
        print(f"  - no picture of the diagram: {ex}", file=sys.stderr)
        return ""


class Sheet:
    """One DEXPI file: its model, the network made of it and where its diagram draws each piece."""

    def __init__(self, path: Path, strict: bool, index: int):
        load = load_json_file if path.suffix.lower() == ".json" else load_xml_file
        self.path = path
        self.model = load(str(path), strict=strict)
        self.is_process = type(self.model.conceptualModel).__name__ == "ProcessModel"
        self.extents = object_extents(self.model)
        if self.is_process:
            self.net, self.compounds = process_network(self.model)
        else:
            self.net, self.compounds = plant_network(self.model, self.extents)
        self.net.sheet(index)
        self.drawing = diagram_svg(self.model)
        self.frame = self.drawing[1] if self.drawing else None
        if self.frame is None:
            for b in self.extents.values():
                self.frame = b if self.frame is None else self.frame.union(b)

    def origin(self) -> str:
        return " ".join(str(v) for v in (self.model.originatingSystemName, self.model.originatingSystemVersion) if isinstance(v, str) and v)


def layout(sheets: list[Sheet]):
    """Where the pieces of each sheet go on the one picture: the sheets side by side, each fitted in a cell."""
    drawn = [sh for sh in sheets if sh.frame is not None and sh.frame.max_x > sh.frame.min_x and sh.frame.max_y > sh.frame.min_y]
    if not drawn:
        return (lambda node: (None, None)), ""
    cell_w = drawn[0].frame.max_x - drawn[0].frame.min_x
    cell_h = drawn[0].frame.max_y - drawn[0].frame.min_y
    cells = {}
    parts = []
    for slot, sh in enumerate(drawn):
        f = sh.frame
        w, h = f.max_x - f.min_x, f.max_y - f.min_y
        scale = min(cell_w / w, cell_h / h)
        ox, oy = slot * cell_w + (cell_w - w * scale) / 2, (cell_h - h * scale) / 2
        cells[id(sh)] = (f, scale, ox, oy)
        if sh.drawing:
            inner = re.sub(r"^<\?xml[^>]*\?>", "", sh.drawing[0]).strip()
            inner = re.sub(r"^<svg\b", f'<svg x="{ox}" y="{oy}"', inner, count=1)
            inner = re.sub(r'\bwidth="[^"]*"', f'width="{w * scale}"', inner, count=1)
            inner = re.sub(r'\bheight="[^"]*"', f'height="{h * scale}"', inner, count=1)
            parts.append(inner)
    total_w = len(drawn) * cell_w
    by_index = {next(iter(sh.net.nodes.values()))["sheet"] if sh.net.nodes else -1: sh for sh in drawn}

    def place(node):
        sh = by_index.get(node.get("sheet"))
        b = sh.extents.get(node.get("own")) if sh is not None else None
        if b is None:
            return None, None
        f, scale, ox, oy = cells[id(sh)]
        x = ox + ((b.min_x + b.max_x) / 2 - f.min_x) * scale
        y = oy + ((b.min_y + b.max_y) / 2 - f.min_y) * scale
        return round(x / total_w, 4), round(y / cell_h, 4)

    if not parts:
        return place, ""
    if len(drawn) == 1 and drawn[0].drawing:
        return place, picture(drawn[0].drawing[0], cell_w, cell_h)
    whole = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="{cell_h}" viewBox="0 0 {total_w} {cell_h}">'
             f'<rect width="{total_w}" height="{cell_h}" fill="white"/>' + "".join(parts) + "</svg>")
    return place, picture(whole, total_w, cell_h)


def emit(net: Network, place) -> tuple[list[dict], list[dict], list[dict]]:
    """The items, lines and control loops of a reduced network, as the reading writes them."""
    units = []
    for key, d in net.nodes.items():
        if d["kind"] != ITEM:
            continue
        x, y = place(d)
        unit = {"tag": d["tag"], "category": d["category"], "x": x, "y": y, "stages": d.get("stages"),
                "specs": {k: round(v, 6) for k, v in (d.get("specs") or {}).items()},
                "dexpi_class": d["cls"], "dexpi_id": d.get("own", key)}
        if d["category"] == "Column" and d.get("stages") and d["cls"] != "Absorbing":
            # a column the model gives the trays of is a rigorous one
            unit["type"] = "DistillationColumn"
        units.append(unit)

    def end(key):
        d = net.nodes[key]
        return d["tag"] if d["kind"] == ITEM else ""

    lines = []
    emitted: dict[int, int] = {}
    for e in net.edges:
        frm, to = end(e["u"]), end(e["v"])
        if not frm and not to:
            net.notes.append(f"line {e['id'] or e['line_number']}: joins two sheet ends only, left out")
            continue
        emitted[id(e)] = len(lines)
        lines.append({"id": e["id"], "name": e["name"], "line_number": e["line_number"], "from": frm, "to": to,
                      "T": e["T"], "P": e["P"], "W": e["W"], "vf": None if e.get("vf") is None else round(e["vf"], 6),
                      "from_level": e.get("from_level"), "to_level": e.get("to_level"),
                      "from_stage": e.get("from_stage"), "to_stage": e.get("to_stage"),
                      "from_port": e.get("from_port", ""), "to_port": e.get("to_port", ""),
                      "compounds": {k: round(v, 6) for k, v in e["compounds"].items()},
                      "mole_fractions": {k: round(v, 8) for k, v in e.get("mole_fractions", {}).items()},
                      "through": e.get("through", [])})

    controllers = []
    for loop in net.controllers:
        valve = net.locate(loop["valve"])
        sense = net.locate(loop["sense"])
        if valve is None or valve[0] != "item" or net.nodes[valve[1]]["category"] != "Valve" or sense is None:
            net.notes.append(f"control loop {loop['tag']}: its valve or what it measures is not among the items and lines, left out")
            continue
        if sense[0] == "line" and id(sense[1]) not in emitted:
            continue
        controllers.append({"tag": loop["tag"], "variable": loop["variable"], "valve": net.nodes[valve[1]]["tag"],
                            "measured_item": net.nodes[sense[1]]["tag"] if sense[0] == "item" else "",
                            "measured_line": emitted[id(sense[1])] if sense[0] == "line" else None})
    return units, lines, controllers


def overlay(units: list[dict], lines: list[dict], process: Sheet, notes: list[str]) -> list[str]:
    """Gives a P&ID reading the data of a process model of the same plant, by the tags they share.

    The equipment data of a process step goes to the item with its tag. A stream goes to the P&ID
    line that joins the same two items, through the pieces only the P&ID has (control valves,
    junctions): the first line of that way takes the stream's conditions, flow and composition,
    the lines after it the flow and the composition.
    """
    tags = {u["tag"].lower(): u for u in units}
    net = process.net
    shared = 0
    for d in net.nodes.values():
        if d["kind"] != ITEM:
            continue
        if d["tag"].lower() in tags:
            shared += 1
        else:
            d["kind"] = PASS  # a step the P&ID does not have is passed through
    if shared == 0:
        notes.append(f"{process.path.name}: the process model shares no tag with the P&ID; its data is left out")
        return []
    net.reduce()
    p_units, p_lines, _ = emit(net, lambda node: (None, None))
    for pu in p_units:
        target = tags.get(pu["tag"].lower())
        if target is not None and pu["dexpi_class"] not in ("MixingSimple", "SplittingMaterial"):
            # the operating data of the process model goes before the design data of the P&ID
            target["specs"].update(pu["specs"])
            if target.get("stages") is None:
                target["stages"] = pu.get("stages")

    common = {pu["tag"].lower() for pu in p_units if pu["tag"].lower() in tags}

    # junctions do not have tags on a P&ID: one of each side that joins the same shared items is the same junction,
    # and the P&ID's takes the tag of the process model's
    def around(tag, all_lines):
        ins = sorted(line["from"].lower() for line in all_lines if line["to"].lower() == tag)
        outs = sorted(line["to"].lower() for line in all_lines if line["from"].lower() == tag)
        if any(t and t not in common for t in ins + outs):
            return None
        return tuple(ins), tuple(outs)

    changed = True
    while changed:
        changed = False
        theirs = {}
        for pu in p_units:
            t = pu["tag"].lower()
            if t not in common and pu["category"] in ("Mixer", "Splitter"):
                theirs.setdefault(around(t, p_lines), []).append(pu)
        for u in units:
            t = u["tag"].lower()
            if t in common or u["category"] not in ("Mixer", "Splitter"):
                continue
            key = around(t, lines)
            twins = theirs.get(key, []) if key is not None else []
            if len(twins) == 1 and twins[0]["tag"].lower() not in tags:
                new = twins[0]["tag"]
                for line in lines:
                    if line["from"].lower() == t:
                        line["from"] = new
                    if line["to"].lower() == t:
                        line["to"] = new
                del tags[t]
                u["tag"] = new
                tags[new.lower()] = u
                common.add(new.lower())
                changed = True
                break

    out_of: dict[str, list[dict]] = {}
    for line in lines:
        out_of.setdefault(line["from"].lower(), []).append(line)

    def ways(start, goal):
        """The ways from an item (or a feed) to another (or a product) through pieces only the P&ID has."""
        found = []

        def walk(line, way):
            to = line["to"].lower()
            if to == goal:
                found.append(way + [line])
            elif to and to not in common and len(way) < 30:
                for nxt in out_of.get(to, []):
                    walk(nxt, way + [line])

        for first in out_of.get(start, []):
            walk(first, [])
        return found

    given = 0
    missed = []
    for stream in p_lines:
        a, b = stream["from"].lower(), stream["to"].lower()
        if (a and a not in common) or (b and b not in common):
            continue
        fits = ways(a, b)
        if len(fits) != 1 or any(line.get("_taken") for line in fits[0]):
            missed.append(stream["name"] or stream["id"] or f"{stream['from']} to {stream['to']}")
            continue
        for k, line in enumerate(fits[0]):
            line["_taken"] = True
            if k == 0:
                for key in ("T", "P", "vf"):
                    line[key] = stream[key]
                if not line["name"]:
                    line["name"] = stream["name"] or stream["id"]
            line["W"] = stream["W"]
            line["compounds"] = dict(stream["compounds"])
            line["mole_fractions"] = dict(stream["mole_fractions"])
        if not fits[0][0]["from_port"]:
            fits[0][0]["from_port"] = stream["from_port"]
        if not fits[0][-1]["to_port"]:
            fits[0][-1]["to_port"] = stream["to_port"]
        given += 1
    for line in lines:
        line.pop("_taken", None)
    notes.append(f"{process.path.name}: {len(common)} items of the process model were found in the P&ID; {given} of {len(p_lines)} streams gave their data to its lines")
    if missed:
        notes.append("streams without one line of the P&ID to go to: " + ", ".join(str(m) for m in missed[:15]) + (", ..." if len(missed) > 15 else ""))
    return process.compounds


def reading(paths: list[Path], strict: bool) -> dict:
    sheets = [Sheet(path, strict, index) for index, path in enumerate(paths)]
    plants = [sh for sh in sheets if not sh.is_process]
    processes = [sh for sh in sheets if sh.is_process]
    base = plants if plants else processes[:1]
    net = base[0].net
    for other in base[1:]:
        net.absorb(other.net)
    if len(base) > 1:
        net.stitch()
    net.reduce()
    net.unique_tags()
    place, overview_png = layout(base)
    units, lines, controllers = emit(net, place)
    compounds = base[0].compounds
    notes = net.notes
    if plants and processes:
        compounds = overlay(units, lines, processes[0], notes)
    for extra in (processes[1:] if plants else processes[1:]):
        notes.append(f"{extra.path.name}: one process model is read at a time; left out")

    aliases = {}
    for sh in sheets:
        aliases.update(sh.net.aliases)
    origins = sorted({sh.origin() for sh in sheets if sh.origin()})
    kinds = ("plant" if plants else "process") + (" and process" if plants and processes else "")
    return {
        "kind": "pid" if plants else "pfd",
        "source": ", ".join(path.name for path in paths),
        "units_system": {"temperature": "C", "pressure": "bar", "massflow": "kg/h", "energy": "kW"},
        "units": units,
        "controllers": controllers,
        "lines" if plants else "streams": lines,
        "process": {"description": "", "class": "", "compounds": compounds, "compound_aliases": aliases,
                    "evidence": "DEXPI " + kinds + " model" + ("s" if len(sheets) > 1 else "") + (", from " + ", ".join(origins) if origins else "")},
        "texts": [],
        "notes": notes,
        "overview": overview_png,
        "title_block": {"title": paths[0].stem},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("model", type=Path, nargs="+",
                        help="a DEXPI file; several are read as one plant: P&ID sheets joined at their off-page connectors, and a process model for the stream data")
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--strict", action="store_true", help="refuse a file that omits mandatory DEXPI attributes")
    parser.add_argument("--case", default="", help="the case to read where the model gives its values for several cases")
    parser.add_argument("--version", action="version", version="dwsim-dexpi " + __version__)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    global CASE
    CASE = args.case.strip()
    try:
        r = reading(args.model, args.strict)
        if CASES:
            r["notes"].append("cases in the model: " + ", ".join(sorted(CASES)) + "; read: " +
                              (CASE if CASE.lower() in {c.lower() for c in CASES} else "the values without a case, else the first one of each"))
    except Exception as ex:  # noqa: BLE001 - whatever the loader raises is the message for the user
        print(f"{', '.join(path.name for path in args.model)}: not a DEXPI model pyDEXPI can read: {ex}", file=sys.stderr)
        return 1
    out = args.output or args.model[0].with_suffix(".reading.json")
    out.write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = r.get("streams") or r.get("lines") or []
    print(f"{r['source']}: {r['kind']}, {len(r['units'])} items "
          f"({', '.join(f'{k} {v}' for k, v in Counter(u['category'] for u in r['units']).most_common())}), "
          f"{len(lines)} lines ({sum(1 for l in lines if l['T'] is not None or l['W'] is not None)} with values), "
          f"{len(r['process']['compounds'])} compounds", file=sys.stderr)
    for note in r["notes"]:
        print("  - " + note, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
