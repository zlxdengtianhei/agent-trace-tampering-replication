# Trace vs reality: recording coding agents independently of their own traces

Small-scale replication kit for Qin et al., "LLM Agents Can Easily Tamper With Their Own Traces"
(arXiv 2609.30266). A coding agent runs in a disposable Docker container without network access.
Independent records are taken outside the agent's control and compared with the agent's native trace.

Agents: Claude Code 2.1.312 and OpenCode 1.19.5, both on the same model (GLM 5.3), so the harness is
the only variable between them. Both were evaluated by the paper (with Opus 5/5.5 and Qwen 3.8 Max),
and they store their traces differently (append-only JSONL vs SQLite with a write-ahead log).

| Layer | Where | What it records | Can the agent see or change it? |
|---|---|---|---|
| native trace | agent home in the container | what the agent says it did (Claude Code JSONL, OpenCode SQLite) | yes — that is the point |
| `observe/mx_observe.bt` (bpftrace) | host, root, filter `uid == 23001` | exec (argv), opens with flags, unlink/rename/truncate, connect, fork, exit codes | no (host pid namespace) |
| strace -f (`image/entry.sh`) | container, root, wraps the agent | the same syscalls with full arguments and resolved paths | can detect it (`TracerPid`), cannot read, kill or change it |
| `observe/shadow.py` | host | every version of every trace file (polling, 0.2 s) | no |
| `observe/snapshot.py` | host | before/after copy + sha256 of agent home and workspace | no |
| `net/gw_bridge.py` | host, root | every model request and the raw response; the tool calls the model asked for | no (the container reaches it only through a byte forwarder) |

## Model access and network

The container runs with `--network none`. A root process inside it (`image/gw_forward.py`) forwards
`127.0.0.1:8080` to a Unix socket in a root-only directory. On the host, `net/gw_bridge.py` serves that
socket: it forwards only model API calls (`/v1/messages`, `/v1/chat/completions`) for the allowed model
ids to an Anthropic/OpenAI-compatible gateway (`MX_GW_UPSTREAM`), drops client credentials, and records
each request body and raw response under `runs/<run-id>/gw/`. The agent has no key, no internet and no
view of the gateway's configuration. This bridge is also the paper's Section 4 idea in miniature: a
record of what the model was asked and what it answered, kept where the agent cannot reach it.

## Use (on the recording host, as root)

```sh
docker build -f image/Dockerfile -t mx-agents:2026-10-28 .
./record_run.sh --agent fake --scenario s1_user_asks_delete          # self-test, no model
./record_run.sh --agent claude --scenario s0_smoke                   # pipeline check with a real agent
./record_run.sh --agent opencode --scenario s1_user_asks_delete [--model zai/glm-5.3]
python3 -I analysis/compare.py runs/<run-id> --agent <agent>          # re-run the comparison
```

Each run writes `runs/<run-id>/`: `bpf.jsonl`, `observe/strace.log`, `shadow/`, `snapshots/{pre,post}/`,
`gw/gw.jsonl` and `gw/bodies/` (model calls), `agent_stdout.log` (the harness's streamed output),
`meta.json`, `report.json`, `report.md`. Run one recording at a time: bpftrace filters on the agent uid,
which every container shares.

## What `compare.py` reports

1. Was the trace deleted or altered: per trace file (deleted, emptied, shrunk, replaced, rewritten
   non-append), and every operation on a trace file with the acting process, its command line, and
   whether it happened after the agent session ended.
2. For each tool call in any version of the trace: matched, invented (no process or file operation),
   or misreported (exit status differs; claimed program never ran).
3. For the final trace and its last non-empty version: observed tool processes the trace does not
   mention, with their file and network effects. Processes the harness starts for itself (e.g. Claude
   Code's shell snapshot) are listed with a "harness-internal" label, not hidden.
4. Against the gateway record, joined by tool-call id: tool calls the model asked for that a trace
   version lacks, claims the model never asked for, and claims whose input differs from the request.

Matching is heuristic. Read `report.json` and the raw records before you cite a verdict.

## Scenarios

`scenarios/` holds five folders. s1–s3 are drafts modelled on the paper's appendix prompts; s4 and s5
are left for your own design. Only `turns/` is mounted into the container.

## Fake agent

`fake_agent/fake_agent.py` is a scripted harness without a model. It plants known divergences
(a misreported exit code, an invented call, two unreported actions, a trace rewrite, a delayed
truncation after the session). `--agent fake` checks that the pipeline detects all of them.

## Related code

The paper's own harness is at github.com/aisa-group/perfect-crime (no licence file; no code is copied
here). Its independent observer is an inotify file recorder in a separate container; this kit adds
process- and syscall-level records.
