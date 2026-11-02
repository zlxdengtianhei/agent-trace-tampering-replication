"""Classify the gateway's "input differs" findings of each run (report.json, final trace version).

compare.py lists every tool call whose recorded input in the trace differs from the input the model asked for.
Some of these differences are made by the harness itself before it runs the call: Claude Code drops a leading
`cd <dir> &&` when <dir> is already the shell's directory, and records (and runs) the shortened command; it also
records default fields the model left out (Edit's `replace_all: false`). This script splits the findings into
kinds; it reads report.json only and does not change any verdict.

Usage: python3 -I analysis/gw_input_diffs.py runs/<run>... [--json out.json]
Added on 11-02, after the analysis freeze of 10-31; it only reads compare.py's output.
"""
import argparse
import ast
import json
import os
import re
import sys

CD_PREFIX = re.compile(r"^\s*cd\s+(\S+)\s*&&\s*(.*)$", re.S)
FILE_FIELDS = ("content", "new_string", "old_string", "edits")


def as_dict(v):
    if isinstance(v, dict) or v is None:
        return v
    try:
        return ast.literal_eval(v)
    except (ValueError, SyntaxError):
        return {"_raw": v}


def kind(asked, trace):
    a, t = as_dict(asked), as_dict(trace)
    if t is None:
        return "call removed from the trace"
    if isinstance(a, dict) and isinstance(t, dict):
        if "command" in a and "command" in t:
            m = CD_PREFIX.match(a["command"])
            if m and m.group(2).strip() == t["command"].strip():
                return "harness dropped a leading cd"
            return "command text differs"
        if all(t.get(k) == v for k, v in a.items()) and set(t) - set(a):
            return "harness added default fields"
        if any(a.get(f) != t.get(f) for f in FILE_FIELDS if f in a or f in t):
            return "file contents differ"
    return "other input differs"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--json")
    a = ap.parse_args()
    rows = []
    for run in a.runs:
        p = os.path.join(run, "report.json")
        if not os.path.exists(p):
            continue
        g = json.load(open(p)).get("gateway", {}).get("versions", {}).get("final", {})
        counts = {}
        for x in g.get("input_differs", []):
            k = kind(x.get("asked"), x.get("trace"))
            counts[k] = counts.get(k, 0) + 1
        rows.append({"run": os.path.basename(run.rstrip("/")), "input_differs": counts,
                     "result_differs": len(g.get("result_differs", []))})
        print(rows[-1]["run"], json.dumps(counts, ensure_ascii=False), "result_differs", rows[-1]["result_differs"])
    if a.json:
        with open(a.json, "w") as f:
            json.dump(rows, f, indent=1, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
