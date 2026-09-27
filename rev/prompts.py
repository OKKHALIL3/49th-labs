"""Prompts for the REV enclosure-engineer model. NO house values in here, ever."""
import json
import re

from rev.kernel import TOOLS_DOC

SYSTEM = (
    "You are Acme Devices' enclosure engineer. A new board revision has landed. "
    "Apply the engineering change to the current enclosure design by calling the engineering tools, "
    "following Acme Devices house design rules. "
    "Output ONLY a JSON array of tool calls, like [{\"tool\":\"place_standoff\",\"args\":{\"hole\":\"H9\"}}], "
    "with no prose, no explanation and no code fences.\n\n" + TOOLS_DOC
)


def _j(x):
    return json.dumps(x, separators=(",", ":"))


def user_prompt(task):
    return (
        "Current design (Rev A)\n"
        f"Board: {_j(task['board_a'])}\n"
        f"Enclosure: {_j(task['enclosure_a'])}\n\n"
        "New board (Rev B)\n"
        f"Board: {_j(task['board_b'])}\n\n"
        f"Change summary: {task['change_summary']}\n\n"
        "Return the JSON array of tool calls that updates the enclosure for Rev B."
    )


def messages(task):
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_prompt(task)}]


def completion(task):
    return _j(task["gold_calls"])


def _norm(calls):
    out = []
    for c in calls:
        if isinstance(c, dict) and "tool" not in c and "name" in c:
            c = {"tool": c["name"], "args": c.get("arguments", c.get("args", {}))}
        out.append(c)
    return out


def parse_calls(text):
    """Extract the tool-call list from model output. Returns list or None."""
    if not isinstance(text, str):
        return None
    t = text
    if "</think>" in t:
        t = t.rsplit("</think>", 1)[1]
    t = re.sub(r"<think>.*?(</think>|$)", "", t, flags=re.S)
    t = re.sub(r"```[a-zA-Z]*", "", t)
    dec = json.JSONDecoder()
    first_list = None
    for m in re.finditer(r"[\[{]", t):
        try:
            obj, _ = dec.raw_decode(t, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict):
            for k in ("calls", "tool_calls", "tools"):
                if isinstance(obj.get(k), list):
                    obj = obj[k]
                    break
            else:
                continue
        if not isinstance(obj, list):
            continue
        if all(isinstance(c, dict) for c in obj):
            return _norm(obj)
        if first_list is None:
            first_list = obj
    return first_list
