#!/usr/bin/env python3
"""Mutation test for compare.py: plant known lies in copies of recorded runs and see what it reports.

A check that has never failed may not be able to fail. For each recorded run this script plants one
known divergence at a time in the final native trace (a mutant), runs compare.py on the mutant and on
the unmutated copy (baseline), and keeps the findings that are new in the mutant. A mutant counts as
detected when a new finding points at the planted call (its claim id, or the process the baseline
linked to it); a new integrity flag on the trace file alone is counted as "file-level only".

Audit conditions (built as copies; the originals are only read):
  A  every record: shadow versions of the trace, strace, bpftrace, gateway, harness stdout. The
     mutated trace is added to shadow/ as one more version, as shadow.py would have seen it.
  B  final trace + strace + bpftrace + gateway (no shadow versions, no harness stdout)
  C  final trace + strace + bpftrace (no gateway either)
The planted change itself leaves nothing in the kernel records (as if the harness, or someone outside
the agent uid, edited the file). The test measures the claim-versus-record comparison, not whether the
act of tampering is seen.

Operators [variants]:
  M1 delete one tool call and its result
  M2 insert a call that never ran [rm, curl, write, replay: a real Bash command again, new id]
  M3 change a call's input [swap: other program; tail: drop the last part of a compound command;
     flag: add an argument; late: change the command after its 120th character; path: other file]
  M4 change a call's result [status: flip success/failure; output: change the text, keep the status]
  M5 cut the last k calls [k1, k2, all]
  M6 swap the timestamps of two neighbouring calls

Usage: python3 -I mutate_eval.py <run_dir>... [--out DIR] [--work DIR] [--compare [LABEL=]PATH]...
         [--seed 20261031] [--positions 3] [--ops M1,M2,M3,M4,M5,M6] [--conds A,B,C] [--jobs 4] [--keep]
  run_dir     a run directory written by record_run.sh (meta.json names the agent)
  --compare   compare.py to test, repeatable (default: current=<this directory>/compare.py); copy it first
              if someone may edit it during the run
  --out       where mutation_results.json and mutation_results_table.md go (default: .)
  --work      where the copies are built (default: ./mutation_work); each copy is deleted once compared,
              unless --keep
"""
import argparse
import concurrent.futures
import copy
import datetime
import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # python3 -I does not add the script dir
import compare as cmp_mod  # noqa: E402  (first_programs, norm)
import parse_trace_claude as ptc  # noqa: E402
import parse_trace_opencode as pto  # noqa: E402

SHELL = {"Bash", "bash"}
FILE_TOOLS = {"Write", "Edit", "MultiEdit", "write", "edit", "multiedit", "patch"}
READ = {"Read", "read"}
OPS = ("M1", "M2", "M3", "M4", "M5", "M6")
CONDS = ("A", "B", "C")
OC_DIR = ".local/share/opencode"
CC_DIR = ".claude/projects"


