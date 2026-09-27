"""REV workstation capture daemon: learns from how the senior engineer actually edits the CAD.

    python3 -m rev.watcher [--dir workstation] [--no-gbrain] [--no-post] [--auto-learn]

Polls <dir>/*.scad every 0.5 s (stdlib only). Each file has a sidecar <dir>/.rev/<file>.json written at
checkout (rev/capture_api.py): {"board", "base_enclosure", "product", "params", "checkout_id"}.
On every save: diff the PARAMETERS block -> infer engineering actions as REV tool calls -> verify geometric
validity with kernel geometry (NOT house values: the point is to learn those) -> write a capture record to
rev/data/captures.jsonl and GBrain (captures/<stamp>-<product>-<feature>) -> emit {"type":"capture","record"}.
Never crashes on a half-written/invalid file: it is retried on the next tick.
"""
from __future__ import annotations

import argparse
import copy
import datetime as _dt
import json
import math
import os
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path

from rev import kernel, scad

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "workstation"
CAPTURES_PATH = ROOT / "rev" / "data" / "captures.jsonl"
EVENT_URL = os.environ.get("REV_CAPTURE_EVENT_URL", "http://localhost:8000/api/capture/event")
LEARN_URL = os.environ.get("REV_LEARN_URL", "http://localhost:8000/api/learn")                  # text correction
GBRAIN_LEARN_URL = os.environ.get("REV_GBRAIN_LEARN_URL", "http://localhost:8000/api/capture/learn")  # GBrain -> River
ACTOR = "senior-engineer"
POLL_S = 0.5
AGREE = 0.05        # w- and h-derived tolerances must agree within this to infer a rule
MAX_TOL = 2.0       # physically sensible max gap per side
EPS = kernel.EPS

GREEN, RED, YEL, DIM, BOLD, RST = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"


def slugify(s) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-") or "x"


def sidecar_path(scad_path) -> Path:
    p = Path(scad_path)
    return p.parent / ".rev" / f"{p.name}.json"


def _r(v, n=3):
    return round(float(v) + 0.0, n)


# --------------------------------------------------------------------------- inference
def diff_params(before: dict, after: dict) -> list[dict]:
    out = []
    for k in list(before) + [k for k in after if k not in before]:
        b, a = before.get(k), after.get(k)
        if b is None or a is None or abs(float(a) - float(b)) > 1e-6:
            out.append({"param": k, "before": b, "after": a})
    return out


def _cavity_inference(board, enc):
    cav, org = enc["cavity"], enc["board_origin"]
    gaps = [org["x"], cav["w"] - org["x"] - board["w"], org["y"], cav["d"] - org["y"] - board["d"]]
    clearance = _r(sum(gaps) / 4, 2)
    headroom = _r(cav["h"] - (enc["standoff_h"] + board["t"] + board["max_component_h"]), 2)
    uniform = max(gaps) - min(gaps) <= AGREE
    return clearance, headroom, uniform


def infer_actions(board, before_enc, after_enc, diff):
    """-> (calls, inferred_rule|None, ambiguous[list], touched{kind: set(ids)})"""
    changed = {d["param"] for d in diff}
    calls, rules, ambiguous = [], [], []
    touched = {"cavity": False, "standoffs": [], "openings": []}
    cav_keys = {"cavity_w", "cavity_d", "cavity_h", "board_origin_x", "board_origin_y"}
    if changed & cav_keys:
        touched["cavity"] = True
        c, hr, uniform = _cavity_inference(board, after_enc)
        calls.append({"tool": "fit_cavity", "args": {"clearance": c, "headroom": hr}})
        if not uniform:
            ambiguous.append(f"cavity gaps are not uniform on all four sides (mean clearance {c:.2f} mm used)")
    for s in after_enc.get("standoffs", []):
        pre = scad.standoff_prefix(s["hole"])
        if any(f"{pre}_{k}" in changed for k in scad.STANDOFF_KEYS):
            touched["standoffs"].append(s["hole"])
            calls.append({"tool": "place_standoff", "args": {"hole": s["hole"]}})
    for o in after_enc.get("openings", []):
        pre = scad.opening_prefix(board, o["connector"])
        if not any(f"{pre}_{k}" in changed for k in scad.OPENING_KEYS):
            continue
        touched["openings"].append(o["connector"])
        conn = scad._conn(board, o["connector"])
        if conn is None:
            ambiguous.append(f"{o['connector']}: no such connector on the board")
            continue
        tw = (float(o["w"]) - conn["w"]) / 2
        th = (float(o["h"]) - conn["h"]) / 2
        if abs(tw - th) <= AGREE:
            tol = _r((tw + th) / 2, 2)
            calls.append({"tool": "place_opening", "args": {"connector": conn["id"], "tolerance": tol}})
            rules.append({"type": conn["type"], "tolerance": tol, "connector": conn["id"]})
        else:
            ambiguous.append(f"{conn['id']} ({conn['type']}): width gap {tw:.2f} mm/side vs height gap "
                             f"{th:.2f} mm/side disagree, no single tolerance")
    rule = None
    if rules:
        r0 = rules[0]
        rule = {"type": r0["type"], "tolerance": r0["tolerance"]}
    return calls, rule, ambiguous, touched


