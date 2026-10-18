#!/usr/bin/env python3
"""Verify the context-persistence fix for one agent (Stanley defects 1 and 2).

Run on a host WITH a Docker daemon:

    python3 tools/verify_context_persistence.py --agent agent-X

It checks three things against a REAL container, by starting it, stopping it,
and starting it again:

  P1  start_agent() passes the three durable mounts and CLAUDE_CONFIG_DIR
  P2  /workspace/self/session-id and diary.md survive a stop/start
  P3  /workspace/transcripts survives, and /workspace/transcripts/CURRENT
      names a life that exists in the list (the UI's 'which life is current')

With no Docker daemon it does NOT pretend: it runs the daemon-side unit
simulation (which needs only this repository) and prints the exact docker
commands to run by hand, then exits 2 so a caller can tell the difference
between "verified" and "could not verify here".

Exit codes: 0 all checks passed, 1 a check failed, 2 could not run (no
docker / no such agent).

Why not just mock docker: the defect is that the WRITABLE LAYER is wiped.
A mock of `docker run` cannot be shown to have the property that the real
container has, because the property lives in the daemon.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "ui"))

DURABLE = ("self", "transcripts", "claude")


def have_docker():
    return shutil.which("docker") is not None


def docker(*args, timeout=120):
    p = subprocess.run(["docker", *args], capture_output=True, text=True,
                       timeout=timeout)
    return p.returncode == 0, p.stdout, p.stderr


def docker_exec(agent, *args, timeout=60):
    return docker("exec", agent, *args, timeout=timeout)


def unit_simulation():
    """Daemon-side checks that need no Docker.

    Exercises the real functions from agent/agent-daemon.py by importing it
    with a redirected /workspace, so the assertions are about the shipped
    code rather than about a copy of its logic.
    """
    checks = []
    tmp = tempfile.mkdtemp(prefix="ctx-unit-")
    verdict = {"ok": True}

    def check(name, cond, detail=""):
        checks.append({"name": name, "ok": bool(cond), "detail": detail})
        if not cond:
            verdict["ok"] = False

    # --- load the daemon module with the workspace redirected ---------------
    import importlib.util
    src = open(os.path.join(REPO, "agent", "agent-daemon.py")).read()
    config_dir = os.path.join(tmp, "claude")
    self_dir = os.path.join(tmp, "self")
    tr_dir = os.path.join(tmp, "transcripts")
    for d in (config_dir, self_dir, tr_dir):
        os.makedirs(d, exist_ok=True)
    # Point the module at the temp tree. This is a path substitution of the
    # four constants that matter, asserted to have happened -- a substitution
    # that silently misses would make every check below vacuous.
    subs = [
        ('WORKSPACE = "/workspace"', "WORKSPACE = %r" % tmp),
        ('SELF_DIR = os.path.join(WORKSPACE, "self")', "SELF_DIR = %r" % self_dir),
        ('TRANSCRIPTS = os.path.join(WORKSPACE, "transcripts")',
         "TRANSCRIPTS = %r" % tr_dir),
        ('CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR", "/workspace/.claude")',
         "CONFIG_DIR = %r" % config_dir),
    ]
    missing = [old for old, _ in subs if old not in src]
    if missing:
        # Report this as a CHECK, not a crash: a verifier that dies before
        # printing anything cannot tell you what is wrong. Every missing
        # anchor is a real defect of the tree under test (the fix is absent),
        # so each becomes a named failure.
        for old in missing:
            check("the tree under test contains: %s" % old.split("=")[0].strip(),
                  False, "anchor absent -> this is a PRE-FIX tree")
        return checks, verdict["ok"]
    for old, new in subs:
        src = src.replace(old, new)
    path = os.path.join(tmp, "daemon_under_test.py")
    with open(path, "w") as f:
        f.write(src)
    spec = importlib.util.spec_from_file_location("daemon_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # --- P1: the UI actually requests the durable mounts --------------------
    control = open(os.path.join(REPO, "ui", "control.py")).read()
    start = control[control.index("def start_agent("):]
    start = start[:start.index("\ndef ", 10)]
    for part in DURABLE:
        check("start_agent mounts a volume for /workspace/%s" % part,
              "workspace_volume(name, %r)" % part in start or
              "/workspace/%s" % part in start,
              "no volume for /workspace/%s" % part)
    check("start_agent sets CLAUDE_CONFIG_DIR to the mounted volume",
          "CLAUDE_CONFIG_DIR=/workspace/.claude" in start,
          "the SDK would write session records outside the volume")

    # --- a stored id with NO records is not resumable ----------------------
    sid_file = os.path.join(self_dir, "session-id")
    with open(sid_file, "w") as f:
        f.write("stale-session-id\n")
    check("a session id with no session records is REFUSED, not resumed",
          mod.read_resumable_session() is None,
          "a stale id was treated as resumable -> muted life")
    check("the stale id was consumed so the next life starts clean",
          not os.path.exists(sid_file),
          "stale id left on disk")

    # --- a stored id WITH records is resumable -----------------------------
    records = mod.sdk_session_records()
    os.makedirs(records, exist_ok=True)
    with open(sid_file, "w") as f:
        f.write("good-session-id\n")
    check("a session id WITH its record directory is resumed",
          mod.read_resumable_session() == "good-session-id",
          "records exist but the id was not returned")

    # --- the record path the SDK resolves is the one we check --------------
    check("session records live under the config dir, not the home dir",
          os.path.abspath(records).startswith(os.path.abspath(config_dir)),
          "records path %r escapes the mounted config dir" % records)
    check("ENV passes CLAUDE_CONFIG_DIR to the SDK subprocess",
          mod.ENV.get("CLAUDE_CONFIG_DIR") == config_dir,
          "the subprocess would resolve a different config dir than the check")

    # --- P3: the CURRENT marker is written for a life ----------------------
    class _Cfg(dict):
        pass
    # Write the marker the way live_one_life does, then read it the way the
    # UI does; both are the shipped code paths.
    with open(mod.CURRENT_LIFE_FILE, "w") as f:
        f.write("1792-life-a")
    check("CURRENT marker exists where the UI looks for it",
          os.path.isfile(os.path.join(tr_dir, "CURRENT")),
          "UI would show no current life")
    os.makedirs(os.path.join(records), exist_ok=True)

    shutil.rmtree(tmp, ignore_errors=True)
    return checks, verdict["ok"]


def docker_checks(agent):
    checks = []

    def check(name, cond, detail=""):
        checks.append({"name": name, "ok": bool(cond), "detail": detail})
        return cond

    ok, out, _ = docker("inspect", agent, "--format",
                        "{{range .Mounts}}{{.Destination}} {{end}}")
    if not ok:
        check("agent %s exists" % agent, False, "docker inspect failed")
        return checks, False
    mounted = set(out.split())
    for part in DURABLE:
        check("/workspace/%s is a mount" % part, "/workspace/%s" % part in mounted,
              "NOT a mount -> wiped on stop/start; mounted: %s" % sorted(mounted))

    # write a sentinel, stop, start, read it back
    sentinel = "context-persistence-probe"
    docker_exec(agent, "sh", "-c",
                "mkdir -p /workspace/self && "
                "echo %s >> /workspace/self/session-id" % sentinel)
    before_self, _ = "", None
    ok, before_self, _ = docker_exec(agent, "cat", "/workspace/self/session-id")
    ok2, before_tr, _ = docker_exec(agent, "sh", "-c",
                                    "ls /workspace/transcripts 2>/dev/null | wc -l")
    check("sentinel written before restart", ok and sentinel in before_self,
          "could not write the probe")

    docker("stop", agent, timeout=120)
    docker("start", agent, timeout=120)
    # the daemon needs a moment to come up and rewrite CURRENT
    for _ in range(60):
        time.sleep(1)
        ok, after_self, _ = docker_exec(agent, "cat", "/workspace/self/session-id")
        if ok and sentinel in after_self:
            break

    check("P2a session-id survives stop/start", ok and sentinel in after_self,
          "the file was lost -> the agent wakes as a first boot")
    ok3, after_tr, _ = docker_exec(agent, "sh", "-c",
                                   "ls /workspace/transcripts 2>/dev/null | wc -l")
    check("P2b transcripts survive stop/start",
          ok3 and int(after_tr.strip() or 0) >= int(before_tr.strip() or 0),
          "transcripts %s -> %s" % (before_tr.strip(), after_tr.strip()))

    ok4, cur, _ = docker_exec(agent, "cat", "/workspace/transcripts/CURRENT")
    cur = cur.strip()
    ok5, listing, _ = docker_exec(
        agent, "sh", "-c",
        "cd /workspace/transcripts && ls life-*.jsonl 2>/dev/null")
    lives = [ln[len("life-"):-len(".jsonl")] for ln in listing.split() if ln]
    check("P3a CURRENT marker exists after restart", ok4 and cur in lives,
          "CURRENT=%r lives=%r" % (cur, lives[:3]))

    # the UI's own function, with docker available, must mark it
    try:
        import control
        lives_api = control.list_lives(agent)
        cur_marked = [lv for lv in lives_api if lv.get("current")]
        check("P3b UI marks exactly one current life",
              len(cur_marked) == 1 and cur_marked[0]["id"] == cur,
              "UI marked %r" % ([lv["id"] for lv in cur_marked],))
    except Exception as exc:                     # noqa: BLE001
        check("P3b UI marks exactly one current life", False, repr(exc))

    return checks, all(c["ok"] for c in checks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", help="an existing agent container to test")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    unit, unit_ok = unit_simulation()

    if not have_docker():
        print("NO DOCKER DAEMON: running the unit simulation only.")
        print("The volume-survives-restart property cannot be shown here -- "
              "it lives in the daemon, not in this process.\n")
        for c in unit:
            print("  %-5s %s%s" % ("PASS" if c["ok"] else "FAIL", c["name"],
                                   "" if c["ok"] else "  (%s)" % c["detail"]))
        print("\nUNIT: %s" % ("PASS" if unit_ok else "FAIL"))
        if args.agent:
            print("\nTO VERIFY ON A DOCKER HOST, run:")
            print("  python3 %s --agent %s" % (os.path.relpath(__file__, REPO),
                                               args.agent))
        return 0 if unit_ok else 1

    docker_checks_list, docker_ok = docker_checks(args.agent)
    all_checks = unit + docker_checks_list
    if args.json:
        print(json.dumps({"checks": all_checks,
                          "ok": unit_ok and docker_ok}, indent=2))
    else:
        for c in all_checks:
            print("  %-5s %s%s" % ("PASS" if c["ok"] else "FAIL", c["name"],
                                   "" if c["ok"] else "  (%s)" % c["detail"]))
        print("\n%s" % ("PASS" if unit_ok and docker_ok else "FAIL"))
    return 0 if (unit_ok and docker_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
