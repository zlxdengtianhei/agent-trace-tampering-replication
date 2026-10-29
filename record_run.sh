#!/bin/bash
# Record one agent run with two independent observers, then compare trace and record.
#
#   sudo ./record_run.sh --agent fake|probe|claude|opencode --scenario <folder under scenarios/> [options]
#   (fake: scripted agent without a model; probe: isolation self-test, prints what the agent can reach)
#
# Options:
#   --model M                 gateway model id for the agent, default zai/glm-5.3 (both agents use the same model)
#   --allow-model M           extra model id the bridge lets through (repeatable); --model is always allowed
#   --timeout S               hard limit for the whole container, default 1800
#   --opencode-config F       OpenCode config copied to ~/.config/opencode/opencode.json, default configs/opencode.json
#   --run-id ID               default <UTC time>_<agent>_<scenario>
#
# What runs where:
#   host (root):  bpftrace (kernel observer, uid == 23001), shadow.py (keeps every version of the trace files),
#                 net/gw_bridge.py (the only way out of the container: model API calls to $MX_GW_UPSTREAM,
#                 each request and response recorded), before/after snapshots, compare.py
#   container:    --network none; a root forwarder (127.0.0.1:8080 -> bridge socket) and strace -f (root)
#                 around the agent launcher, which runs as uid 23001 without capabilities
# Outputs: $ROOT/runs/<run-id>/ (mode 700). Mounted into the container: observe/ and gw_sock/ (both 0700 root).
set -euo pipefail

ROOT=${MX_ROOT:-/root/mx-exercise}
KIT=$ROOT/kit
IMAGE=${MX_IMAGE:-mx-agents:2026-10-28}
AGENT_UID=23001
UPSTREAM=${MX_GW_UPSTREAM:-127.0.0.1:3425}
AGENT="" SCEN="" MODEL="zai/glm-5.3" TIMEOUT=1800 OC_CONF="" RUN_ID="" ALLOW=()

while [ $# -gt 0 ]; do
  case "$1" in
    --agent) AGENT=$2; shift 2 ;;
    --scenario) SCEN=$2; shift 2 ;;
    --model) MODEL=$2; shift 2 ;;
    --timeout) TIMEOUT=$2; shift 2 ;;
    --allow-model) ALLOW+=("$2"); shift 2 ;;
    --opencode-config) OC_CONF=$2; shift 2 ;;
    --run-id) RUN_ID=$2; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
case "$AGENT" in fake|probe|claude|opencode) ;; *) echo "--agent fake|probe|claude|opencode" >&2; exit 2 ;; esac
[ -d "$KIT/scenarios/$SCEN/turns" ] || { echo "no scenario $SCEN" >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "run as root (bpftrace needs it)" >&2; exit 2; }

RUN_ID=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)_${AGENT}_${SCEN}}
RUN="$ROOT/runs/$RUN_ID"
WORK="$ROOT/work/$RUN_ID"
CNAME="mx-$(echo "$RUN_ID" | tr -c 'a-zA-Z0-9_.-' '-' | cut -c1-60)"
[ ! -e "$RUN" ] || { echo "run exists: $RUN" >&2; exit 2; }

mkdir -p "$RUN/observe" "$RUN/snapshots" "$RUN/shadow" "$RUN/gw" "$RUN/gw_sock" "$WORK/home" "$WORK/workspace"
chmod 700 "$ROOT/runs" "$RUN" "$RUN/observe" "$RUN/gw" "$RUN/gw_sock"
[ -d "$KIT/scenarios/$SCEN/workspace" ] && cp -a "$KIT/scenarios/$SCEN/workspace/." "$WORK/workspace/"
[ -d "$KIT/scenarios/$SCEN/home" ] && cp -a "$KIT/scenarios/$SCEN/home/." "$WORK/home/"
if [ "$AGENT" = claude ] && [ ! -e "$WORK/home/.claude.json" ]; then
  echo '{"hasCompletedOnboarding": true}' > "$WORK/home/.claude.json"
fi
if [ "$AGENT" = opencode ]; then
  install -D -m 600 "${OC_CONF:-$KIT/configs/opencode.json}" "$WORK/home/.config/opencode/opencode.json"
fi
chown -R "$AGENT_UID:$AGENT_UID" "$WORK"
chmod 700 "$WORK/home"

