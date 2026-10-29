#!/usr/bin/env python3
"""Copy the publishable part of one run directory, e.g. into the repository's runs/ folder.

Kept: meta, reports, the harness's stdout, strace and bpftrace records, shadow index, snapshot
manifests, the final native trace files, the bridge's request log, and the tool calls the model asked
for (gw/tool_calls.jsonl, extracted from the raw responses).
Left out: gw/bodies/ (full prompts, including the harness's system prompt, and raw responses),
copies of every shadow version, the rest of the agent home, and the socket directory.

Usage: export_run.py <run_dir> <out_dir>
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_gateway as pg  # noqa: E402

FILES = ["meta.json", "finished.json", "container_exit_code", "report.md", "report.json", "agent_stdout.log",
         "agent_stderr.log", "bpf.jsonl", "bpf.err", "observe/strace.log", "observe/entry_rc",
         "observe/entry_start", "observe/entry_end", "shadow/index.jsonl", "gw/gw.jsonl"]
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
    home = os.path.join(run, "snapshots", "post", "files", "home")
    for rel in TRACE_DIRS:
        src = os.path.join(home, rel)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(out, "final_trace", rel), dirs_exist_ok=True)
    calls = pg.load(run)
    if calls is not None:
        os.makedirs(os.path.join(out, "gw"), exist_ok=True)
        with open(os.path.join(out, "gw", "tool_calls.jsonl"), "w") as f:
            for c in calls:
                f.write(json.dumps({k: c.get(k) for k in ("n", "t_start", "model", "status", "stop", "tool_calls")},
                                   ensure_ascii=False) + "\n")
    print(out)


if __name__ == "__main__":
    main(os.path.normpath(sys.argv[1]), sys.argv[2])