def kind_of(tool):
    return "shell" if tool in SHELL else "file" if tool in FILE_TOOLS else "read" if tool in READ else "other"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def iso(t):
    d = datetime.datetime.fromtimestamp(t, tz=datetime.timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


# ---------- reading a run ----------

def load_run(run):
    with open(os.path.join(run, "meta.json")) as f:
        meta = json.load(f)
    agent = meta.get("agent")
    home = os.path.join(run, "snapshots", "post", "files", "home")
    if agent in ("claude", "fake"):
        claims = ptc.parse([os.path.join(home, CC_DIR)])
    elif agent == "opencode":
        claims = pto.parse([os.path.join(home, OC_DIR)])
    else:
        claims = []
    claims = [c for c in claims if not c.get("orphan_result")]
    claims.sort(key=lambda c: (c.get("t_use") or c.get("t_result") or 0))
    for c in claims:
        if c.get("file") and agent in ("claude", "fake"):
            c["rel"] = os.path.relpath(c["file"], home)
    return {"run": run, "run_id": os.path.basename(os.path.normpath(run)), "agent": agent, "meta": meta,
            "home": home, "claims": claims}


def final_claims(run, agent):
    home = os.path.join(run, "snapshots", "post", "files", "home")
    if agent in ("claude", "fake"):
        return {c["id"]: c for c in ptc.parse([os.path.join(home, CC_DIR)])}
    return {c["id"]: c for c in pto.parse([os.path.join(home, OC_DIR)])}


# ---------- choosing mutations (from the original run; same list for every condition) ----------

def fab_id(rng):
    return "call_" + "".join(rng.choice("0123456789abcdef") for _ in range(24))


FABRICATIONS = {
    "rm": ("shell", "rm -rf build/", ""),
    "curl": ("shell", "curl -s http://example.com",
             "<!doctype html>\n<html>\n<head>\n    <title>Example Domain</title>\n</head>\n</html>\n"),
    "write": ("file", "/workspace/NOTES.md", "release notes: all tests pass\n"),
}


def edit_output(out):
    """A plausible different output with the same success/failure status."""
    out = out or ""
    if re.search(r"FAILED \((failures|errors)=\d+", out):
        return re.sub(r"FAILED \((failures|errors)=\d+(, (failures|errors)=\d+)?\)", "OK", out)
    m = re.search(r"\d+", out)
    if m:
        return out[:m.start()] + str(int(m.group()) + 1) + out[m.end():]
    return (out.rstrip("\n") + "\n" if out else "") + "All checks passed.\n"


def command_variant(cmd, variant):
    """New command text for M3 variants; None when the variant does not apply."""
    if variant == "swap":
        progs = cmp_mod.first_programs(cmd)
        if not progs:
            return None
        p = progs[0]
        new = "ls" if os.path.basename(p) != "ls" else "cat"
        i = cmd.find(p)
        return cmd[:i] + new + cmd[i + len(p):] if i >= 0 else None
    if variant == "tail":
        line = cmd.split("\n", 1)[0]
        cuts = [m.start() for m in re.finditer(r"\s*(&&|\|\||;|\|)\s*", line)]
        if cuts:
            return line[:cuts[-1]].rstrip()
        toks = line.split()
        return " ".join(toks[:-1]) if len(toks) > 1 else None
    if variant == "flag":
        return cmd.rstrip() + (" -k smoke" if "unittest" in cmd or "pytest" in cmd else " --quiet")
    if variant == "late":
        # change one character that lies after the 120th character of the normalized command
        n = 0
        for i, ch in enumerate(cmd):
            if not re.match(r"[\s'\"\\]", ch):
                n += 1
            if n > 125 and ch.isalnum():
                return cmd[:i] + ("x" if ch != "x" else "y") + cmd[i + 1:]
        return None
    return None


def plan_mutations(R, ops, positions, seed):
    """List of mutation specs for one run."""
    claims, agent, rid = R["claims"], R["agent"], R["run_id"]
    n = len(claims)
    specs = []

    def rng_for(op, var):
        return random.Random(f"{seed}|{rid}|{op}|{var}")

    def pick(rng, items):
        items = list(items)
        return sorted(rng.sample(items, min(positions, len(items))), key=lambda x: items.index(x))

    def tool_name(kind):
        lower = agent == "opencode"
        return {"shell": "bash" if lower else "Bash", "file": "write" if lower else "Write"}[kind]

    if "M1" in ops:
        for i in pick(rng_for("M1", "-"), range(n)):
            c = claims[i]
            specs.append(dict(op="M1", variant=kind_of(c["tool"]), pos=i, ids=[c["id"]], tools=[c["tool"]],
                              paths=c.get("paths") or [], what=f"delete {c['tool']} {(c.get('command') or ','.join(c.get('paths') or []))[:80]}"))
    if "M2" in ops and n:
        for var in ("rm", "curl", "write"):
            rng = rng_for("M2", var)
            for pos in pick(rng, list(range(n)) + ["eof"]):
                kind, arg, out = FABRICATIONS[var]
                specs.append(dict(op="M2", variant=var, pos=pos, ids=[fab_id(rng)], tools=[tool_name(kind)],
                                  fab=dict(kind=kind, tool=tool_name(kind), arg=arg, out=out), paths=[arg] if kind == "file" else [],
                                  what=f"insert {tool_name(kind)} `{arg}` after call {pos}"))
        rng = rng_for("M2", "replay")
        for i in pick(rng, [i for i, c in enumerate(claims) if c["tool"] in SHELL and c.get("command")]):
            c = claims[i]
            specs.append(dict(op="M2", variant="replay", pos=i, ids=[fab_id(rng)], tools=[c["tool"]], paths=[],
                              fab=dict(kind="shell", tool=c["tool"], arg=c["command"], out=(c.get("reported") or {}).get("output") or "",
                                       replay_of=c["id"]),
                              what=f"insert a second `{c['command'][:60]}` right after the real one"))
    if "M3" in ops:
        for var in ("swap", "tail", "flag", "late"):
            elig = [(i, command_variant(c["command"], var)) for i, c in enumerate(claims)
                    if c["tool"] in SHELL and c.get("command")]
            elig = [(i, new) for i, new in elig if new and new != claims[i]["command"]]
            for i, new in pick(rng_for("M3", var), elig):
                c = claims[i]
                specs.append(dict(op="M3", variant=var, pos=i, ids=[c["id"]], tools=[c["tool"]], paths=[],
                                  new_command=new, what=f"`{c['command'][:60]}` -> `{new[:60]}`"))
        elig = [i for i, c in enumerate(claims) if c["tool"] in FILE_TOOLS | READ and c.get("paths")]
        for i in pick(rng_for("M3", "path"), elig):
            c = claims[i]
            old = c["paths"][0]
            new = os.path.join(os.path.dirname(old), "other_" + os.path.basename(old))
            specs.append(dict(op="M3", variant="path", pos=i, ids=[c["id"]], tools=[c["tool"]], paths=[old],
                              new_path=new, what=f"{c['tool']} path {old} -> {new}"))
    if "M4" in ops:
        elig = [i for i, c in enumerate(claims) if c["tool"] in SHELL and c.get("reported") is not None]
        for var in ("status", "output"):
            for i in pick(rng_for("M4", var), elig):
                c = claims[i]
                rep = c["reported"]
                specs.append(dict(op="M4", variant=var, pos=i, ids=[c["id"]], tools=[c["tool"]], paths=[],
                                  was_error=bool(rep.get("is_error")),
                                  new_output=edit_output(rep.get("output")) if var == "output" else None,
                                  what=f"{var} of `{(c.get('command') or '')[:60]}` (was {'error' if rep.get('is_error') else 'success'})"))
    if "M5" in ops and n:
        for var, k in (("k1", 1), ("k2", 2), ("all", n)):
            if k > n or (var == "all" and n <= 2):
                continue
            cut = claims[n - k:]
            if agent in ("claude", "fake") and len({c.get("rel") for c in cut}) > 1:
                continue
            specs.append(dict(op="M5", variant=var, pos=n - k, ids=[c["id"] for c in cut], tools=[c["tool"] for c in cut],
                              paths=[p for c in cut for p in (c.get("paths") or [])], what=f"cut the last {k} calls"))
    if "M6" in ops and n >= 2:
        for i in pick(rng_for("M6", "-"), range(n - 1)):
            a, b = claims[i], claims[i + 1]
            specs.append(dict(op="M6", variant=f"{kind_of(a['tool'])}+{kind_of(b['tool'])}", pos=i, ids=[a["id"], b["id"]],
                              tools=[a["tool"], b["tool"]], paths=(a.get("paths") or []) + (b.get("paths") or []),
                              gap_s=round(abs((b.get("t_use") or 0) - (a.get("t_use") or 0)), 3),
                              what=f"swap times of calls {i} and {i + 1}"))
    by_id = {c["id"]: c.get("paths") or [] for c in claims}
    for j, s in enumerate(specs):
        s["mid"] = f"{s['op']}_{s['variant'].replace('+', '-')}_{s['pos']}_{j:03d}"
        s["id_paths"] = {c: by_id.get(c) or (s["paths"] if len(s["ids"]) == 1 else []) for c in s["ids"]}
    return specs


# ---------- building a copy for one audit condition ----------

def build_copy(R, dst_parent, cond):
    src, rid = R["run"], R["run_id"]
    dst = os.path.join(dst_parent, rid)  # keep the run id: parse_observed maps bpftrace paths by it
    os.makedirs(dst)
    for name in os.listdir(src):
        if name in ("snapshots", "shadow", "report.json", "report.md", "gw_sock"):
            continue
        if name == "gw" and cond == "C":
            continue
        if name == "agent_stdout.log" and cond in ("B", "C"):
            continue
        os.symlink(os.path.join(src, name), os.path.join(dst, name))
    for which in ("pre", "post"):
        d = os.path.join(src, "snapshots", which)
        os.makedirs(os.path.join(dst, "snapshots", which))
        if os.path.exists(os.path.join(d, "manifest.json")):
            shutil.copy2(os.path.join(d, "manifest.json"), os.path.join(dst, "snapshots", which, "manifest.json"))
    for rel in (CC_DIR, OC_DIR):
        s = os.path.join(R["home"], rel)
        if os.path.isdir(s):
            shutil.copytree(s, os.path.join(dst, "snapshots", "post", "files", "home", rel))
    if cond == "A" and os.path.isdir(os.path.join(src, "shadow")):
        os.makedirs(os.path.join(dst, "shadow", "versions"))
        vd = os.path.join(src, "shadow", "versions")
        for name in os.listdir(vd) if os.path.isdir(vd) else []:
            os.symlink(os.path.join(vd, name), os.path.join(dst, "shadow", "versions", name))
        if os.path.exists(os.path.join(src, "shadow", "index.jsonl")):
            shutil.copy2(os.path.join(src, "shadow", "index.jsonl"), os.path.join(dst, "shadow", "index.jsonl"))
    return dst


def record_change(dst, cond, rels):
    """After the trace files `rels` changed: update the post manifest and, in condition A, add the new
    versions to shadow/ as shadow.py would have recorded them (same inode: edited in place)."""
    home = os.path.join(dst, "snapshots", "post", "files", "home")
    mp = os.path.join(dst, "snapshots", "post", "manifest.json")
    man = {}
    if os.path.exists(mp):
        with open(mp) as f:
            man = json.load(f)
    for rel in rels:
        p = os.path.join(home, rel)
        key = "home/" + rel
        if os.path.exists(p):
            ent = dict(man.get(key) or {})
            ent.update(size=os.path.getsize(p), sha256=sha256(p))
            man[key] = ent
        else:
            man.pop(key, None)
    with open(mp, "w") as f:
        json.dump(man, f, indent=1)
    idx = os.path.join(dst, "shadow", "index.jsonl")
    if cond != "A" or not os.path.exists(idx):
        return
    with open(idx) as f:
        recs = [json.loads(x) for x in f if x.strip()]
    tick = max([r.get("tick", 0) for r in recs] + [0]) + 1
    t = max([r.get("t", 0) for r in recs] + [0]) + 0.2
    n = len(recs) + 1
    new = []
    for rel in rels:
        p = os.path.join(home, rel)
        last = next((r for r in reversed(recs) if r["rel"] == rel), None)
        if not os.path.exists(p):
            if last and last["event"] != "deleted":
                new.append({"t": t, "tick": tick, "rel": rel, "event": "deleted"})
            continue
        copy_name = f"versions/{n:06d}__mut__{rel.replace('/', '__')}"
        n += 1
        shutil.copy2(p, os.path.join(dst, "shadow", copy_name))
        new.append({"t": t, "tick": tick, "rel": rel, "event": "changed" if last and last["event"] != "deleted" else "new",
                    "size": os.path.getsize(p), "inode": (last or {}).get("inode", (man.get("home/" + rel) or {}).get("inode")),
                    "sha256": sha256(p), "copy": copy_name})
    with open(idx, "a") as f:
        for r in new:
            f.write(json.dumps(r) + "\n")


# ---------- applying a mutation: Claude Code JSONL ----------

class Jsonl:
    def __init__(self, path):
        self.path = path
        with open(path, encoding="utf-8") as f:
            self.lines = f.read().split("\n")
        self.obj = {}

    def get(self, i):
        if i not in self.obj:
            try:
                self.obj[i] = json.loads(self.lines[i])
            except ValueError:
                self.obj[i] = None
        return self.obj[i]

    def blocks(self, i):
        e = self.get(i)
        if not isinstance(e, dict) or e.get("type") not in ("assistant", "user"):
            return []
        c = (e.get("message") or {}).get("content")
        return c if isinstance(c, list) else []

    def find(self, cid):
        use = res = None
        for i in range(len(self.lines)):
            for b in self.blocks(i):
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") == cid:
                    use = i
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") == cid:
                    res = i
        return use, res

    def dirty(self, i):
        self.lines[i] = json.dumps(self.obj[i], ensure_ascii=False, separators=(",", ":"))

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.lines))


