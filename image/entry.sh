#!/bin/bash
# Container entry point (root). Starts the gateway forwarder, then strace -f around the agent launcher,
# which runs as uid 23001.
# /observe is a root-only (0700) bind mount: the agent can neither read nor change the strace log.
# Usage (set by record_run.sh): entry.sh <agent>   with /scenario mounted read-only.
set -u
AGENT="$1"
OUT=/observe
SYSCALLS='/^(open|openat|openat2|creat|unlink|unlinkat|rmdir|rename|renameat|renameat2|truncate|ftruncate|link|linkat|symlink|symlinkat|mkdir|mkdirat|chmod|fchmod|fchmodat|fchmodat2|utimensat|fallocate|execve|execveat|clone|clone3|fork|vfork|exit_group|kill|tgkill|setsid|ptrace|connect|bind|listen)$'
date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$OUT/entry_start"
# Model gateway: the container has no network; a root forwarder turns 127.0.0.1:8080 into the
# host bridge's socket (root-only directory /run/mx-gw). It is not under strace (it is not the agent).
if [ -S /run/mx-gw/gw.sock ]; then
  python3 -I /opt/mx/gw_forward.py /run/mx-gw/gw.sock 8080 > "$OUT/gw_forward.log" 2>&1 &
  for _ in $(seq 1 50); do grep -q forwarding "$OUT/gw_forward.log" 2>/dev/null && break; sleep 0.1; done
fi
strace -f -q -ttt -yy -s 4096 --seccomp-bpf -e signal=none -e trace="$SYSCALLS" -o "$OUT/strace.log" -- \
  setpriv --reuid=23001 --regid=23001 --clear-groups --inh-caps=-all --bounding-set=-all --no-new-privs \
  env -i HOME=/home/agent USER=agent LOGNAME=agent SHELL=/bin/bash LANG=C.UTF-8 TERM=dumb \
      PATH=/usr/local/bin:/usr/bin:/bin MX_AGENT="$AGENT" ${MX_MODEL:+MX_MODEL="$MX_MODEL"} \
  /opt/mx/launchers/"$AGENT".sh /scenario
rc=$?
echo "$rc" > "$OUT/entry_rc"
date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$OUT/entry_end"
exit "$rc"