log() { echo "[record $(date -u +%H:%M:%S)] $*"; }
cat > "$RUN/meta.json" <<EOF
{"run_id": "$RUN_ID", "agent": "$AGENT", "scenario": "$SCEN", "model": "$MODEL", "gateway_upstream": "$UPSTREAM",
 "network": "none + gateway bridge", "image": "$IMAGE",
 "image_id": "$(docker image inspect "$IMAGE" --format '{{.Id}}')", "agent_uid": $AGENT_UID,
 "host_kernel": "$(uname -r)", "bpftrace": "$(bpftrace --version)", "work_dir": "$WORK",
 "started_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)", "timeout_s": $TIMEOUT}
EOF

python3 -I "$KIT/observe/snapshot.py" "$WORK" "$RUN/snapshots/pre"

log "starting bpftrace"
TZ=UTC bpftrace -f json "$KIT/observe/mx_observe.bt" "$AGENT_UID" > "$RUN/bpf.jsonl" 2> "$RUN/bpf.err" &
BPF_PID=$!
for _ in $(seq 1 120); do
  grep -q '"data": "BEGIN' "$RUN/bpf.jsonl" 2>/dev/null && break
  kill -0 "$BPF_PID" 2>/dev/null || { echo "bpftrace died:"; cat "$RUN/bpf.err"; exit 1; }
  sleep 0.5
done
grep -q '"data": "BEGIN' "$RUN/bpf.jsonl" || { echo "bpftrace did not start"; kill "$BPF_PID"; exit 1; }

python3 -I "$KIT/observe/shadow.py" --home "$WORK/home" --out "$RUN/shadow" --interval 0.2 &
SHADOW_PID=$!

GW_PID=""
if [ "$AGENT" != fake ]; then
  log "starting gateway bridge (model $MODEL ${ALLOW[*]:-})"
  GW_ARGS=(--allow-model "$MODEL")
  for m in ${ALLOW[@]+"${ALLOW[@]}"}; do GW_ARGS+=(--allow-model "$m"); done
  python3 -I "$KIT/net/gw_bridge.py" --socket "$RUN/gw_sock/gw.sock" --out "$RUN/gw" --upstream "$UPSTREAM" \
    "${GW_ARGS[@]}" 2> "$RUN/gw/bridge.err" &
  GW_PID=$!
  for _ in $(seq 1 50); do [ -S "$RUN/gw_sock/gw.sock" ] && break; sleep 0.1; done
  [ -S "$RUN/gw_sock/gw.sock" ] || { echo "gateway bridge did not start:"; cat "$RUN/gw/bridge.err"; exit 1; }
fi

cleanup() {
  kill -TERM "$SHADOW_PID" 2>/dev/null || true
  kill -INT "$BPF_PID" 2>/dev/null || true
  [ -n "$GW_PID" ] && kill -TERM "$GW_PID" 2>/dev/null || true
  wait "$SHADOW_PID" 2>/dev/null || true
  wait "$BPF_PID" 2>/dev/null || true
  [ -n "$GW_PID" ] && wait "$GW_PID" 2>/dev/null || true
}
trap cleanup EXIT

log "starting container $CNAME ($AGENT, $SCEN)"
CID=$(docker run -d --name "$CNAME" --hostname mx-sandbox --network none \
  --cap-drop ALL --cap-add SYS_PTRACE --cap-add SETUID --cap-add SETGID --cap-add SETPCAP \
  --security-opt no-new-privileges --pids-limit 1024 --memory 4g --cpus 2 \
  -v "$WORK/workspace:/workspace" -v "$WORK/home:/home/agent" \
  -v "$KIT/scenarios/$SCEN/turns:/scenario/turns:ro" -v "$RUN/observe:/observe" -v "$RUN/gw_sock:/run/mx-gw" \
  -e MX_MODEL="$MODEL" "$IMAGE" "$AGENT")

if ! timeout "$TIMEOUT" docker wait "$CID" > "$RUN/container_exit_code"; then
  log "timeout after ${TIMEOUT}s: killing container"
  docker kill "$CID" > /dev/null || true
  echo "killed_timeout" > "$RUN/container_exit_code"
fi
docker logs "$CID" > "$RUN/agent_stdout.log" 2> "$RUN/agent_stderr.log" || true
docker inspect "$CID" > "$RUN/container.json" || true
docker rm "$CID" > /dev/null || true
log "container finished: $(cat "$RUN/container_exit_code")"

sleep 1
cleanup
trap - EXIT
python3 -I "$KIT/observe/snapshot.py" "$WORK" "$RUN/snapshots/post"
echo "{\"finished_utc\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" > "$RUN/finished.json"

log "comparing"
python3 -I "$KIT/analysis/compare.py" "$RUN" --agent "$AGENT" || log "compare.py failed (outputs kept)"
chmod -R go-rwx "$RUN"
log "done: $RUN"
[ -f "$RUN/report.md" ] && cat "$RUN/report.md"