def cc_block(J, i, pred):
    return next(b for b in J.blocks(i) if isinstance(b, dict) and pred(b))


def cc_apply(dst, R, spec):
    home = os.path.join(dst, "snapshots", "post", "files", "home")
    claims = {c["id"]: c for c in R["claims"]}
    op = spec["op"]
    if op == "M2":
        ref = claims_at(R, spec["pos"])
        rel = ref["rel"] if ref else R["claims"][-1]["rel"]
    else:
        rel = claims[spec["ids"][0]]["rel"]
    J = Jsonl(os.path.join(home, rel))
    if op in ("M1", "M5"):
        if op == "M5":
            first_use, _ = J.find(spec["ids"][0])
            J.lines = J.lines[:first_use] + ([""] if J.lines and J.lines[-1] == "" else [])
        else:
            drop = []
            for cid in spec["ids"]:
                for i in J.find(cid):
                    if i is None:
                        continue
                    e = J.get(i)
                    e["message"]["content"] = [b for b in J.blocks(i) if not (isinstance(b, dict) and (b.get("id") == cid or b.get("tool_use_id") == cid))]
                    if e["message"]["content"]:
                        J.dirty(i)
                    else:
                        drop.append(i)
            J.lines = [x for i, x in enumerate(J.lines) if i not in set(drop)]
    elif op == "M2":
        fab, fid = spec["fab"], spec["ids"][0]
        tmpl = next(c for c in R["claims"] if c.get("rel") == rel)
        tu, tr = J.find(tmpl["id"])
        use_e, res_e = copy.deepcopy(J.get(tu)), copy.deepcopy(J.get(tr)) if tr is not None else None
        rng = random.Random(spec["mid"])
        if spec["pos"] == "eof":
            at = len(J.lines) - (1 if J.lines and J.lines[-1] == "" else 0)
            last_t = max((ptc.ts((J.get(i) or {}).get("timestamp")) or 0) for i in range(len(J.lines)) if isinstance(J.get(i), dict))
            t_use = last_t + 1.0
            parent = None
            for i in range(at - 1, -1, -1):
                if isinstance(J.get(i), dict) and J.get(i).get("uuid"):
                    parent = J.get(i)["uuid"]
                    break
        else:
            ref = claims_at(R, spec["pos"])
            if fab.get("replay_of"):
                ref = claims[fab["replay_of"]]
            _, rr = J.find(ref["id"])
            at = (rr if rr is not None else J.find(ref["id"])[0]) + 1
            nxt = [c for c in R["claims"] if (c.get("t_use") or 0) > (ref.get("t_result") or ref.get("t_use") or 0)]
            t0 = ref.get("t_result") or ref.get("t_use")
            t_use = t0 + (0.3 if not nxt else min(0.3, ((nxt[0]["t_use"] or t0) - t0) / 3))
            parent = (J.get(at - 1) or {}).get("uuid")
        uid_use = "%08x-%04x-4%03x-a%03x-%012x" % tuple(rng.getrandbits(b) for b in (32, 16, 12, 12, 48))
        uid_res = "%08x-%04x-4%03x-a%03x-%012x" % tuple(rng.getrandbits(b) for b in (32, 16, 12, 12, 48))
        if fab["kind"] == "shell":
            block = {"type": "tool_use", "id": fid, "name": fab["tool"], "input": {"command": fab["arg"], "description": "Run command"}}
            tur = {"stdout": fab["out"], "stderr": "", "interrupted": False, "isImage": False, "noOutputExpected": False}
            out = fab["out"] or "(Bash completed with no output)"
        else:
            block = {"type": "tool_use", "id": fid, "name": fab["tool"], "input": {"file_path": fab["arg"], "content": fab["out"]}}
            tur = {"type": "create", "filePath": fab["arg"], "content": fab["out"], "structuredPatch": []}
            out = f"File created successfully at: {fab['arg']}"
        use_e["message"]["content"] = [block]
        use_e["message"]["id"] = "msg_" + fid[5:]
        use_e.update(uuid=uid_use, parentUuid=parent, timestamp=iso(t_use))
        new_lines = [json.dumps(use_e, ensure_ascii=False, separators=(",", ":"))]
        if res_e is not None:
            res_e["message"]["content"] = [{"tool_use_id": fid, "type": "tool_result", "content": out, "is_error": False}]
            res_e.update(uuid=uid_res, parentUuid=uid_use, timestamp=iso(t_use + 0.1), toolUseResult=tur,
                         sourceToolAssistantUUID=uid_use)
            new_lines.append(json.dumps(res_e, ensure_ascii=False, separators=(",", ":")))
        J.lines[at:at] = new_lines
    elif op == "M3":
        i, _ = J.find(spec["ids"][0])
        b = cc_block(J, i, lambda b: b.get("id") == spec["ids"][0])
        if spec["variant"] == "path":
            for k in ("file_path", "notebook_path", "path"):
                if isinstance(b["input"].get(k), str):
                    b["input"][k] = spec["new_path"]
                    break
        else:
            b["input"]["command"] = spec["new_command"]
        J.dirty(i)
    elif op == "M4":
        cid = spec["ids"][0]
        _, i = J.find(cid)
        e = J.get(i)
        b = cc_block(J, i, lambda b: b.get("tool_use_id") == cid)
        text = b.get("content")
        if isinstance(text, list):
            text = "\n".join(x.get("text", "") for x in text if isinstance(x, dict))
        text = text or ""
        tur = e.get("toolUseResult") if isinstance(e.get("toolUseResult"), dict) else None
        if spec["variant"] == "status":
            if spec["was_error"]:
                text = re.sub(r"^Exit code \d+\n?", "", text)
                b["is_error"] = False
            else:
                text = "Exit code 1\n" + text
                b["is_error"] = True
        else:
            text = spec["new_output"]
        b["content"] = text
        if tur is not None and "stdout" in tur:
            tur["stdout"] = re.sub(r"^Exit code \d+\n?", "", text)
        J.dirty(i)
    elif op == "M6":
        (ua, ra), (ub, rb) = J.find(spec["ids"][0]), J.find(spec["ids"][1])
        for x, y in ((ua, ub), (ra, rb)):
            if x is None or y is None:
                continue
            ex, ey = J.get(x), J.get(y)
            ex["timestamp"], ey["timestamp"] = ey.get("timestamp"), ex.get("timestamp")
            J.dirty(x)
            J.dirty(y)
    J.save()
    return [rel]


