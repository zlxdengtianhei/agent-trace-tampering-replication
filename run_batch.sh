#!/bin/bash
# Run a list of recordings, N at a time. Each recording gets its own agent uid (record_run.sh allocates it),
# so the runs' bpftrace records stay apart.
#
#   sudo ./run_batch.sh <jobs_file> [parallel=3]
#
# jobs_file: one job per line, "<agent> <scenario> <run_id>"; blank lines and lines starting with # are skipped.
# Jobs whose run directory already exists are skipped, so a batch can be restarted.
# Logs: $ROOT/logs/<run_id>.log; a one-line summary per job goes to stdout.
set -u
ROOT=${MX_ROOT:-/root/mx-exercise}
KIT=$ROOT/kit
JOBS=$1
P=${2:-3}
mkdir -p "$ROOT/logs"
one() {
  local agent=$1 scen=$2 id=$3
  if [ -e "$ROOT/runs/$id" ]; then echo "skip $id (exists)"; return 0; fi
  local t0=$(date +%s)
  "$KIT/record_run.sh" --agent "$agent" --scenario "$scen" --run-id "$id" > "$ROOT/logs/$id.log" 2>&1
  local rc=$?
  echo "$(date -u +%H:%M:%S) rc=$rc $(( $(date +%s) - t0 ))s $id"
}
export -f one
export ROOT KIT
grep -v '^\s*#' "$JOBS" | grep -v '^\s*$' | xargs -P "$P" -L 1 bash -c 'one "$0" "$1" "$2"'
