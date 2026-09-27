"""OpenSCAD export/import for REV enclosures (the senior engineer's CAD file).

    export(board, enclosure, product_name) -> OpenSCAD source (renders in OpenSCAD: open-top box with
        floor, standoffs with screw bores, connector cut-outs; PCB + connector bodies as %-reference only)
    parse_params(source) -> {name: float}          (the PARAMETERS block; simple arithmetic allowed)
    to_enclosure(params, board, base_enclosure) -> enclosure   (round-trips export exactly)

Parameter names (one per line, all mm):
    wall, standoff_h, cavity_w, cavity_d, cavity_h, board_origin_x, board_origin_y,
    standoff_<H>_x|y|dia|bore, opening_<J>_<type>_pos|z|w|h
"""
from __future__ import annotations

import ast
import copy
import operator
import re

BEGIN = "// === PARAMETERS"
END = "// === END PARAMETERS"
STANDOFF_KEYS = ("x", "y", "dia", "bore")
OPENING_KEYS = ("pos", "z", "w", "h")


def ident(s) -> str:
    """OpenSCAD-safe identifier fragment."""
    return re.sub(r"[^A-Za-z0-9_]", "_", str(s))


def fmt(v) -> str:
    s = f"{float(v):.3f}"
    return s[:-1] if s.endswith("0") else s     # 91.00, 9.74, 0.125


def _conn(board, cid):
    for c in (board or {}).get("connectors", []):
        if c.get("id") == cid:
            return c
    return None


def standoff_prefix(hole_id) -> str:
    return f"standoff_{ident(hole_id)}"


def opening_prefix(board, conn_id) -> str:
    c = _conn(board, conn_id)
    return f"opening_{ident(conn_id)}_{ident(c['type'])}" if c else f"opening_{ident(conn_id)}"


def param_names(board, enc) -> list[str]:
    names = ["wall", "standoff_h", "cavity_w", "cavity_d", "cavity_h", "board_origin_x", "board_origin_y"]
    for s in enc.get("standoffs", []):
        names += [f"{standoff_prefix(s['hole'])}_{k}" for k in STANDOFF_KEYS]
    for o in enc.get("openings", []):
        names += [f"{opening_prefix(board, o['connector'])}_{k}" for k in OPENING_KEYS]
    return names


def to_params(board, enc) -> dict:
    p = {"wall": enc.get("wall", 2.0), "standoff_h": enc.get("standoff_h", 5.0),
         "cavity_w": enc["cavity"]["w"], "cavity_d": enc["cavity"]["d"], "cavity_h": enc["cavity"]["h"],
         "board_origin_x": enc["board_origin"]["x"], "board_origin_y": enc["board_origin"]["y"]}
    for s in enc.get("standoffs", []):
        for k in STANDOFF_KEYS:
            p[f"{standoff_prefix(s['hole'])}_{k}"] = s[k]
    for o in enc.get("openings", []):
        for k in OPENING_KEYS:
            p[f"{opening_prefix(board, o['connector'])}_{k}"] = o[k]
    return {k: float(v) for k, v in p.items()}


