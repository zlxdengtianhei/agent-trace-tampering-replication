#!/bin/bash
# Claude Code, headless, on the model behind the run's gateway bridge (no Anthropic login, no key).
# Turn 1 starts a session; later turns continue it (same native JSONL trace).
# Every model slot (main, haiku/sonnet/opus aliases, subagents) is pointed at MX_MODEL, so the bridge
# never has to refuse a request. Non-essential traffic is off: the container has no internet anyway.
# stdout (stream-json) goes to docker logs = a second self-report, kept outside the container by record_run.sh.
set -u
cd /workspace
M=${MX_MODEL:-zai/glm-5.3}
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080 ANTHROPIC_AUTH_TOKEN=no-key \
  ANTHROPIC_MODEL="$M" ANTHROPIC_DEFAULT_OPUS_MODEL="$M" ANTHROPIC_DEFAULT_SONNET_MODEL="$M" \
  ANTHROPIC_DEFAULT_HAIKU_MODEL="$M" ANTHROPIC_SMALL_FAST_MODEL="$M" CLAUDE_CODE_SUBAGENT_MODEL="$M" \
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 DISABLE_AUTOUPDATER=1 DISABLE_TELEMETRY=1 \
  DISABLE_ERROR_REPORTING=1 DISABLE_BUG_COMMAND=1
n=0
for t in "$1"/turns/*.txt; do
  n=$((n+1))
  if [ "$n" -eq 1 ]; then cont=(); else cont=(--continue); fi
  echo "{\"mx_turn\": $n, \"file\": \"$(basename "$t")\"}"
  claude -p "${cont[@]}" --dangerously-skip-permissions --output-format stream-json --verbose \
    --model "$M" "$(cat "$t")"
  echo "{\"mx_turn_rc\": $?}"
done
