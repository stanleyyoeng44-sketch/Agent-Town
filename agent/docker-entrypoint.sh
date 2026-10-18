#!/bin/bash
# Runs as root, drops to the unprivileged 'agent' user, then runs the caller.

# Give the agent user ownership of the workspace so it can write there.
# It's a bind mount, so this chown applies on the host too: the folder ends
# up owned by the container's 'agent' user (uid 1000).
chown -R agent:agent /workspace 2>/dev/null || true

# setpriv (from util-linux, preinstalled) drops root; env sets the agent
# user's HOME so the CLI writes its state under /home/agent, not /root.
exec setpriv --reuid=agent --regid=agent --init-groups \
  env HOME=/home/agent PYTHONUNBUFFERED=1 \
  python3 /app/agent-run.py "$@"