# --------------------------------------------------------------------------- export
def export(board: dict, enclosure: dict, product_name: str) -> str:
    enc = enclosure
    p = to_params(board, enc)
    L = []
    a = L.append
    a(f"// REV enclosure -- {product_name}")
    a(f"// Board rev {board.get('rev', '?')} ({fmt(board['w'])} x {fmt(board['d'])} mm). Units: mm.")
    a("// Coordinates: origin = inside floor, bottom-left corner of the cavity; x right, y back, z up.")
    a("//")
    a("// Edit and save -- REV captures every change.")
    a("// Each saved change is diffed, turned into engineering actions, verified, and recorded in GBrain.")
    a("")
    a(f"{BEGIN} (edit these) ===")
    a(f"wall = {fmt(p['wall'])};              // wall + floor thickness")
    a(f"standoff_h = {fmt(p['standoff_h'])};        // PCB sits on top of the standoffs")
    a(f"cavity_w = {fmt(p['cavity_w'])};          // inside width  (x)")
    a(f"cavity_d = {fmt(p['cavity_d'])};          // inside depth  (y)")
    a(f"cavity_h = {fmt(p['cavity_h'])};          // inside height (z, open top)")
    a(f"board_origin_x = {fmt(p['board_origin_x'])};     // PCB bottom-left corner in the cavity")
    a(f"board_origin_y = {fmt(p['board_origin_y'])};")
    for s in enc.get("standoffs", []):
        pre = standoff_prefix(s["hole"])
        a("")
        a(f"// standoff under mounting hole {s['hole']}")
        for k in STANDOFF_KEYS:
            a(f"{pre}_{k} = {fmt(p[f'{pre}_{k}'])};")
    for o in enc.get("openings", []):
        pre = opening_prefix(board, o["connector"])
        c = _conn(board, o["connector"])
        body = f" (connector body {fmt(c['w'])} x {fmt(c['h'])})" if c else ""
        a("")
        a(f"// {o['side']} wall cut-out for {o['connector']} {c['type'] if c else ''}{body}; pos = along the wall, z = centre height")
        for k in OPENING_KEYS:
            a(f"{pre}_{k} = {fmt(p[f'{pre}_{k}'])};")
    a(f"{END} ===")
    a("")
    a("// --- PCB reference (read-only, from the board design; drawn with % so it is not part of the part) ---")
    a(f"board_w = {fmt(board['w'])};")
    a(f"board_d = {fmt(board['d'])};")
    a(f"board_t = {fmt(board['t'])};")
    a(f"board_max_component_h = {fmt(board['max_component_h'])};")
    a("")
    a("$fn = 48;")
    a("")
    a("module cutout(side, pos, z, w, h) {")
    a('    if (side == "front") translate([pos - w/2, -wall - 1, z - h/2]) cube([w, wall + 2, h]);')
    a('    if (side == "back")  translate([pos - w/2, cavity_d - 1, z - h/2]) cube([w, wall + 2, h]);')
    a('    if (side == "left")  translate([-wall - 1, pos - w/2, z - h/2]) cube([wall + 2, w, h]);')
    a('    if (side == "right") translate([cavity_w - 1, pos - w/2, z - h/2]) cube([wall + 2, w, h]);')
    a("}")
    a("")
    a("module standoff(x, y, dia, bore) {")
    a("    translate([x, y, -0.01]) difference() {")
    a("        cylinder(h = standoff_h + 0.01, d = dia);")
    a("        translate([0, 0, 1]) cylinder(h = standoff_h + 1, d = bore);")
    a("    }")
    a("}")
    a("")
    a("module shell() {")
    a("    difference() {")
    a("        translate([-wall, -wall, -wall]) cube([cavity_w + 2*wall, cavity_d + 2*wall, cavity_h + wall]);")
    a("        cube([cavity_w, cavity_d, cavity_h + 1]);   // cavity, open top")
    for o in enc.get("openings", []):
        pre = opening_prefix(board, o["connector"])
        a(f'        cutout("{o["side"]}", {pre}_pos, {pre}_z, {pre}_w, {pre}_h);')
    a("    }")
    a("}")
    a("")
    a("module enclosure() {")
    a("    union() {")
    a("        shell();")
    for s in enc.get("standoffs", []):
        pre = standoff_prefix(s["hole"])
        a(f"        standoff({pre}_x, {pre}_y, {pre}_dia, {pre}_bore);")
    a("    }")
    a("}")
    a("")
    a("module pcb() {")
    a("    translate([board_origin_x, board_origin_y, standoff_h]) {")
    a("        cube([board_w, board_d, board_t]);")
    for c in board.get("connectors", []):
        cz = fmt(float(board["t"]) + float(c["z"]) - float(c["h"]) / 2)
        cw, ch, cp = fmt(c["w"]), fmt(c["h"]), fmt(c["pos"])
        if c["side"] == "front":
            a(f"        translate([{cp} - {cw}/2, -1, {cz}]) cube([{cw}, 8, {ch}]);   // {c['id']} {c['type']}")
        elif c["side"] == "back":
            a(f"        translate([{cp} - {cw}/2, board_d - 7, {cz}]) cube([{cw}, 8, {ch}]);   // {c['id']} {c['type']}")
        elif c["side"] == "left":
            a(f"        translate([-1, {cp} - {cw}/2, {cz}]) cube([8, {cw}, {ch}]);   // {c['id']} {c['type']}")
        else:
            a(f"        translate([board_w - 7, {cp} - {cw}/2, {cz}]) cube([8, {cw}, {ch}]);   // {c['id']} {c['type']}")
    a("    }")
    a("}")
    a("")
    a("enclosure();")
    a("%pcb();")
    a("")
    return "\n".join(L)


# --------------------------------------------------------------------------- parse
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_ASSIGN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^;]*);")


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _eval(node.operand)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    raise ValueError("unsupported expression")


def parse_params(source: str) -> dict:
    """Numeric assignments from the PARAMETERS block (whole file if no markers). Raises ValueError if malformed."""
    if not isinstance(source, str):
        raise ValueError("source must be text")
    i, j = source.find(BEGIN), source.find(END)
    if i >= 0:
        if j < i:
            raise ValueError("PARAMETERS block not terminated (file half-written?)")
        source = source[source.index("\n", i) + 1:j]
    out = {}
    for line in source.splitlines():
        s = line.split("//", 1)[0].strip()
        if not s:
            continue
        m = _ASSIGN.match(s)
        if not m:
            raise ValueError(f"cannot parse line: {line.strip()!r}")
        name, expr = m.group(1), m.group(2).strip()
        try:
            v = _eval(ast.parse(expr, mode="eval"))
        except Exception:
            raise ValueError(f"{name}: not a number: {expr!r}")
        out[name] = round(v, 6)
    return out


def to_enclosure(params: dict, board: dict, base_enclosure: dict) -> dict:
    """Apply parameter values onto the base enclosure's structure. Raises KeyError if a parameter is missing."""
    enc = copy.deepcopy(base_enclosure)
    r = lambda k: round(float(params[k]), 3)  # noqa: E731
    enc["wall"] = r("wall")
    enc["standoff_h"] = r("standoff_h")
    enc["cavity"] = {"w": r("cavity_w"), "d": r("cavity_d"), "h": r("cavity_h")}
    enc["board_origin"] = {"x": r("board_origin_x"), "y": r("board_origin_y")}
    for s in enc.get("standoffs", []):
        pre = standoff_prefix(s["hole"])
        for k in STANDOFF_KEYS:
            s[k] = r(f"{pre}_{k}")
    for o in enc.get("openings", []):
        pre = opening_prefix(board, o["connector"])
        for k in OPENING_KEYS:
            o[k] = r(f"{pre}_{k}")
    return enc
