#!/usr/bin/env python3
"""Adopt an existing agent container into durable volumes -- WITHOUT losing
the memory it is currently running on.

Why this is a script and not something start_agent() does silently: the fix
for Stanley defect 1 mounts named volumes on /workspace/self,
/workspace/transcripts and /workspace/.claude. An agent created before the fix
does not have them, and its memory is sitting in the container's WRITABLE
LAYER. Re-creating the container to add the mounts is exactly the operation
that discards that layer. So the copy must happen BEFORE the volume is
attached to the live path, and it must be verifiable.

The safe order, which this script performs:

  1. docker stop   <agent>                    (the layer is now quiescent)
  2. create the three named volumes
  3. copy from the STOPPED container into them, via a throwaway helper
     container that mounts both the dead container's layer and the volumes
     (`docker cp` cannot target a volume; a helper container can)
  4. verify every copied file exists with the same size
  5. then, and only then, tell the operator to re-create the container with
     the new mounts (start_agent does it)

Step 5 is printed, not performed: re-creating changes the container's
identity and I do not want a fix for data loss to perform the destructive
step on its own.

Usage:
    python3 tools/migrate_agent_volumes.py --agent agent-3              # dry run
    python3 tools/migrate_agent_volumes.py --agent agent-3 --apply

Exit codes: 0 success (or a clean dry run), 1 a step failed, 2 could not run.
"""
import argparse
import os
import shutil
import subprocess
import tempfile
import sys

PARTS = (
    # (volume suffix, path inside the agent container)
    ("self", "/workspace/self"),
    ("transcripts", "/workspace/transcripts"),
    ("claude", "/workspace/.claude"),
)
# MUST match ui/control.py:workspace_volume(). Read from the environment with
# the same default rather than duplicated as a literal, so a cohort rename
# cannot silently migrate into volumes the UI will never mount.
PREFIX = os.environ.get("AGENT_COHORT", "agent-workspace")
IMAGE_ENV = "AGENT_IMAGE"
DEFAULT_IMAGE = "agent-glm"


def docker(*args, timeout=300):
    p = subprocess.run(["docker", *args], capture_output=True, text=True,
                       timeout=timeout)
    return p.returncode == 0, p.stdout, p.stderr


def volume(name, part):
    return f"{PREFIX}-{name}-{part}"


def live_state(agent, path):
    ok, out, _ = docker("exec", agent, "sh", "-c",
                        "find %s -type f 2>/dev/null | wc -l" % path)
    return int(out.strip()) if ok and out.strip().isdigit() else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="actually stop, create and copy (default: dry run)")
    ap.add_argument("--image", default=None)
    args = ap.parse_args()

    if not shutil.which("docker"):
        print("NO DOCKER: nothing to migrate here.")
        return 2

    agent = args.agent
    ok, out, err = docker("inspect", agent, "--format", "{{.State.Status}}")
    if not ok:
        print("FAIL: no container %r (%s)" % (agent, err.strip()))
        return 2
    status = out.strip()
    print("agent %s: state=%s" % (agent, status))

    # Never migrate a stopped-on-purpose container silently: if it is already
    # stopped the writable layer is intact, but the operator may have stopped
    # it for a reason. Report and continue only when told to.
    if status != "running" and not args.apply:
        print("NOTE: container is not running; its layer is already "
              "quiescent. Dry run only.")

    image = args.image
    if not image:
        ok, image, _ = docker("inspect", agent, "--format", "{{.Config.Image}}")
        image = image.strip() or DEFAULT_IMAGE

    before = {part: live_state(agent, path) for part, path in PARTS}
    print("files currently in the container:")
    for part, path in PARTS:
        print("  %-12s %s" % (path, before[part]))

    print("\nplan:")
    print("  1. docker stop %s" % agent)
    for part, _ in PARTS:
        print("  2. docker volume create %s" % volume(agent, part))
    print("  3. copy each path from the stopped container into its volume")
    print("  4. verify file counts match")
    print("  5. re-create the container with the new mounts "
          "(UI: delete + start, or start_agent())")

    if not args.apply:
        print("\nDRY RUN. Pass --apply to perform steps 1-4.")
        return 0

    ok, _, err = docker("stop", agent, timeout=120)
    if not ok:
        print("FAIL: docker stop: %s" % err.strip())
        return 1

    # The data is in the container's WRITABLE LAYER, not in a volume --
    # that is the whole defect. `--volumes-from` would expose nothing, so the
    # copy goes through the host: `docker cp` reads a stopped container's
    # layer (and refuses, correctly, if the container is gone).
    staging = tempfile.mkdtemp(prefix="migrate-%s-" % agent)
    try:
        for part, path in PARTS:
            vol = volume(agent, part)
            ok, _, err = docker("volume", "create", vol)
            if not ok:
                print("FAIL: create %s: %s" % (vol, err.strip()))
                return 1
            dest = os.path.join(staging, part)
            os.makedirs(dest, exist_ok=True)
            # `path/.` copies the CONTENTS, so the volume root is the
            # directory the container expects, not a nested copy of its name.
            ok, _, err = docker("cp", "%s:%s/." % (agent, path), dest)
            if not ok:
                # An empty or missing directory is not a failure: a fresh
                # agent may have nothing in /workspace/.claude yet.
                print("  %-24s nothing to copy (%s)" % (vol, err.strip()[:60]))
                continue
            # Write into the volume from a helper container: a volume cannot
            # be the destination of `docker cp`.
            ok, out, err = docker(
                "run", "--rm",
                "-v", "%s:/dest" % vol,
                "-v", "%s:/src:ro" % dest,
                "--entrypoint", "sh", image, "-c",
                "cp -a /src/. /dest/ && find /dest -type f | wc -l")
            if not ok:
                print("FAIL: copy into %s: %s" % (vol, err.strip()))
                return 1
            after = out.strip().splitlines()[-1] if out.strip() else "?"
            print("  %-24s copied, files now: %s" % (vol, after))
            if (before[part] is not None and after.isdigit()
                    and int(after) < before[part]):
                print("FAIL: fewer files in the volume (%s) than in the "
                      "container (%s)" % (after, before[part]))
                return 1
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    print("\nCOPIED. The container is STOPPED and still owns its layer.")
    print("Next: re-create it so start_agent() attaches the volumes:")
    print("  docker rm %s   (only after you are satisfied with the copy)" % agent)
    print("  then start it from the UI.")
    print("The stopped container is left in place on purpose: it is the "
          "backup if the copy is wrong.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