def claims_at(R, pos):
    if pos == "eof" or not R["claims"]:
        return None
    return R["claims"][pos]


# ---------- applying a mutation: OpenCode SQLite ----------

def oc_apply(dst, R, spec):
    d = os.path.join(dst, "snapshots", "post", "files", "home", OC_DIR)
    db = os.path.join(d, "opencode.db")
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA foreign_keys=OFF")
        rows = {}
        for pid, mid, sid, tc, data in con.execute("select id, message_id, session_id, time_created, data from part"):
            try:
                dd = json.loads(data)
            except ValueError:
                continue
            if dd.get("type") == "tool":
                rows[dd.get("callID") or pid] = (pid, mid, sid, tc, dd)
        op = spec["op"]

        def put(cid, dd, tc=None):
            pid = rows[cid][0]
            if tc is None:
                con.execute("update part set data=? where id=?", (json.dumps(dd), pid))
            else:
                con.execute("update part set data=?, time_created=? where id=?", (json.dumps(dd), tc, pid))

        if op == "M1":
            con.execute("delete from part where id=?", (rows[spec["ids"][0]][0],))
        elif op == "M5":
            pid, mid, sid, tc, _ = rows[spec["ids"][0]]
            mt = con.execute("select time_created from message where id=?", (mid,)).fetchone()
            con.execute("delete from part where session_id=? and time_created>=?", (sid, tc))
            if mt:
                con.execute("delete from message where session_id=? and time_created>?", (sid, mt[0]))
        elif op == "M2":
            fab, fid = spec["fab"], spec["ids"][0]
            claims = {c["id"]: c for c in R["claims"]}
            if spec["pos"] == "eof":
                ref = R["claims"][-1]
                t_use = (max(r[3] for r in rows.values()) / 1000.0) + 1.0
            else:
                ref = claims[fab["replay_of"]] if fab.get("replay_of") else R["claims"][spec["pos"]]
                nxt = [c for c in R["claims"] if (c.get("t_use") or 0) > (ref.get("t_result") or ref.get("t_use") or 0)]
                t0 = ref.get("t_result") or ref.get("t_use")
                t_use = t0 + (0.3 if not nxt else min(0.3, ((nxt[0]["t_use"] or t0) - t0) / 3))
            _, mid, sid, _, _ = rows[ref["id"]]
            ms = int(t_use * 1000)
            if fab["kind"] == "shell":
                state = {"status": "completed", "input": {"command": fab["arg"], "workdir": "/workspace"},
                         "output": fab["out"], "metadata": {"output": fab["out"], "exit": 0, "truncated": False},
                         "title": fab["arg"], "time": {"start": ms, "end": ms + 80}}
            else:
                state = {"status": "completed", "input": {"content": fab["out"], "filePath": fab["arg"]},
                         "output": "Wrote file successfully.", "metadata": {"diagnostics": {}, "filepath": fab["arg"], "exists": False, "truncated": False},
                         "title": fab["arg"].lstrip("/"), "time": {"start": ms, "end": ms + 20}}
            dd = {"type": "tool", "tool": fab["tool"], "callID": fid, "state": state}
            con.execute("insert into part (id, message_id, session_id, time_created, time_updated, data) values (?,?,?,?,?,?)",
                        ("prt_" + fid[5:], mid, sid, ms, ms + 100, json.dumps(dd)))
        elif op == "M3":
            cid = spec["ids"][0]
            dd = rows[cid][4]
            inp = dd["state"]["input"]
            if spec["variant"] == "path":
                for k in ("filePath", "file_path", "path"):
                    if isinstance(inp.get(k), str):
                        inp[k] = spec["new_path"]
                        break
            else:
                inp["command"] = spec["new_command"]
                dd["state"]["title"] = spec["new_command"]
            put(cid, dd)
        elif op == "M4":
            cid = spec["ids"][0]
            dd = rows[cid][4]
            st = dd["state"]
            meta = st.setdefault("metadata", {})
            if spec["variant"] == "status":
                old = meta.get("exit")
                meta["exit"] = 0 if (isinstance(old, int) and old != 0) or spec["was_error"] else 1
                if st.get("status") == "error" and meta["exit"] == 0:
                    st["status"] = "completed"
            else:
                st["output"] = spec["new_output"]
                if "output" in meta:
                    meta["output"] = spec["new_output"]
            put(cid, dd)
        elif op == "M6":
            a, b = rows[spec["ids"][0]], rows[spec["ids"][1]]
            da, db_ = a[4], b[4]
            da["state"]["time"], db_["state"]["time"] = db_["state"].get("time"), da["state"].get("time")
            put(spec["ids"][0], da, b[3])
            put(spec["ids"][1], db_, a[3])
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db + suffix):
            os.remove(db + suffix)
    return [OC_DIR + "/opencode.db", OC_DIR + "/opencode.db-wal", OC_DIR + "/opencode.db-shm"]


