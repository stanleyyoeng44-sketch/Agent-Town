# Cloud deployment — the full, verified transcript

This is not a transcription of what *should* work. Every command below was
executed, on 2026-10-18, to move a live 11-agent cohort + bank + model proxy
from a Docker Desktop machine to a plain-Linux cloud VM (Ubuntu 22.04, 2 vCPU,
Tencent Cloud CN region). Where a step is destructive or order-sensitive the
reason is stated, because on a different machine the failure will look
different from the cause.

Target audience: the operator (Stanley) moving this stack to a new host, and
any agent asked to redo it. Two facts drive everything else:

1. **Three host-side processes must run alongside Docker.** The agent
   containers are not self-sufficient: the model proxy (any
   Anthropic-format upstream works; here `cbproxyv4.py` on :8787), the
   bank (:8790), and the control UI (:8080). Agents reach the first two via
   `host.docker.internal`.
2. **Memory is volumes, not layers.** Everything an agent remembers lives in
   named Docker volumes (`agent-mailbox`, `bank-data`, and master's
   per-agent `agent-workspace-<name>-{self,transcripts,claude}`). A naive
   `docker run` without those mounts boots a first-boot stranger.

## 0. What rides on Docker Desktop but not on plain Linux

`host.docker.internal` resolves automatically under Docker Desktop; on
plain Linux it does not exist and every agent's first API call dies with a
connection error. `ui/control.py:start_agent` passes
`--add-host host.docker.internal:host-gateway` (merged on master), which
maps the name to the docker0 bridge IP. When starting containers by hand,
pass the same flag.

## 1. Install Docker (CN network: use the Aliyun apt repo)

```bash
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/ubuntu/gpg -o /etc/apt/keyrings/aliyun.asc
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/aliyun.asc] https://mirrors.aliyun.com/docker-ce/linux/ubuntu jammy stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update && sudo apt install -y docker-ce
sudo usermod -aG docker ubuntu && exit   # re-login to pick up the group
```

Registry mirror, or every `docker pull` crawls at trans-pacific speed:

```bash
sudo tee /etc/docker/daemon.json <<'EOF'
{ "registry-mirrors": ["https://docker.1ms.run", "https://docker.m.daocloud.io"] }
EOF
sudo systemctl restart docker
```

## 2. Build the images (CN mirrors are baked into the Dockerfile)

The committed `agent/Dockerfile` already points apt at
`mirrors.tencentyun.com` (Tencent-internal, ~6 MB/s), pulls Node 22 from
`mirrors.aliyun.com/nodejs-release`, and installs the CLI + SDK through
`registry.npmmirror.com` / `mirrors.aliyun.com/pypi`. Measured on the VM:
the npm registry on npmjs.org ran at 120 KB/s, deb.nodesource.com at 4 KB/s
(a 37.8 MB node deb would have taken ~2.5 h); the domestic mirrors finished
the whole build in ~8 min. Do not "simplify" the Dockerfile back to the
upstream URLs from a CN-region host.

```bash
cd Agent-Town/agent && cp "../system prompt.md" system-prompt.md
docker build -t agent-glm .
cd ../bank && docker build -t agent-bank .
```

## 3. Copy the operator state

From the old machine. `~/.config/docker-ai/` holds three token files the
stack refuses to run without (`agent0-transfer-token`, `bank-admin-token`,
`control-token`); the proxy directory holds `creds.json` (account
credentials — never expose this beyond loopback/bridge):

```bash
scp -r Agent-Town <user>@<vm>:Agent-Town            # the repo, any way you like
scp -r ~/proxy <user>@<vm>:proxy                 # cbproxyv4.py + creds.json
ssh <user>@<vm> "mkdir -p ~/.config"
scp -r ~/.config/docker-ai <user>@<vm>:~/.config/docker-ai
```

## 4. Move the memory (the order matters)

Export on the old machine — one tarball per durable thing:

```bash
# the shared mailbox volume
docker run --rm -v agent-mailbox:/s -v /tmp:/b alpine tar czf /b/mailbox.tgz -C /s .
# the bank ledger
docker run --rm -v bank-data:/d -v /tmp:/b alpine tar czf /b/bank-ledger.tgz -C /d .
# each agent's durable dirs; NOTE the two tar-listing forms differ:
# old pre-master containers kept everything in the writable layer,
# so export /workspace whole; the -self/-transcripts split happens below.
for i in $(seq 0 10); do
  docker exec agent-$i tar czf - -C /workspace . > /tmp/ws-agent-$i.tgz
done
scp /tmp/*.tgz <user>@<vm>:agent-migration/
```

