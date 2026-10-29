#!/bin/bash
# Isolation self-test: runs as the agent (uid 23001, under strace) and prints what the agent can and
# cannot reach. No model. Usage: record_run.sh --agent probe --scenario s0_smoke
set -u
ok() { echo "ok    $*"; }
bad() { echo "BAD   $*"; }
echo "id: $(id)"; grep -E 'TracerPid|CapEff' /proc/self/status
ls /observe >/dev/null 2>&1 && bad "can list /observe" || ok "/observe not readable"
(echo x >> /observe/strace.log) 2>/dev/null && bad "can write the strace log" || ok "strace log not writable"
ls /run/mx-gw >/dev/null 2>&1 && bad "can list /run/mx-gw" || ok "/run/mx-gw (bridge socket) not reachable"
kill -9 1 2>/dev/null && bad "killed pid 1" || ok "cannot kill pid 1 (root)"
echo "interfaces: $(ls /sys/class/net | tr '\n' ' ')"
# Public internet, a public DNS server, the Docker host via its default bridge, and the gateway port
# on the container's own loopback (the gateway listens on the host's loopback, not this one).
for target in 1.1.1.1:443 8.8.8.8:53 172.17.0.1:22 172.17.0.1:3425 127.0.0.1:3425; do
  timeout 4 bash -c "</dev/tcp/${target%:*}/${target#*:}" 2>/dev/null && bad "$target reachable" || ok "$target not reachable"
done
getent hosts example.com >/dev/null 2>&1 && bad "DNS works" || ok "no DNS"
ls /var/run/docker.sock >/dev/null 2>&1 && bad "docker.sock present" || ok "no docker.sock"
GW=http://127.0.0.1:8080
echo "models via gateway: $(curl -s -m 10 $GW/v1/models)"
code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' $GW/api/settings); [ "$code" = 404 ] && ok "other gateway paths: 404" || bad "other gateway path: $code"
code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' $GW/v1/messages -H 'content-type: application/json' \
  -d '{"model":"some-other-model","max_tokens":5,"messages":[{"role":"user","content":"hi"}]}')
[ "$code" = 403 ] && ok "other model ids: 403" || bad "other model id: $code"
env | grep -i -E 'key|token|secret' | sed 's/=.*/=<set>/' | sed 's/^/env: /'
exit 0