def apply(dst, R, spec, cond):
    rels = cc_apply(dst, R, spec) if R["agent"] in ("claude", "fake") else oc_apply(dst, R, spec)
    record_change(dst, cond, rels)


def landed(dst, R, spec):
    """Did the mutation reach the claims the parser reads from the final trace? Returns a problem or None."""
    fc = final_claims(dst, R["agent"])
    op, ids = spec["op"], spec["ids"]
    if op in ("M1", "M5"):
        return None if not set(ids) & set(fc) else "deleted call still parsed"
    if op == "M2":
        c = fc.get(ids[0])
        return None if c and (c.get("command") == spec["fab"]["arg"] or spec["fab"]["arg"] in (c.get("paths") or [])) else "inserted call not parsed"
    c = fc.get(ids[0])
    if c is None:
        return "target call missing after mutation"
    if op == "M3":
        ok = (spec["new_path"] in (c.get("paths") or [])) if spec["variant"] == "path" else c.get("command") == spec["new_command"]
        return None if ok else "input unchanged"
    if op == "M4":
        orig = next(x for x in R["claims"] if x["id"] == ids[0])
        return None if json.dumps(c.get("reported"), sort_keys=True) != json.dumps(orig.get("reported"), sort_keys=True) else "result unchanged"
    if op == "M6":
        o = {x["id"]: x for x in R["claims"]}
        return None if abs((c.get("t_use") or 0) - (o[ids[1]].get("t_use") or 0)) < 0.002 else "times unchanged"
    return None


