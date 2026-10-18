FROM python:3.12-slim

# Tencent-cloud friendly: use the in-country Debian mirror (mirrors.tencentyun.com
# is on the VM's internal network; deb.debian.org stalls from this region).
RUN sed -i "s|deb.debian.org|mirrors.tencentyun.com|g" /etc/apt/sources.list.d/debian.sources

# Tools the Claude Code CLI and the agent's Bash tool will want.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git ripgrep xz-utils \
 && rm -rf /var/lib/apt/lists/*

# Node.js 22 — the Claude Code CLI is a Node program.
# nodesource.com is throttled to ~4KB/s from CN clouds, so install from the
# npmmirror (Alibaba) Node binary mirror instead — pure domestic traffic.
ARG NODE_VERSION=22.14.0
# nodejs-release filenames use x64/arm64, Debian calls them amd64/arm64.
RUN ARCH=$(dpkg --print-architecture | sed 's/amd64/x64/') \
 && curl -fsSL https://mirrors.aliyun.com/nodejs-release/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${ARCH}.tar.xz \
    -o /tmp/node.tar.xz \
 && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
 && rm /tmp/node.tar.xz \
 && node --version && npm --version

# The Claude Code CLI itself. npmjs.org is slow from CN; use the npmmirror
# registry (domestic CDN) and disable the CLI's auto-updater.
RUN npm install -g @anthropic-ai/claude-code --registry=https://registry.npmmirror.com

# The Python SDK that drives the CLI.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
      --index-url https://mirrors.aliyun.com/pypi/simple/

# The daemon, the one-shot runner, the task-submission helper, and the
# operator config. All owned by root in /app; the agent runs as the
# unprivileged 'agent' user, so it can read them but cannot write to them.
COPY agent-daemon.py agent-task.sh agent-config.json viewer.py /app/
# Life-cycle MCP server (restart/chat/list_peers) — stdio, launched per life.
COPY mcp-stdio.py /app/mcp-stdio.py
# The operator's system prompt. Edit "system prompt.md" in the parent
# directory (docker AI/), then copy it here before rebuilding:
#   cp "../system prompt.md" system-prompt.md
COPY system-prompt.md /app/system-prompt.txt
RUN chmod 755 /app/agent-daemon.py /app/agent-task.sh \
 && chmod 644 /app/agent-config.json /app/system-prompt.txt \
 && ln -s /app/agent-task.sh /usr/local/bin/agent-task

# Non-root user: the Claude Code CLI refuses bypassPermissions as root.
RUN useradd --create-home --shell /bin/bash agent \
 && mkdir -p /workspace /workspace/self /workspace/transcripts \
              /workspace/.claude /shared/mailbox \
 && chown -R agent:agent /workspace \
 && chmod 777 /shared/mailbox /shared

# Copy mcp.json into the image if you later add one (baked, root-owned):
# COPY mcp.json /app/mcp.json

# Long-running daemon as PID 1... as the 'agent' user.
USER agent
WORKDIR /workspace
# Declare the durable paths so a container restart (docker stop/start, a
# crash) does not wipe the agent's memory. Without these, /workspace lives in
# the container's writable layer and session-id, diary.md, wake.md and every
# transcript vanish together -- the agent wakes as a first-boot stranger and
# the daemon's crash-resume path can never fire. The control UI mounts a
# named volume per agent on each path (ui/control.py:start_agent).
VOLUME ["/workspace/self", "/workspace/transcripts", "/workspace/.claude"]
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/app/agent-daemon.py"]