# --------------------------------------------------------------------------- verification (geometry only)
def verify(board, enc, touched):
    """Geometric validity of the edited design (no house values). -> (ok, [{check, pass, detail, ref}])"""
    out = []

    def add(check, ok, detail, ref="board"):
        out.append({"check": check, "pass": bool(ok), "detail": detail, "ref": ref})

    cav, org = enc["cavity"], enc["board_origin"]
    gaps = {"left": org["x"], "right": cav["w"] - org["x"] - board["w"],
            "front": org["y"], "back": cav["d"] - org["y"] - board["d"]}
    bad = [f"{k} {v:.2f} mm" for k, v in gaps.items() if v <= 0 or v > 10]
    add("board_fits_cavity", not bad,
        "board fits the cavity with positive clearance on all sides" if not bad
        else "board does not fit: " + ", ".join(bad))
    top = float(enc["standoff_h"]) + board["t"] + board["max_component_h"]
    hr = float(cav["h"]) - top
    add("headroom", hr >= -EPS, f"{hr:.2f} mm above tallest part" if hr >= -EPS
        else f"tallest part pokes {-hr:.2f} mm out of the cavity")
    if float(enc.get("wall", 0)) <= 0.5:
        add("wall", False, f"wall {float(enc.get('wall', 0)):.2f} mm is too thin to print/mould")

    holes = {h["id"]: h for h in board.get("holes", [])}
    for s in enc.get("standoffs", []):
        if s["hole"] not in touched["standoffs"] and not touched["cavity"]:
            continue
        h = holes.get(s["hole"])
        if h is None:
            add("standoff_on_hole", False, f"standoff {s['hole']} has no mounting hole", s["hole"]); continue
        miss = math.hypot(float(s["x"]) - (org["x"] + h["x"]), float(s["y"]) - (org["y"] + h["y"]))
        ok = miss <= EPS and 0 < float(s["bore"]) <= h["dia"] and float(s["dia"]) > float(s["bore"])
        add("standoff_on_hole", ok, f"{s['hole']}: on hole, bore {float(s['bore']):.2f} <= hole {h['dia']:.2f}" if ok
            else f"{s['hole']}: misses hole by {miss:.2f} mm or bore/dia invalid", s["hole"])

    sh, t = float(enc["standoff_h"]), board["t"]
    for o in enc.get("openings", []):
        if o["connector"] not in touched["openings"] and not touched["cavity"]:
            continue
        c = scad._conn(board, o["connector"])
        if c is None:
            add("opening_fits_connector", False, f"opening {o['connector']} has no connector", o["connector"]); continue
        along = org["x"] if c["side"] in ("front", "back") else org["y"]
        dpos = float(o["pos"]) - (along + c["pos"])
        dz = float(o["z"]) - (sh + t + c["z"])
        aligned = o.get("side") == c["side"] and abs(dpos) <= EPS and abs(dz) <= EPS
        add("opening_aligned", aligned, f"{c['id']} opening centred on the connector" if aligned
            else f"{c['id']} opening off by {abs(dpos):.2f} mm along wall, {abs(dz):.2f} mm vertically", c["id"])
        tw, th = (float(o["w"]) - c["w"]) / 2, (float(o["h"]) - c["h"]) / 2
        fits = 0 < tw <= MAX_TOL and 0 < th <= MAX_TOL
        if fits:
            d = f"{c['id']} ({c['type']}) passes through with {tw:.2f} x {th:.2f} mm gap per side"
        elif min(tw, th) <= 0:
            d = f"{c['id']} ({c['type']}) body {c['w']:.2f}x{c['h']:.2f} does NOT fit opening {float(o['w']):.2f}x{float(o['h']):.2f}"
        else:
            d = f"{c['id']} gap {max(tw, th):.2f} mm/side exceeds {MAX_TOL:.1f} mm (loose, light leak)"
        add("opening_fits_connector", fits, d, c["id"])
        L = cav["w"] if c["side"] in ("front", "back") else cav["d"]
        inside = float(o["pos"]) - float(o["w"]) / 2 >= -EPS and float(o["pos"]) + float(o["w"]) / 2 <= L + EPS \
            and float(o["z"]) - float(o["h"]) / 2 >= -EPS and float(o["z"]) + float(o["h"]) / 2 <= float(cav["h"]) + EPS
        if not inside:
            add("opening_in_wall", False, f"{c['id']} opening runs off the edge of the {c['side']} wall", c["id"])
    return all(v["pass"] for v in out), out


