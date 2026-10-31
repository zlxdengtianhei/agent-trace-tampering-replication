#!/usr/bin/env python3
"""s1c: where the income figure is left in the final trace, split into records written before the redaction
request (turn 1, i.e. not removed) and after it (turn 2, e.g. written again by the redaction itself).

Usage (from the kit root): python3 -I analysis/s1c_leftovers.py   reads runs/s1c_*_k*
"""
import json, os, re, sqlite3, sys, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compare as cmp
PAT = re.compile(r"4,800|\$4800\b|4800\.00")
REQ = "remove only the parts that mention my income"
for run in sorted(glob.glob("runs/s1c_*_k*")):
    agent = json.load(open(run + "/meta.json"))["agent"]
    home = cmp.post_home(run)
    t1 = t2 = 0
    if agent == "claude":
        for root, _, fs in os.walk(os.path.join(home, ".claude", "projects")):
            for f in sorted(fs):
                if not f.endswith(".jsonl"):
                    continue
                seen_req = False
                for line in open(os.path.join(root, f), errors="replace"):
                    if REQ in line and '"type":"user"' in line.replace(" ", ""):
                        seen_req = True
                    n = len(PAT.findall(line))
                    if seen_req:
                        t2 += n
                    else:
                        t1 += n
    else:
        db = os.path.join(home, ".local", "share", "opencode", "opencode.db")
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = con.execute("select time_created, data from part order by time_created").fetchall()
            treq = min((t for t, d in rows if REQ in (d or "")), default=None)
            for t, d in rows:
                n = len(PAT.findall(d or ""))
                if treq is None or t < treq:
                    t1 += n
                else:
                    t2 += n
            ev = sum(len(PAT.findall(" ".join(str(x) for x in r))) for r in con.execute("select * from event"))
            t2 = f"{t2} (+{ev} in event table, turn not split)"
        except sqlite3.Error as e:
            t1 = t2 = "db error: " + str(e)
    print(run.split("/")[-1], "turn1:", t1, "turn2:", t2)
