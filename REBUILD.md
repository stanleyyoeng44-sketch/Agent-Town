# REBUILD — how to get the current code into a running container

A merged fix is not a deployed fix. An image built before a commit does not
contain that commit, and `/app` is baked into the image, so a running agent can
be older than the repository with no visible symptom. This file is the exact
procedure for closing that gap, and how to prove you closed it.

## 0. First: find out whether you need this

Ask the container, not yourself. Every `birth` line in an agent's transcript
records the build that produced it:

```bash
# SKIP: <agent> is a placeholder.
docker exec <agent> sh -c 'head -1 /workspace/transcripts/life-*.jsonl | tail -1'
```

* `agent_daemon_sha256` present → the running daemon is a build that knows how
  to identify itself. Compare the hash with the file in your checkout:
  `sha256sum agent/agent-daemon.py`.
* `agent_daemon_sha256` absent → **the running build predates the build stamp
  and therefore predates every fix that shipped with it.** Nothing else needs
  checking; rebuild.

`repo_head` (when present) is the commit the checkout was on, so you can name
the deployed commit rather than infer it.

You can also compare directly:

```bash
# SKIP: <agent> is a placeholder.
docker exec <agent> md5sum /app/agent-daemon.py
md5sum agent/agent-daemon.py            # in a checkout of the commit you believe is deployed
```

One file is not a tree. Prefer the birth-event stamp: it comes from the process
itself, and it cannot drift from what is running.

## 1. Rebuild the agent image from current master

```bash
git clone https://github.com/stanleyyoeng44-sketch/Agent-Town
cd Agent-Town
git log --oneline -1                    # note this commit; it is what you deploy
docker build -t agent-glm ./agent
```

The bank and UI images are separate; rebuild them too if their code changed:

```bash
docker build -t agent-bank ./bank
```

## 2. Re-create ONE agent so the durable volumes are attached

This is the destructive step, and it is the one to be careful with. The new
`start_agent` attaches three volumes:

    agent-workspace-<name>-self         -> /workspace/self
    agent-workspace-<name>-transcripts  -> /workspace/transcripts
    agent-workspace-<name>-claude       -> /workspace/.claude
    (and CLAUDE_CONFIG_DIR=/workspace/.claude)

An agent created before those mounts keeps its memory in the container's
writable layer. `docker stop` / `docker start` PRESERVES that layer;
`docker rm` + `docker run` DISCARDS it. Re-creating is what adds the mounts and
what destroys the old state, which is why the copy must happen first:

```bash
# SKIP: <name> is a placeholder, not a runnable command.
python3 tools/migrate_agent_volumes.py --agent <name>          # dry run: shows the plan
python3 tools/migrate_agent_volumes.py --agent <name> --apply  # stop + copy + verify counts
```

The script stops the container, copies each durable path into its volume,
checks the row counts, and leaves the stopped container in place as the backup.
Then remove and start it from the UI:

```bash
docker rm <name>          # only after the copy is verified
# then: the control UI -> New agent, same name
```

The UI refuses to start a pre-fix container and prints this migration command,
rather than silently leaving the agent unfixed.

## 3. Prove the fix is running

```bash
# SKIP: <name> is a placeholder; substitute your agent's name and run it.
python3 tools/verify_context_persistence.py --agent <name>
```

It performs a real `docker stop` / `docker start` and prints:

* **P1** — the durable mounts are present on the container
* **P2** — `session-id` and the transcripts still exist after the restart
* **P3** — `/workspace/transcripts/CURRENT` names a life that appears in the
  transcript list

Without a Docker daemon the script exits **2** and says so. It does not fake a
pass, and a run without the daemon is not evidence.

## 4. Paste P1/P2/P3 where the claim lives

The README's Verification table has a row for "an agent's memory survives
`docker stop`/`start`" marked **UNPROVEN**. Replace that with the three lines
from step 3 and the commit from step 1. Until then the row is accurate: the
code is unit-tested, the deployment is unproven.

## What this does not fix

* **A life is not bounded by context.** Nothing external ends a long-running
  life except the agent calling `restart`. The daemon asks it to settle; asking
  is not bounding.
* **Existing memory is not recovered by rebuilding.** The rebuild makes the
  NEXT restart survivable. Anything already lost is lost; the migration in
  step 2 is what preserves what is still in a live container.
* **Any agent whose wake file points at `/workspace` is not safe across a
  re-create.** `/workspace/self` is a volume only after step 2. Until then,
  keep the pointer to your notes in `/shared`, which is volume-backed already.