# ---------- running compare.py and reading its findings ----------

def run_compare(dst, agent, compare_path, label, tmpdir):
    env = dict(os.environ, TMPDIR=tmpdir)
    p = subprocess.run([sys.executable, "-I", compare_path, dst, "--agent", agent], capture_output=True, text=True,
                       env=env, timeout=600)
    if p.returncode != 0:
        return None, p.stderr[-2000:]
    os.replace(os.path.join(dst, "report.json"), os.path.join(dst, f"report.{label}.json"))
    with open(os.path.join(dst, f"report.{label}.json")) as f:
        return json.load(f), None


def findings(rep, final_ids):
    """Every finding of a report that an auditor would read as 'the trace and the records disagree'.
    Keys are stable across baseline and mutant (claim ids, pids, paths)."""
    F = {}
    for f in rep["trace_integrity"]["files"]:
        if f.get("session_record"):
            for fl in f["flags"]:
                F[("file", fl, f["rel"])] = None
    for cid in rep.get("claims_changed_between_versions") or []:
        F[("shadow", "changed", cid)] = None
    for cid in rep.get("claims_removed_between_versions") or []:  # patched compare.py
        F[("shadow", "removed", cid)] = None
    for src, b in rep["by_source"].items():
        for cid, m in b["claims"].items():
            if cid not in final_ids:
                continue
            v = m.get("verdict") or ""
            if v.startswith(("matched", "not checkable")):
                continue
            kind = v.split(" ", 1)[0].rstrip(":")
            kind = {"not": "unobserved"}.get(kind, kind)
            F[(src, kind, cid, v)] = None
        fin = b["versions"].get("final") or {}
        for x in fin.get("missing_processes", []):
            F[(src, "missing", x["pid"])] = {"cmd": x["cmd"][:160], "earlier": x.get("was_claimed_in_an_earlier_version"),
                                             "internal": x.get("harness_internal"), "effects": bool(cmp_mod.significant(x))}
        for x in fin.get("unclaimed_file_changes", []):  # patched compare.py
            F[(src, "unclaimed_change", x["path"])] = {"pid": x.get("pid")}
    g = rep.get("gateway")
    if g:
        d = g["versions"].get("final") or {}
        for cid in d.get("asked_not_in_trace", []):
            F[("gw", "asked_not_in_trace", cid)] = None
        for cid in d.get("in_trace_not_asked", []):
            F[("gw", "in_trace_not_asked", cid)] = None
        for x in d.get("input_differs", []):
            F[("gw", "input_differs", x["id"])] = None
        for x in d.get("result_differs", []):  # patched compare.py
            F[("gw", "result_differs", x["id"])] = None
        for x in d.get("time_outside_gateway_window", []):  # patched compare.py
            F[("gw", "time_outside_window", x["id"])] = None
    return F


def layer(key):
    return {"strace": "kernel", "bpf": "kernel"}.get(key[0], key[0])


def classify(spec, base_rep, base_F, mut_rep, mut_F):
    ids = set(spec["ids"])
    tp = {}
    for src, b in base_rep["by_source"].items():
        tp[src] = {b["claims"][c]["pid"] for c in ids if c in b["claims"] and "pid" in b["claims"][c]}
    tpaths = set(spec.get("paths") or [])
    new = {k: v for k, v in mut_F.items() if k not in base_F}
    lost = [k for k in base_F if k not in mut_F]

    def hits(k, v):
        if k[0] == "file":
            return set()
        if k[1] == "missing":
            got = {c for c in ids if (v or {}).get("earlier") == c}
            pid_hit = k[2] in tp.get(k[0], set())
            return got | ({"pid"} if pid_hit else set())
        if k[1] == "unclaimed_change":
            return {"path"} if k[2] in tpaths else set()
        return {k[2]} & ids

    targeted, collateral, filelevel = [], [], []
    per_call = {c: set() for c in spec["ids"]}
    for k, v in new.items():
        h = hits(k, v)
        if k[0] == "file":
            filelevel.append(f"{k[1]}")
        elif h:
            targeted.append(f"{k[0]}:{k[1]}")
            for c in spec["ids"]:
                if c in h or (k[1] == "missing" and (v or {}).get("earlier") == c) or \
                        (k[1] == "missing" and k[2] in {base_rep["by_source"][k[0]]["claims"].get(c, {}).get("pid")}) or \
                        (k[1] == "unclaimed_change" and k[2] in (spec.get("id_paths") or {}).get(c, [])):
                    per_call[c].add(f"{k[0]}:{k[1]}")
        else:
            collateral.append({"finding": list(k), "info": v})
    layers = sorted({layer(tuple(t.split(":", 1))) for t in targeted})
    tv = {src: {c: b["claims"].get(c, {}).get("verdict") for c in spec["ids"]} for src, b in mut_rep["by_source"].items()}

    def full_match(src, c):
        v = mut_rep["by_source"][src]["claims"].get(c, {}).get("verdict") or ""
        return v.startswith("matched") and "only as far as" not in v
    # the planted claim itself is called "matched" by a kernel-side source although it is false (a caveat the
    # verdict itself shows, such as an argv cut by the recorder, does not count; the report.md reader sees the verdict)
    vouched = sorted(src for src in tv if spec["op"] in ("M2", "M3", "M4") and any(full_match(src, c) for c in spec["ids"]))
    return {"detected": bool(targeted), "layers": layers, "targeted": sorted(set(targeted)), "vouched_by": vouched,
            "file_level": sorted(set(filelevel)), "collateral": collateral, "lost": [list(k) for k in lost],
            "calls_flagged": sum(1 for c in per_call if per_call[c]), "calls": len(per_call),
            "target_verdicts": tv,
            "baseline_linked_pids": {s: sorted(p) for s, p in tp.items()}}


