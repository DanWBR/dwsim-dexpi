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
"pid" reading.

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

__version__ = "0.2.0"

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

def _quantity(values):
    """The first value of a list of QualifiedValue that holds a number (or a vector) with a unit."""
    for qualified in values or []:
        value = getattr(qualified, "value", None)
        if value is None or getattr(value, "unit", None) is None:
            continue
        if getattr(value, "value", None) is not None or getattr(value, "values", None):
            return value
    return None


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
                    a, b = ins[0], outs[0]
                    merged = dict(b)
                    merged.update({k: v for k, v in a.items() if v not in (None, "", {})})
                    merged["u"], merged["v"] = a["u"], b["v"]
                    merged["from_port"], merged["to_port"] = a.get("from_port", ""), b.get("to_port", "")
                    merged["from_level"], merged["to_level"] = a.get("from_level"), b.get("to_level")
                    merged["through"] = a.get("through", []) + [d["tag"] or d["cls"]] + b.get("through", [])
                    self.edges = [e for e in self.edges if e is not a and e is not b]
                    self._gone.extend((a, b))
                    if merged["u"] != merged["v"]:
                        self.edges.append(merged)
                        self._joined[id(a)] = self._joined[id(b)] = merged
                        self._went[n] = ("edge", merged)
                    else:
                        self._went[n] = ("node", merged["u"])
                    del self.nodes[n]
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
        net.edge(add(src), add(dst), id=conn.identifier or "", name=label, line_number="",
                 T=temperature_c(conn.temperature), P=pressure_bar(conn.pressure),
                 W=w if w is not None else total, compounds=comp, mole_fractions=moles,
                 from_port=_role(conn.source), to_port=_role(conn.target))
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
    return net, compounds


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


def _control_loops(g) -> list[dict]:
    """The loops of a P&ID: a function that controls, what its transmitter senses and the valve it moves."""
    loops = []
    for n, d in g.nodes(data=True):
        if d.get("label") != "ProcessInstrumentationFunction" or "C" not in (d.get("processInstrumentationFunctions") or ""):
            continue
        variable = (d.get("processInstrumentationFunctionCategory") or "")[:1].upper()
        valves = [v2 for _, a, e in g.out_edges(n, data=True) if e.get("label") == "SignalConveyingFunction"
                  for _, v2, e2 in g.out_edges(a, data=True) if e2.get("label") == "OperatedValveReference"]
        senses = [(s2, e2.get("stitched_from")) for s, _, e in g.in_edges(n, data=True) if e.get("label") == "MeasuringLineFunction"
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
    net.controllers = _control_loops(g)
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
            net.node(n, ITEM, d.get("tagName") or "", _equipment_category(classes), cls)
        elif n not in piped:
            continue
        elif "PipeOffPageConnector" in classes:
            net.node(n, END, cls=cls)
        elif n in controlled:
            net.node(n, ITEM, controlled[n], "Valve", cls)
        elif "SafetyValveOrFitting" in classes:
            net.node(n, ITEM, d.get("pipingComponentName") or "", "Relief", cls)
        else:
            net.node(n, PASS, cls=cls)
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
                 T=None, P=None, W=None, compounds={}, **ends)
    return net, []


# -- the reading ----------------------------------------------------------------------------

OVERVIEW_PIXELS = 2400


def overview(model) -> tuple[str, Bounds | None]:
    """The model's diagram as a PNG (base64) and the part of the sheet it shows.

    ("", None) when the model has no diagram or pyDEXPI's PNG export (the resvg package) is not
    installed: the reading then goes without a picture.
    """
    if model.diagram is None:
        return "", None
    try:
        from pydexpi.exports.png_export import svg_to_png
        from pydexpi.exports.svg_export import DrawDiagram

        svg = DrawDiagram(model.diagram, padding=5.0).draw_svg(return_element=False, background=True)
        box = re.search(r'viewBox="([^"]+)"', svg)
        x, y, width, height = (float(v) for v in box.group(1).split())
        if width <= 0 or height <= 0:
            return "", None
        with tempfile.TemporaryDirectory() as folder:
            png = Path(folder) / "overview.png"
            svg_to_png(svg, png, scale=OVERVIEW_PIXELS / max(width, height))
            data = png.read_bytes()
        return base64.b64encode(data).decode("ascii"), Bounds(x, y, x + width, y + height)
    except Exception as ex:  # noqa: BLE001 - the picture is a convenience, the reading does not depend on it
        print(f"  - no picture of the diagram: {ex}", file=sys.stderr)
        return "", None