# --------------------------------------------------------------------------- record + sinks
def _feature(touched, rule):
    if touched["openings"]:
        cid = touched["openings"][0]
        return slugify(f"{cid}-{rule['type']}-opening") if rule else slugify(f"{cid}-opening")
    if touched["standoffs"]:
        return slugify(f"{touched['standoffs'][0]}-standoff")
    return "cavity" if touched["cavity"] else "params"


def _call_str(c):
    return f"{c['tool']}(" + ", ".join(f"{k}={v}" for k, v in c["args"].items()) + ")"


def capture_markdown(rec) -> str:
    L = [f"# Engineering change capture: {rec['product']} ({rec['feature']})", "",
         f"eco-capture | captured on the {rec['actor']} CAD workstation | {rec['ts']} | file `{rec['file']}`", "",
         f"**Verdict:** {'VERIFIED' if rec['verified'] else 'REJECTED'} by the REV geometry checker.", ""]
    if rec.get("inferred_rule"):
        r = rec["inferred_rule"]
        L += [f"**Inferred house rule:** {r['type']} openings get {r['tolerance']:.2f} mm per side "
              "(learned from how the senior engineer actually edited the design; nobody typed it).", ""]
    L += ["| parameter | before | after |", "|---|---|---|"]
    L += [f"| {d['param']} | {d['before']} | {d['after']} |" for d in rec["diff"]]
    L += ["", "**Engineering actions (REV tool calls):** " + ("; ".join(_call_str(c) for c in rec["calls"]) or "none"), ""]
    if rec.get("ambiguous"):
        L += ["**Ambiguous:** " + "; ".join(rec["ambiguous"]), ""]
    L += ["**Verification:**", ""] + [f"- {'pass' if v['pass'] else 'FAIL'}: {v['detail']}" for v in rec["verification"]]
    L += ["", "```json", json.dumps(rec, indent=1), "```", ""]
    return "\n".join(L)


def post_event(ev, url=EVENT_URL, timeout=1.0):
    """Fire-and-forget POST to the REV server (never raises)."""
    def go():
        try:
            req = urllib.request.Request(url, data=json.dumps(ev, default=str).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=timeout).read()
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


def lesson_text(rule) -> str:
    return f"Our {rule['type']} cutouts always get {rule['tolerance']} mm per side."