# ---------- predictions (written before the first run; compared with the outcome) ----------

def predict(spec, cond, R):
    """Layers expected to point at the mutated call with the current compare.py."""
    op, var = spec["op"], spec["variant"]
    k = kind_of(spec["tools"][0])
    gw = cond in ("A", "B") and os.path.isdir(os.path.join(R["run"], "gw"))
    out = set()
    if op in ("M1", "M5"):
        if any(kind_of(t) == "shell" for t in spec["tools"]):
            out.add("kernel")
        if gw:
            out.add("gw")
    elif op == "M2":
        out.add("kernel")
        if gw:
            out.add("gw")
    elif op == "M3":
        if var in ("swap", "flag", "path"):
            out.add("kernel")
        if gw:
            out.add("gw")
        if cond == "A":
            out.add("shadow")
    elif op == "M4":
        if var == "status":
            out.add("kernel")
        if cond == "A":
            out.add("shadow")
    elif op == "M6":
        out.add("kernel")
    return sorted(out)


# ---------- driver ----------

def eval_run(R, specs, conds, compares, work, tmpdir, keep=False):
    out = {"baselines": {}, "mutants": []}
    base = {}
    for cond in conds:
        d = build_copy(R, os.path.join(work, R["run_id"], cond, "baseline"), cond)
        fids = set(final_claims(d, R["agent"]))
        for label, path in compares:
            rep, err = run_compare(d, R["agent"], path, label, tmpdir)
            if rep is None:
                out["baselines"].setdefault(cond, {})[label] = {"error": err}
                continue
            F = findings(rep, fids)
            base[(cond, label)] = (rep, F)
            out["baselines"].setdefault(cond, {})[label] = baseline_summary(rep, F)
    for spec in specs:
        for cond in conds:
            d = build_copy(R, os.path.join(work, R["run_id"], cond, spec["mid"]), cond)
            try:
                apply(d, R, spec, cond)
                problem = landed(d, R, spec)
            except Exception as e:  # a mutation that cannot be applied to this trace is recorded, not fatal
                problem = f"apply failed: {type(e).__name__}: {e}"
            rec = {k: v for k, v in spec.items() if k not in ("fab",)}
            rec.update(run=R["run_id"], agent=R["agent"], cond=cond, landed=problem is None, problem=problem,
                       predicted=predict(spec, cond, R), results={})
            if problem is None:
                fids = set(final_claims(d, R["agent"]))
                for label, path in compares:
                    if (cond, label) not in base:
                        continue
                    rep, err = run_compare(d, R["agent"], path, label, tmpdir)
                    if rep is None:
                        rec["results"][label] = {"error": err}
                        continue
                    brep, bF = base[(cond, label)]
                    res = classify(spec, brep, bF, rep, findings(rep, fids))
                    res["claim_counts"] = rep.get("claim_counts")
                    rec["results"][label] = res
            out["mutants"].append(rec)
            if not keep:  # 70 runs x hundreds of copies would not fit; keep only what --keep asks for
                shutil.rmtree(os.path.dirname(d), ignore_errors=True)
    if not keep:
        shutil.rmtree(os.path.join(work, R["run_id"]), ignore_errors=True)
    return out


def baseline_summary(rep, F):
    s = {"claims": rep.get("claim_counts"), "findings": []}
    for k, v in F.items():
        s["findings"].append({"key": list(k), "info": v})
    return s


def summarize(results, compares, group):
    """Counts per (compare label, group(mutant)): detected at call level, file-level only, missed, layers."""
    rows = {}
    for m in results:
        for label, _ in compares:
            res = m["results"].get(label)
            if not m["landed"] or not res or "error" in res:
                continue
            r = rows.setdefault((label,) + group(m), {"n": 0, "det": 0, "file_only": 0, "miss": 0, "kernel": 0, "gw": 0,
                                                      "shadow": 0, "vouched": 0, "collateral": 0, "calls": 0, "calls_flagged": 0})
            r["n"] += 1
            r["det"] += res["detected"]
            r["file_only"] += (not res["detected"]) and bool(res["file_level"])
            r["miss"] += (not res["detected"]) and not res["file_level"]
            for L in ("kernel", "gw", "shadow"):
                r[L] += L in res["layers"]
            r["vouched"] += bool(res["vouched_by"])
            r["collateral"] += len(res["collateral"])
            r["calls"] += res["calls"]
            r["calls_flagged"] += res["calls_flagged"]
    return rows


