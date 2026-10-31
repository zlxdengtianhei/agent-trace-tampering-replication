#!/usr/bin/env python3
"""Copy the publishable part of one run directory, e.g. into the repository's runs/ folder.

Kept: meta, reports, the harness's stdout, strace and bpftrace records, shadow index, snapshot
manifests, the final native trace files, the bridge's request log, the tool calls the model asked
for (gw/tool_calls.jsonl, extracted from the raw responses), the scenario service's request log, and the
prompts as sent in this run (turns/, with the run's nonce).
Also: shadow.tar.xz (every kept version of the trace files) and gw/tool_results_sent.jsonl (each tool result as
the harness sent it to the model), so that compare.py can be rerun on the published copy after
`tar -xJf shadow.tar.xz`.
Left out: gw/bodies/ (full prompts, including the harness's system prompt, and raw responses), the rest of
the agent home, and the socket directory.

Usage: export_run.py <run_dir> <out_dir>
"""
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_gateway as pg  # noqa: E402

FILES = ["meta.json", "finished.json", "container_exit_code", "report.md", "report.json", "agent_stdout.log",
         "agent_stderr.log", "bpf.jsonl", "bpf.err", "observe/strace.log", "observe/entry_rc",
         "observe/entry_start", "observe/entry_end", "observe/service.log", "shadow/index.jsonl", "gw/gw.jsonl"]
TRACE_DIRS = [".claude/projects", ".local/share/opencode"]


def main(run, out):
    os.makedirs(out, exist_ok=True)
    for rel in FILES:
        src = os.path.join(run, rel)
        if os.path.isfile(src):
            os.makedirs(os.path.dirname(os.path.join(out, rel)) or out, exist_ok=True)
            shutil.copy2(src, os.path.join(out, rel))
    for which in ("pre", "post"):
        d = os.path.join(run, "snapshots", which)
        for name in os.listdir(d) if os.path.isdir(d) else []:
            if name.startswith("manifest"):
                os.makedirs(os.path.join(out, "snapshots", which), exist_ok=True)
                shutil.copy2(os.path.join(d, name), os.path.join(out, "snapshots", which, name))
    try:
        work = json.load(open(os.path.join(run, "meta.json"))).get("work_dir")
    except (OSError, ValueError):
        work = None
    if work and os.path.isdir(os.path.join(work, "turns")):
        shutil.copytree(os.path.join(work, "turns"), os.path.join(out, "turns"), dirs_exist_ok=True)
    home = os.path.join(run, "snapshots", "post", "files", "home")
    for rel in TRACE_DIRS:
        src = os.path.join(home, rel)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(out, "final_trace", rel), dirs_exist_ok=True)
    calls = pg.load(run)
    if calls is not None:
        os.makedirs(os.path.join(out, "gw"), exist_ok=True)
        seen = set()
        with open(os.path.join(out, "gw", "tool_calls.jsonl"), "w") as f:
            for c in calls:
                row = {k: c.get(k) for k in ("n", "t_start", "t_end", "method", "target", "model", "status",
                                             "answered_by", "error", "messages_sent", "stop", "tool_calls")}
                # Each request repeats every earlier tool result; keep each result only in the first request
                # that carried it (its time and is_error are what compare.py's gateway checks use).
                row["tool_results"] = {k: v for k, v in (c.get("tool_results") or {}).items() if k not in seen}
                seen.update(row["tool_results"])
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        with open(os.path.join(out, "gw", "tool_results_sent.jsonl"), "w") as f:
            for cid, text in sorted(pg.results_sent(run).items()):
                f.write(json.dumps({"id": cid, "text": text}, ensure_ascii=False) + "\n")
    # Every version of the trace files that shadow.py kept, compressed (OpenCode's SQLite copies shrink ~200x).
    if os.path.isdir(os.path.join(run, "shadow", "versions")):
        subprocess.run(["tar", "-C", run, "-cJf", os.path.join(out, "shadow.tar.xz"), "shadow/versions"], check=True)
    print(out)


if __name__ == "__main__":
    main(os.path.normpath(sys.argv[1]), sys.argv[2])
