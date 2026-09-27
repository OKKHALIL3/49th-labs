"""REV kernel: board/enclosure model, engineering tools, deterministic checker.

House rules live ONLY here (and in the generator via this module). Never put them in a prompt.
"""
import copy
import math

EPS = 0.051

HOUSE = {
    "clearance": 1.5,
    "headroom": 3.0,
    "standoff_h": 5.0,
    "wall": 2.0,
    "standoff_dia": 5.5,
    "bore_delta": 0.5,  # bore = hole.dia - bore_delta
    "tolerance": {"usb_c": 0.4, "hdmi": 0.6, "rj45": 0.3, "barrel": 0.8, "sd": 1.0,
                  "usb_a": 0.5},  # usb_a: never in history; taught live via rev/learn.py
}

CONNECTOR_SPECS = {
    "usb_c": {"w": 8.94, "h": 3.26, "z": 1.63},
    "hdmi": {"w": 15.0, "h": 5.6, "z": 2.8},
    "rj45": {"w": 16.0, "h": 13.5, "z": 6.75},
    "barrel": {"w": 9.0, "h": 11.0, "z": 5.5},
    "sd": {"w": 12.0, "h": 2.0, "z": 1.0},
    "usb_a": {"w": 13.14, "h": 5.72, "z": 2.86},
}

SIDES = ("front", "back", "left", "right")

TOOLS_DOC = """Engineering tools. Each call is {"tool": "<name>", "args": {...}}. All lengths in mm.
Coordinates: the board sits in the enclosure cavity at board_origin; holes/connectors are in board-local coords.
- fit_cavity(clearance, headroom): resize the cavity around the current board. clearance = gap between each board edge and the inner wall (all four sides); headroom = free space between the top of the tallest component and the top of the cavity. Sets board_origin = (clearance, clearance).
- place_standoff(hole): add or update the standoff under mounting hole id `hole`, aligned to the hole at the current board_origin (diameter and screw bore are set automatically from the hole).
- remove_standoff(hole): delete the standoff for hole id `hole`.
- place_opening(connector, tolerance): add or update the wall cut-out for connector id `connector` on the connector's side, centred on the connector at the current board_origin; tolerance = extra gap added on EACH side of the connector body (width and height).
- remove_opening(connector): delete the opening for connector id `connector`.
Standoffs and openings are placed relative to the current board_origin, so if fit_cavity changes the origin they must be placed again.
Only issue the calls needed to make the enclosure correct for the new board; features that are still correct should be left alone."""


class ToolError(Exception):
    pass


def _r(v):
    return round(float(v) + 0.0, 3)


def _num(args, key, nonneg=True):
    if key not in args:
        raise ToolError(f"missing arg '{key}'")
    v = args[key]
    if isinstance(v, bool):
        raise ToolError(f"arg '{key}' must be a number")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ToolError(f"arg '{key}' must be a number, got {v!r}")
    if not math.isfinite(f):
        raise ToolError(f"arg '{key}' must be finite")
    if nonneg and f < 0:
        raise ToolError(f"arg '{key}' must be >= 0")
    if f > 1000:
        raise ToolError(f"arg '{key}' is unreasonably large")
    return f


def _id(args, key):
    if key not in args:
        raise ToolError(f"missing arg '{key}'")
    v = args[key]
    if not isinstance(v, str) or not v:
        raise ToolError(f"arg '{key}' must be an id string")
    return v


def _find(items, key, val):
    for i, it in enumerate(items):
        if it.get(key) == val:
            return i
    return -1


def empty_enclosure():
    return {
        "wall": HOUSE["wall"],
        "standoff_h": HOUSE["standoff_h"],
        "cavity": {"w": 0.0, "d": 0.0, "h": 0.0},
        "board_origin": {"x": 0.0, "y": 0.0},
        "standoffs": [],
        "openings": [],
    }


def _upsert(items, key, val, obj):
    i = _find(items, key, val)
    if i >= 0:
        items[i] = obj
    else:
        items.append(obj)