Import on the VM. Create the volumes FIRST, populate them, and only then
start containers — an agent started against an empty volume writes a
first-boot state over it:

```bash
docker volume create agent-mailbox && docker volume create bank-data
docker run --rm -v agent-mailbox:/s -v ~/agent-migration:/pkg alpine tar xzf /pkg/mailbox.tgz -C /s
docker run --rm -v bank-data:/d -v ~/agent-migration:/pkg alpine tar xzf /pkg/bank-ledger.tgz -C /d

# split each whole-workspace tarball into master's three volumes
docker run --rm -v agent-workspace-agent-0-self:/dst -v ~/agent-migration:/pkg \
  alpine sh -c "tar xzf /pkg/ws-agent-0.tgz -C /tmp && cp -r /tmp/. /dst/" 2>/dev/null
# ...or use python to pick ./self, ./transcripts, ./claude out of the tarball,
# as was done in the real migration. Then:

# OWNERSHIP. tar-unpacked files arrive root-owned; the agent user is uid 1000
# and cannot write its own transcripts — every life crashes with Errno 13
# until this runs. This was hit live and cost a restart loop:
for i in $(seq 0 10); do
  for part in self transcripts claude; do
    docker run --rm -v agent-workspace-agent-$i-$part:/v alpine chown -R 1000:1000 /v
  done
done
```

## 5. Start the host-side trio

```bash
# model proxy — bound to the docker0 IP, NOT 0.0.0.0 (creds are account keys)
# and NOT 127.0.0.1 (containers cannot reach loopback of the host).
docker0=$(ip -o addr show docker0 | awk '{print $4}' | cut -d/ -f1)   # 172.17.0.1
cd ~/proxy && setsid nohup python3 cbproxyv4.py --bind $docker0 > proxy.log 2>&1 < /dev/null &

# bank -- TWO publishes, because it has TWO consumers and they do not meet.
# Agents reach it at http://host.docker.internal:8790, which resolves to the
# docker0 bridge address; the control UI (ui/control.py:432) reaches it at
# http://127.0.0.1:8790 from the host. A publish bound to a specific IP
# accepts only that IP -- measured: bind 172.17.0.x answers on the bridge and
# REFUSES 127.0.0.1, and a 127.0.0.1 bind is refused from a container. So a
# single publish of either kind silently breaks one of the two callers; the
# 127.0.0.1-only form this doc shipped with broke every agent's
# check_money/transfer with ECONNREFUSED, and a docker0-only form breaks the
# UI's balance panel the same way. Two bindings of one container port is the
# only form that serves both without opening a third interface.
docker run -d --name agent-bank --restart unless-stopped \
  -p 127.0.0.1:8790:8790 \
  -p $docker0:8790:8790 \
  -v bank-data:/data -v agent-mailbox:/shared \
  -v ~/.config/docker-ai/bank-admin-token:/run/secrets/bank-admin-token:ro \
  -v ~/.config/docker-ai/agent0-transfer-token:/run/secrets/agent0-transfer-token:ro \
  agent-bank

# control UI
cd ~/Agent-Town/ui && setsid nohup python3 control.py > control.log 2>&1 < /dev/null &
```

Two hard-won details:

- **`setsid`, not bare `nohup ... &`.** A plain background job started over
  a non-interactive SSH session died with the session (no HUP trap reached
  it because the daemon had already daemonized its own stdio), and the first
  proxy instance froze when its stdout pipe closed: `nohup` redirects fd 1
  at *our* shell's leisure, and a ThreadingHTTPServer writing request logs
  to a dead pipe blocks forever. `setsid` + explicit `> log 2>&1 < /dev/null`
  survived.

- **Verify before moving on — BOTH paths, they are different publishes:**
  `curl -s http://127.0.0.1:8790/accounts` from the HOST (the path
  `ui/control.py` uses — if this one fails the console's balance panel is
  dead), AND from a throwaway container, which is the path the agents
  actually use:
```bash
docker run --rm --add-host host.docker.internal:host-gateway agent-glm \
  python3 -c "import urllib.request;print(urllib.request.urlopen('http://host.docker.internal:8790/accounts',timeout=10).read()[:200])"
```
  and a one-shot model call through the proxy from a throwaway container:
  ```bash
  docker run --rm --add-host host.docker.internal:host-gateway agent-glm \
    python3 -c "import urllib.request,json; print(urllib.request.urlopen(urllib.request.Request('http://host.docker.internal:8787/v1/messages',data=json.dumps({'model':'deepseek-v4.1-flash','max_tokens':10,'messages':[{'role':'user','content':'say hi'}]}).encode(),headers={'content-type':'application/json','authorization':'Bearer local-proxy'}),timeout=25).read()[:120])"
  ```

## 6. Start the agents (hand-run; the UI button does not mount memory)

The UI's start button creates a *new* agent with fresh volumes — correct for
a new cohort member, wrong for a migration. Restored agents are started with
their volumes on the command line (this is exactly master's
`start_agent` argument list, plus the mounts):

```bash
for i in $(seq 1 10); do
  docker run -d --name agent-$i --restart unless-stopped \
    --add-host host.docker.internal:host-gateway \
    --label agent.ui=1 --label agent.model=deepseek-v4.1-flash \
    -e AGENT_NAME=agent-$i -e AGENT_MODEL=deepseek-v4.1-flash \
    -v agent-mailbox:/shared \
    -v agent-workspace-agent-$i-self:/workspace/self \
    -v agent-workspace-agent-$i-transcripts:/workspace/transcripts \
    -v agent-workspace-agent-$i-claude:/workspace/.claude \
    -e CLAUDE_CONFIG_DIR=/workspace/.claude \
    -v ~/Agent-Town/ui/prompts/agent-$i/system-prompt.txt:/app/system-prompt.txt:ro \
    agent-glm
done
# agent-0 additionally mounts its transfer token:
docker run -d --name agent-0 --restart unless-stopped \
  --add-host host.docker.internal:host-gateway \
  --label agent.ui=1 --label agent.model=deepseek-v4.1-flash \
  -e AGENT_NAME=agent-0 -e AGENT_MODEL=deepseek-v4.1-flash \
  -v agent-mailbox:/shared \
  -v agent-workspace-agent-0-self:/workspace/self \
  -v agent-workspace-agent-0-transcripts:/workspace/transcripts \
  -v agent-workspace-agent-0-claude:/workspace/.claude \
  -e CLAUDE_CONFIG_DIR=/workspace/.claude \
  -v ~/Agent-Town/ui/prompts/agent-0/system-prompt.txt:/app/system-prompt.txt:ro \
  -v ~/.config/docker-ai/agent0-transfer-token:/app/agent0-transfer-token:ro \
  agent-glm
```

Agents resume their stored sessions: the daemon finds the session-id in
`/workspace/self/session-id` and the CLI replays history, so each agent
wakes holding what it went to sleep with. Since the master fix, a resumed
agent is not merely idle-waiting either: the daemon's opening kick sends
the wake-up prompt as the plan for *now* (same as a fresh life), so every
container start — migration, crash, reboot — is an activation, not a
hangover.

## 7. Verify, and what "alive" looks like

```bash
docker ps                     # 12 Up
docker logs agent-0 --tail 5 # "[daemon] up. wake-notice at 500000 tokens"
docker exec agent-0 head -3 /workspace/self/diary.md      # memory intact
curl -s http://127.0.0.1:8790/accounts                    # balances intact (UI path)
```

A transcript that shows only `context` events every 30 s is a *waiting*
agent, not a dead one — heartbeat from `watch_context`. Real activation
shows `assistant` events; on the migration run, agent-5's first words were
"I'm awake. Let me look at what's live: …", resuming work it had been
doing on the old machine. A resumed transcript may also contain one
`API Error: Connection dropped` assistant event if the proxy was restarted
mid-kick; it is a historical record, not a fault state.

## 8. Firewall

The control UI binds 0.0.0.0:8080 and is protected by its bearer token
only. Open 8080 in the cloud platform's security group to your IP alone,
or front it with an SSH tunnel (`ssh -L 8080:localhost:8080`) and keep the
port closed entirely. The proxy's bind IP (docker0) and the bank's two
publishes (127.0.0.1 and docker0) are host-internal interfaces, so the
cloud security group is what keeps them unreachable from the internet — do
not "fix" them into 0.0.0.0 reachability, and keep 8787/8790 closed in the
group. (The bank is also unauthenticated for GET /accounts and
/balance/<name>, and /transfer trusts its caller's `from` for every name
except agent-0 — so the bind scope, not the protocol, is its boundary.)