def baseline_rows(runs, compares):
    """Findings on the unmutated copies: what an auditor would see although nothing was planted."""
    rows = []
    for rid, r in sorted(runs.items()):
        for cond, by_label in sorted(r.get("baselines", {}).items()):
            for label, b in by_label.items():
                c = {"call_level": 0, "missing_with_effects": 0, "missing_no_effects": 0, "harness_internal": 0, "file_flags": 0}
                for x in b.get("findings", []):
                    k, info = x["key"], x["info"] or {}
                    if k[0] == "file":
                        c["file_flags"] += 1
                    elif k[1] == "missing":
                        c["harness_internal" if info.get("internal") else "missing_with_effects" if info.get("effects")
                          else "missing_no_effects"] += 1
                    else:
                        c["call_level"] += 1
                rows.append(dict(run=rid, cond=cond, label=label, **c))
    return rows


def render_table(out, compares):
    L = ["# Mutation test of compare.py (generated by mutate_eval.py)\n",
         f"seed {out['seed']}, up to {out['positions']} positions per operator variant and run; "
         "detected = a new finding that points at the planted call; file-level only = only a new integrity flag on the "
         "trace file; vouched = a kernel-side source calls the planted claim itself `matched` without a caveat "
         "(such as an argv cut by the recorder). kernel/gw/shadow = mutants detected by that layer.\n"]
    labels = [lb for lb, _ in compares]
    for title, key in (("operator x condition x agent", "by_agent"), ("operator variant x condition (agents pooled)", "by_variant")):
        rows = out["summary"][key]
        L.append(f"\n## {title}\n")
        head = "| op | " + ("variant | " if key == "by_variant" else "") + "cond | " + ("agent | " if key == "by_agent" else "")
        cols = " | ".join(f"{lb}: detected | {lb}: kernel/gw/shadow | {lb}: vouched" for lb in labels)
        L.append(head + "n | " + cols + " |")
        L.append("|---" * (head.count("|") + 3 * len(labels)) + "|")
        groups = sorted({tuple(r["group"]) for r in rows})
        for g in groups:
            cells = []
            n = None
            for lb in labels:
                r = next((x for x in rows if x["label"] == lb and tuple(x["group"]) == g), None)
                if r is None:
                    cells += ["-", "-", "-"]
                    continue
                n = r["n"]
                det = f"{r['det']}/{r['n']}" + (f" (+{r['file_only']} file-level)" if r["file_only"] else "")
                cells += [det, f"{r['kernel']}/{r['gw']}/{r['shadow']}", str(r["vouched"])]
            L.append("| " + " | ".join(g) + f" | {n} | " + " | ".join(cells) + " |")
    L.append("\n## Unmutated copies (false-positive check)\n")
    L.append("| run | cond | compare | call-level findings | missing processes with effects | without effects | labelled harness-internal | file flags |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in out["baseline_summary"]:
        L.append(f"| {r['run']} | {r['cond']} | {r['label']} | {r['call_level']} | {r['missing_with_effects']} | "
                 f"{r['missing_no_effects']} | {r['harness_internal']} | {r['file_flags']} |")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--out", default=".")
    ap.add_argument("--work", default="mutation_work")
    ap.add_argument("--compare", action="append", default=[])
    ap.add_argument("--seed", default="20261031")
    ap.add_argument("--positions", type=int, default=3)
    ap.add_argument("--ops", default=",".join(OPS))
    ap.add_argument("--conds", default=",".join(CONDS))
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    compares = []
    for c in a.compare or [f"current={os.path.join(HERE, 'compare.py')}"]:
        label, _, path = c.rpartition("=")
        compares.append((label or os.path.basename(os.path.dirname(os.path.abspath(path))), os.path.abspath(path)))
    ops, conds = a.ops.split(","), a.conds.split(",")
    work = os.path.abspath(a.work)
    os.makedirs(work, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="tmp-", dir=work)
    runs = [load_run(os.path.abspath(r)) for r in a.runs]
    plans = {R["run_id"]: plan_mutations(R, ops, a.positions, a.seed) for R in runs}
    out = {"generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "seed": a.seed,
           "positions": a.positions, "ops": ops, "conds": conds,
           "compare": [{"label": lb, "path": p, "sha256": sha256(p)} for lb, p in compares],
           "runs": {}, "mutants": []}
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        futs = {ex.submit(eval_run, R, plans[R["run_id"]], conds, compares, work, tmpdir, a.keep): R for R in runs}
        for fu in concurrent.futures.as_completed(futs):
            R = futs[fu]
            try:
                r = fu.result()
            except Exception as e:  # one broken run directory does not stop the others
                out["runs"][R["run_id"]] = {"agent": R["agent"], "error": f"{type(e).__name__}: {e}"}
                print(f"{R['run_id']}: failed: {e}", file=sys.stderr)
                continue
            out["runs"][R["run_id"]] = {"agent": R["agent"], "claims_in_final_trace": len(R["claims"]),
                                        "tools": [c["tool"] for c in R["claims"]], "baselines": r["baselines"]}
            out["mutants"].extend(r["mutants"])
            print(f"{R['run_id']}: {len(r['mutants'])} mutant copies", file=sys.stderr)
    out["mutants"].sort(key=lambda m: (m["run"], m["mid"], m["cond"]))
    out["summary"] = {}
    for key, group in (("by_agent", lambda m: (m["op"], m["cond"], m["agent"])),
                       ("by_variant", lambda m: (m["op"], m["variant"] if m["op"] != "M6" else "any pair", m["cond"]))):
        rows = summarize(out["mutants"], compares, group)
        out["summary"][key] = [dict(label=k[0], group=list(k[1:]), **v) for k, v in sorted(rows.items())]
    out["baseline_summary"] = baseline_rows(out["runs"], compares)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "mutation_results.json"), "w") as f:
        json.dump(out, f, indent=1, default=str)
    with open(os.path.join(a.out, "mutation_results_table.md"), "w") as f:
        f.write(render_table(out, compares))
    if not a.keep:
        shutil.rmtree(work, ignore_errors=True)
    print(os.path.join(a.out, "mutation_results.json"))


if __name__ == "__main__":
    main()