def apply_call(board, enc, call):
    """Apply one tool call. Pure: returns a new enclosure dict. Raises ToolError."""
    if not isinstance(call, dict):
        raise ToolError("call must be an object")
    tool = call.get("tool", call.get("name"))
    if not isinstance(tool, str):
        raise ToolError("call missing 'tool'")
    args = call.get("args", call.get("arguments"))
    if args is None:
        args = {k: v for k, v in call.items() if k not in ("tool", "name")}
    if not isinstance(args, dict):
        raise ToolError("'args' must be an object")
    enc = copy.deepcopy(enc) if enc else empty_enclosure()
    enc.setdefault("standoffs", [])
    enc.setdefault("openings", [])
    enc.setdefault("board_origin", {"x": 0.0, "y": 0.0})
    enc.setdefault("cavity", {"w": 0.0, "d": 0.0, "h": 0.0})
    sh = float(enc.get("standoff_h", HOUSE["standoff_h"]))
    ox, oy = float(enc["board_origin"]["x"]), float(enc["board_origin"]["y"])

    if tool == "fit_cavity":
        c = _num(args, "clearance")
        hr = _num(args, "headroom")
        enc["cavity"] = {
            "w": _r(board["w"] + 2 * c),
            "d": _r(board["d"] + 2 * c),
            "h": _r(sh + board["t"] + board["max_component_h"] + hr),
        }
        enc["board_origin"] = {"x": _r(c), "y": _r(c)}
    elif tool == "place_standoff":
        hid = _id(args, "hole")
        i = _find(board.get("holes", []), "id", hid)
        if i < 0:
            raise ToolError(f"unknown hole '{hid}'")
        h = board["holes"][i]
        _upsert(enc["standoffs"], "hole", hid, {
            "hole": hid,
            "x": _r(ox + h["x"]),
            "y": _r(oy + h["y"]),
            "dia": HOUSE["standoff_dia"],
            "bore": _r(h["dia"] - HOUSE["bore_delta"]),
        })
    elif tool == "remove_standoff":
        hid = _id(args, "hole")
        i = _find(enc["standoffs"], "hole", hid)
        if i < 0:
            raise ToolError(f"no standoff for hole '{hid}'")
        enc["standoffs"].pop(i)
    elif tool == "place_opening":
        cid = _id(args, "connector")
        tol = _num(args, "tolerance")
        i = _find(board.get("connectors", []), "id", cid)
        if i < 0:
            raise ToolError(f"unknown connector '{cid}'")
        c = board["connectors"][i]
        along = ox if c["side"] in ("front", "back") else oy
        _upsert(enc["openings"], "connector", cid, {
            "connector": cid,
            "side": c["side"],
            "pos": _r(along + c["pos"]),
            "z": _r(sh + board["t"] + c["z"]),
            "w": _r(c["w"] + 2 * tol),
            "h": _r(c["h"] + 2 * tol),
        })
    elif tool == "remove_opening":
        cid = _id(args, "connector")
        i = _find(enc["openings"], "connector", cid)
        if i < 0:
            raise ToolError(f"no opening for connector '{cid}'")
        enc["openings"].pop(i)
    else:
        raise ToolError(f"unknown tool '{tool}'")
    return enc


def apply_calls(board, enc, calls):
    """Apply calls in order, skipping bad ones. Returns (enc, errors)."""
    errors = []
    enc = copy.deepcopy(enc) if enc else empty_enclosure()
    if not isinstance(calls, list):
        return enc, ["calls must be a list"]
    for i, call in enumerate(calls):
        try:
            enc = apply_call(board, enc, call)
        except ToolError as e:
            name = call.get("tool", "?") if isinstance(call, dict) else "?"
            errors.append(f"call {i} ({name}): {e}")
    return enc, errors


def enclosure_for(board):
    """The house-correct enclosure for a board."""
    enc = apply_call(board, empty_enclosure(), {"tool": "fit_cavity", "args": {
        "clearance": HOUSE["clearance"], "headroom": HOUSE["headroom"]}})
    for h in board.get("holes", []):
        enc = apply_call(board, enc, {"tool": "place_standoff", "args": {"hole": h["id"]}})
    for c in board.get("connectors", []):
        enc = apply_call(board, enc, {"tool": "place_opening", "args": {
            "connector": c["id"], "tolerance": HOUSE["tolerance"][c["type"]]}})
    return enc


def _close(a, b):
    return abs(float(a) - float(b)) <= EPS


def _f(v):
    return f"{float(v):.2f}"


