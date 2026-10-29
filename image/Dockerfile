# Agent sandbox image: two coding agents (no login; they reach a model only through the run's gateway
# bridge), strace, the gateway forwarder, and the fake agent.
# Build (context = kit/): docker build -f image/Dockerfile -t mx-agents:2026-10-28 .
FROM node:22-trixie-slim@sha256:2117f713a417f3aff682ac8f5bc2ebbdf1beed51f36e67ab1e0fd3866b6d2d8f

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      strace=6.13+ds-1 git python3 procps sqlite3 ca-certificates curl ripgrep jq less \
 && rm -rf /var/lib/apt/lists/*

ARG CLAUDE_CODE_VERSION=2.1.312
ARG OPENCODE_VERSION=1.19.5
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "opencode-ai@${OPENCODE_VERSION}" \
 && npm cache clean --force

# The agent user. A distinctive uid lets the host-side bpftrace filter on uid == 23001.
RUN groupadd -g 23001 agent && useradd -m -u 23001 -g 23001 -s /bin/bash agent

COPY image/entry.sh /opt/mx/entry.sh
COPY image/gw_forward.py /opt/mx/gw_forward.py
COPY image/launchers/ /opt/mx/launchers/
COPY fake_agent/fake_agent.py /opt/mx/fake_agent.py
RUN chmod 755 /opt/mx/entry.sh /opt/mx/gw_forward.py /opt/mx/launchers/*.sh

# Runs as root only to start strace; the agent itself runs as uid 23001 without capabilities.
ENTRYPOINT ["/opt/mx/entry.sh"]