def reading(path: Path, strict: bool) -> dict:
    load = load_json_file if path.suffix.lower() == ".json" else load_xml_file
    model = load(str(path), strict=strict)
    is_process = type(model.conceptualModel).__name__ == "ProcessModel"
    extents = object_extents(model)
    net, compounds = process_network(model) if is_process else plant_network(model, extents)
    net.reduce()
    net.unique_tags()

    sheet = None
    for b in extents.values():
        sheet = b if sheet is None else sheet.union(b)
    # with the diagram rendered, the items are placed on that picture
    picture, frame = overview(model)
    if frame is not None:
        sheet = frame

    def place(key):
        b = extents.get(key)
        if b is None or sheet is None or sheet.max_x == sheet.min_x or sheet.max_y == sheet.min_y:
            return None, None
        return (round(((b.min_x + b.max_x) / 2 - sheet.min_x) / (sheet.max_x - sheet.min_x), 4),
                round(((b.min_y + b.max_y) / 2 - sheet.min_y) / (sheet.max_y - sheet.min_y), 4))

    units = []
    for key, d in net.nodes.items():
        if d["kind"] != ITEM:
            continue
        x, y = place(key)
        units.append({"tag": d["tag"], "category": d["category"], "x": x, "y": y, "stages": d.get("stages"),
                      "specs": {k: round(v, 6) for k, v in (d.get("specs") or {}).items()},
                      "dexpi_class": d["cls"], "dexpi_id": key})

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
                      "T": e["T"], "P": e["P"], "W": e["W"],
                      "from_level": e.get("from_level"), "to_level": e.get("to_level"),
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

    origin = " ".join(str(v) for v in (model.originatingSystemName, model.originatingSystemVersion)
                      if isinstance(v, str) and v)
    return {
        "kind": "pfd" if is_process else "pid",
        "source": path.name,
        "units_system": {"temperature": "C", "pressure": "bar", "massflow": "kg/h", "energy": "kW"},
        "units": units,
        "controllers": controllers,
        "streams" if is_process else "lines": lines,
        "process": {"description": "", "class": "", "compounds": compounds,
                    "evidence": "DEXPI " + ("process" if is_process else "plant") + " model" + (", from " + origin if origin else "")},
        "texts": [],
        "notes": net.notes,
        "overview": picture,
        "title_block": {"title": path.stem},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("model", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--strict", action="store_true", help="refuse a file that omits mandatory DEXPI attributes")
    parser.add_argument("--version", action="version", version="dwsim-dexpi " + __version__)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        r = reading(args.model, args.strict)
    except Exception as ex:  # noqa: BLE001 - whatever the loader raises is the message for the user
        print(f"{args.model.name} is not a DEXPI model pyDEXPI can read: {ex}", file=sys.stderr)
        return 1
    out = args.output or args.model.with_suffix(".reading.json")
    out.write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = r.get("streams") or r.get("lines") or []
    print(f"{args.model.name}: {r['kind']}, {len(r['units'])} items "
          f"({', '.join(f'{k} {v}' for k, v in Counter(u['category'] for u in r['units']).most_common())}), "
          f"{len(lines)} lines ({sum(1 for l in lines if l['T'] is not None or l['W'] is not None)} with values), "
          f"{len(r['process']['compounds'])} compounds", file=sys.stderr)
    for note in r["notes"]:
        print("  - " + note, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
