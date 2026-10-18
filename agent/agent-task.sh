#!/bin/bash
# Submit a task to the agent daemon from outside the container:
#   docker exec glm-agent agent-task "do something"
# Writes the task into /workspace/inbox and tails the reply file until the
# daemon finishes it, so the caller sees the agent's live output.

set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "usage: docker exec glm-agent agent-task \"your task here\"" >&2
  exit 2
fi

TASK_FILE="/workspace/inbox/$(date +%s%N).task"
REPLY_FILE="${TASK_FILE%.task}.reply"

printf '%s\n' "$*" > "$TASK_FILE"

# Wait for the reply, then print it. Tail the daemon's transcript stream
# is not possible from here; the daemon echoes agent output to its own
# stdout, visible via `docker logs -f glm-agent`.
if [ -t 0 ]; then
  echo "[waiting for agent...]" >&2
fi
while [ ! -s "$REPLY_FILE" ]; do
  sleep 2
done
cat "$REPLY_FILE"