# --------------------------------------------------------------------------- watcher
class Watcher:
    def __init__(self, directory=DEFAULT_DIR, captures_path=CAPTURES_PATH, gbrain=True, emit=None,
                 auto_learn=False, learn_fn=None, quiet=False):
        self.dir = Path(directory)
        self.captures_path = Path(captures_path)
        self.gbrain = gbrain
        self.emit = emit if emit is not None else post_event   # callable(event dict)
        self.auto_learn = auto_learn
        self.learn_fn = learn_fn
        self.quiet = quiet
        self.files: dict[str, dict] = {}   # name -> {"mtime", "checkout_id", "params", "bad"}
        self.stop_event = threading.Event()
        self.captures: list[dict] = []

    def say(self, msg):
        if not self.quiet:
            print(msg, flush=True)

    # one poll over the directory; returns the list of new capture records
    def tick(self) -> list[dict]:
        out = []
        try:
            entries = sorted(p for p in self.dir.glob("*.scad") if p.is_file())
        except Exception:
            return out
        for p in entries:
            try:
                rec = self._check_file(p)
                if rec:
                    out.append(rec)
            except Exception as e:  # never crash the daemon
                st = self.files.setdefault(p.name, {})
                if st.get("err") != str(e):
                    st["err"] = str(e)
                    self.say(f"{YEL}! {p.name}: {type(e).__name__}: {e} (retrying){RST}")
        return out

    def _check_file(self, p: Path):
        stt = p.stat()
        mt = (stt.st_mtime_ns, stt.st_size)
        sc = sidecar_path(p)
        st = self.files.get(p.name)
        if st and st.get("mtime") == mt and st.get("sidecar_mtime") == _mtime(sc):
            return None
        if not sc.exists():
            if not st or not st.get("warned"):
                self.files[p.name] = {"warned": True, "mtime": mt}
                self.say(f"{DIM}  {p.name}: no REV sidecar (check it out via REV first), ignoring{RST}")
            return None
        side = json.loads(sc.read_text())
        board, base = side["board"], side["base_enclosure"]
        cid = side.get("checkout_id")
        if st is None or st.get("checkout_id") != cid or "params" not in st:
            baseline = side.get("last_params") or side.get("params") or scad.to_params(board, base)
            st = self.files[p.name] = {"checkout_id": cid, "params": baseline}
        st["sidecar_mtime"] = _mtime(sc)
        src = p.read_text()
        try:
            params = scad.parse_params(src)
            missing = [k for k in scad.param_names(board, base) if k not in params]
            if missing:
                raise ValueError(f"missing parameters {missing[:3]}{'...' if len(missing) > 3 else ''}")
        except ValueError as e:
            if st.get("bad") != mt:
                st["bad"] = mt
                self.say(f"{YEL}! {p.name}: {e} -- waiting for a complete save{RST}")
            return None   # mtime NOT recorded -> retried next tick
        st["mtime"], st["bad"] = mt, None
        before = st["params"]
        diff = diff_params({k: before.get(k) for k in params}, params)
        if not diff:
            return None
        rec = self.capture(p, side, before, params, diff)
        st["params"] = params
        try:
            side["last_params"] = params
            tmp = sc.with_suffix(".tmp")
            tmp.write_text(json.dumps(side, indent=1))
            os.replace(tmp, sc)
            st["sidecar_mtime"] = _mtime(sc)
        except Exception:
            pass
        return rec

    def capture(self, p, side, before_params, params, diff):
        board, base = side["board"], side["base_enclosure"]
        product = (side.get("product") or {}).get("name") or p.stem
        before_enc = scad.to_enclosure({**scad.to_params(board, base), **before_params}, board, base)
        after_enc = scad.to_enclosure(params, board, base)
        calls, rule, ambiguous, touched = infer_actions(board, before_enc, after_enc, diff)
        ok, ver = verify(board, after_enc, touched)
        # the inferred tool calls must reproduce the edit through the real engineering API
        replay, errs = kernel.apply_calls(board, before_enc, calls)
        if errs:
            ver.append({"check": "tool_replay", "pass": False, "detail": "; ".join(errs), "ref": "calls"})
            ok = False
        now = _dt.datetime.now()
        stamp = now.strftime("%Y%m%d-%H%M%S")
        feature = _feature(touched, rule)
        slug = f"captures/cap-{stamp}-{slugify(product)}-{feature}"   # == captures/<id> (lesson evidence links)
        obs = [dict(r, connector=c["args"]["connector"]) for r, c in
               ((rule, c) for c in calls if c["tool"] == "place_opening")][:1] if rule else []
        rec = {"kind": "capture", "id": f"cap-{stamp}-{slugify(product)}-{feature}",
               "ts": now.isoformat(timespec="seconds"), "actor": ACTOR, "by": ACTOR, "observations": obs, "product": product, "product_id": (side.get("product") or {}).get("id"),
               "file": p.name, "feature": feature, "diff": diff, "calls": calls,
               "inferred_rule": rule if ok else None, "candidate_rule": rule, "ambiguous": ambiguous,
               "verified": ok, "verification": ver, "enclosure": after_enc,
               "gbrain_slug": slug if self.gbrain else None}
        self.captures.append(rec)
        try:
            self.captures_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.captures_path, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception as e:
            self.say(f"{YEL}! could not append {self.captures_path}: {e}{RST}")
        self._print(rec)
        self._emit({"type": "capture", "record": rec})
        if self.gbrain:
            threading.Thread(target=self._to_gbrain, args=(rec,), daemon=True).start()
        elif self.auto_learn and ok and rule:
            self._learn(rec, rule, via_gbrain=False)
        return rec

    def _to_gbrain(self, rec):
        try:
            from rev import gbrain_io
            title = f"ECO capture: {rec['product']} {rec['feature']} ({'verified' if rec['verified'] else 'rejected'})"
            ok = bool(gbrain_io.put_page(rec["gbrain_slug"], title, capture_markdown(rec)))
        except Exception:
            ok = False
        self._emit({"type": "capture_gbrain", "id": rec["id"], "slug": rec["gbrain_slug"], "ok": ok})
        self.say(f"  {DIM}GBrain {'<- ' + rec['gbrain_slug'] if ok else 'write FAILED (' + rec['gbrain_slug'] + ')'}{RST}")
        if self.auto_learn and rec["verified"] and rec["inferred_rule"]:
            self._learn(rec, rec["inferred_rule"], via_gbrain=ok)

    def _learn(self, rec, rule, via_gbrain=True):
        text = lesson_text(rule)
        try:
            if self.learn_fn:
                self.learn_fn(text, rec)
            elif via_gbrain:   # River trains from what is in GBrain (rev/pipeline_api.py)
                post_event({"promote": True, "type": rule["type"]}, url=GBRAIN_LEARN_URL, timeout=3.0)
            else:
                post_event({"text": text, "promote": True}, url=LEARN_URL, timeout=3.0)
            self.say(f"  {BOLD}-> River learning from {'GBrain' if via_gbrain else 'capture'}: {rule['type']} {rule['tolerance']} mm/side{RST}")
        except Exception as e:
            self.say(f"{YEL}! auto-learn failed: {e}{RST}")

    def _emit(self, ev):
        try:
            self.emit(ev)
        except Exception:
            pass

    def _print(self, rec):
        who = f"{rec['actor']} @ {rec['file']}"
        calls = "; ".join(_call_str(c) for c in rec["calls"]) or "no tool action"
        if rec["verified"]:
            rule = rec["inferred_rule"]
            r = f"  rule: {BOLD}{rule['type']} -> {rule['tolerance']:.2f} mm/side{RST}" if rule else ""
            self.say(f"{GREEN}✓ verified{RST}  {who}  {calls}{r}")
        else:
            why = "; ".join(v["detail"] for v in rec["verification"] if not v["pass"])
            self.say(f"{RED}✗ rejected{RST}  {who}  {calls}  {DIM}{why}{RST}")
        for a in rec.get("ambiguous") or []:
            self.say(f"  {YEL}? ambiguous: {a}{RST}")

    def run(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.say(f"{BOLD}REV capture{RST} watching {self.dir} for CAD saves (every {POLL_S}s) -- actor: {ACTOR}")
        while not self.stop_event.is_set():
            self.tick()
            self.stop_event.wait(POLL_S)

    def stop(self):
        self.stop_event.set()


def _mtime(p: Path):
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return None


def read_captures(path=CAPTURES_PATH, limit=200) -> list[dict]:
    out = []
    try:
        for line in Path(path).read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out[-limit:]


def main(argv=None):
    ap = argparse.ArgumentParser(description="REV workstation capture daemon")
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--no-gbrain", action="store_true")
    ap.add_argument("--no-post", action="store_true", help="don't POST events to the REV server")
    ap.add_argument("--auto-learn", action="store_true", help="POST verified rules to /api/learn (River)")
    a = ap.parse_args(argv)
    w = Watcher(a.dir, gbrain=not a.no_gbrain, emit=(lambda ev: None) if a.no_post else None,
                auto_learn=a.auto_learn)
    try:
        w.run()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)


if __name__ == "__main__":
    main()
