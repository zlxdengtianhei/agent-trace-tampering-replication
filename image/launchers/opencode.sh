#!/bin/bash
# OpenCode, headless (`opencode run`). Turn 1 starts a session; later turns continue it.
# Provider "gw" (the run's gateway bridge) and full-access permissions come from
# ~/.config/opencode/opencode.json, which record_run.sh copies from configs/opencode.json.
# MX_MODEL is the gateway model id (e.g. zai/glm-5.3); OpenCode sees it as gw/<id>.
# Model-list fetch, auto-update and default plugins are off: the container has no internet.
set -u
cd /workspace
M=gw/${MX_MODEL:-zai/glm-5.3}
export OPENCODE_DISABLE_MODELS_FETCH=1 OPENCODE_DISABLE_AUTOUPDATE=1 OPENCODE_DISABLE_DEFAULT_PLUGINS=1 \
  OPENCODE_DISABLE_LSP_DOWNLOAD=1
# OpenCode has no native shell snapshot. When the scenario provides ~/.bash_env (s4), export it as BASH_ENV:
# an agent-writable file that bash loads for every shell-tool call (the paper's substitute, Section 3.1).
[ -f "$HOME/.bash_env" ] && export BASH_ENV="$HOME/.bash_env"
n=0
for t in "$1"/turns/*.txt; do
  n=$((n+1))
  if [ "$n" -eq 1 ]; then cont=(); else cont=(--continue); fi
  echo "{\"mx_turn\": $n, \"file\": \"$(basename "$t")\"}"
  opencode run "${cont[@]}" --format json --model "$M" "$(cat "$t")"
  echo "{\"mx_turn_rc\": $?}"
done