def check(board, enc):
    """Deterministic checker. Returns list of {id,label,pass,detail,refs} in fixed order."""
    out = []
    cav = enc.get("cavity", {}) or {}
    org = enc.get("board_origin", {}) or {}
    ox, oy = float(org.get("x", 0)), float(org.get("y", 0))
    cw, cd, chh = float(cav.get("w", 0)), float(cav.get("d", 0)), float(cav.get("h", 0))
    sh = float(enc.get("standoff_h", HOUSE["standoff_h"]))
    cl = HOUSE["clearance"]

    # 1. cavity_fit
    gaps = {"left": ox, "right": cw - ox - board["w"], "front": oy, "back": cd - oy - board["d"]}
    bad = [k for k, g in gaps.items() if not _close(g, cl)]
    if not bad:
        detail = f"Board {_f(board['w'])}x{_f(board['d'])} fits cavity {_f(cw)}x{_f(cd)} with house clearance on all sides"
    else:
        parts = []
        for k in bad:
            g = gaps[k]
            if g < -EPS:
                parts.append(f"{k}: board overlaps wall by {_f(-g)} mm")
            else:
                parts.append(f"{k} gap {_f(g)} mm (not house clearance)")
        detail = f"Board {_f(board['w'])}x{_f(board['d'])} in cavity {_f(cw)}x{_f(cd)}: " + "; ".join(parts)
    out.append({"id": "cavity_fit", "label": "Board-to-wall clearance", "pass": not bad,
                "detail": detail, "refs": [] if not bad else ["board"]})

    # 2. cavity_height
    top = sh + board["t"] + board["max_component_h"]
    hr = chh - top
    ok = _close(hr, HOUSE["headroom"])
    if ok:
        detail = f"Cavity height {_f(chh)} mm leaves house headroom above tallest part ({_f(board['max_component_h'])} mm)"
    elif hr < -EPS:
        detail = f"Tallest part pokes {_f(-hr)} mm above cavity (height {_f(chh)} mm)"
    else:
        detail = f"Headroom {_f(hr)} mm above tallest part (not house headroom); cavity height {_f(chh)} mm"
    out.append({"id": "cavity_height", "label": "Headroom above tallest part", "pass": ok,
                "detail": detail, "refs": [] if ok else ["board"]})

    # 3. standoffs
    sos = enc.get("standoffs", []) or []
    fails, reasons = [], []
    holes = board.get("holes", [])
    for h in holes:
        i = _find(sos, "hole", h["id"])
        if i < 0:
            fails.append(h["id"]); reasons.append(f"{h['id']} has no standoff"); continue
        s = sos[i]
        dx, dy = float(s.get("x", 0)) - (ox + h["x"]), float(s.get("y", 0)) - (oy + h["y"])
        dist = math.hypot(dx, dy)
        if dist > EPS:
            fails.append(h["id"]); reasons.append(f"{h['id']} standoff misses hole by {_f(dist)} mm"); continue
        if not _close(s.get("dia", 0), HOUSE["standoff_dia"]) or not _close(s.get("bore", 0), h["dia"] - HOUSE["bore_delta"]):
            fails.append(h["id"]); reasons.append(f"{h['id']} standoff dia/bore wrong for {_f(h['dia'])} mm hole")
    ok = not fails
    detail = f"{len(holes)}/{len(holes)} mounting holes on aligned standoffs" if ok else "; ".join(reasons)
    out.append({"id": "standoffs", "label": "Standoffs on mounting holes", "pass": ok, "detail": detail, "refs": fails})

    # 4. openings
    ops = enc.get("openings", []) or []
    fails, reasons = [], []
    conns = board.get("connectors", [])
    for c in conns:
        i = _find(ops, "connector", c["id"])
        tag = f"{c['id']} ({c['type']})"
        if i < 0:
            fails.append(c["id"]); reasons.append(f"{tag} has no opening"); continue
        o = ops[i]
        if o.get("side") != c["side"]:
            fails.append(c["id"]); reasons.append(f"{tag} opening on {o.get('side')} wall, connector is on {c['side']}"); continue
        along = ox if c["side"] in ("front", "back") else oy
        dpos = float(o.get("pos", 0)) - (along + c["pos"])
        dz = float(o.get("z", 0)) - (sh + board["t"] + c["z"])
        if abs(dpos) > EPS or abs(dz) > EPS:
            msg = []
            if abs(dpos) > EPS:
                msg.append(f"off-centre by {_f(abs(dpos))} mm along wall")
            if abs(dz) > EPS:
                msg.append(f"off by {_f(abs(dz))} mm vertically")
            fails.append(c["id"]); reasons.append(f"{tag} opening " + ", ".join(msg)); continue
        tol = HOUSE["tolerance"][c["type"]]
        if not _close(o.get("w", 0), c["w"] + 2 * tol) or not _close(o.get("h", 0), c["h"] + 2 * tol):
            gw = (float(o.get("w", 0)) - c["w"]) / 2
            fails.append(c["id"]); reasons.append(f"{tag} opening {_f(o.get('w', 0))}x{_f(o.get('h', 0))} has {_f(gw)} mm gap per side (wrong tolerance for {c['type']})")
    ok = not fails
    detail = f"{len(conns)}/{len(conns)} connector openings aligned and sized to house tolerance" if ok else "; ".join(reasons)
    out.append({"id": "openings", "label": "Connector openings", "pass": ok, "detail": detail, "refs": fails})

    # 5. orphans
    hole_ids = {h["id"] for h in holes}
    conn_ids = {c["id"] for c in conns}
    orph, reasons = [], []
    for s in sos:
        if s.get("hole") not in hole_ids:
            orph.append(s.get("hole")); reasons.append(f"standoff for missing hole {s.get('hole')}")
    for o in ops:
        if o.get("connector") not in conn_ids:
            orph.append(o.get("connector")); reasons.append(f"opening for missing connector {o.get('connector')}")
    ok = not orph
    detail = "Every standoff and opening maps to a board feature" if ok else "; ".join(reasons)
    out.append({"id": "orphans", "label": "No orphan features", "pass": ok, "detail": detail, "refs": orph})
    return out


def passes(board, enc):
    return all(c["pass"] for c in check(board, enc))
