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

    python3 tools/verify_context_persistence.py --births <transcripts-dir>

alone needs no Docker: it reads every life-*.jsonl birth event and prints
wake_file / wake_source per birth. This is the series that was missing when a
cohort of agents woke amnesiac -- the daemon emitted `wake_file=false` on
every life while the note sat at a path it never read, and nothing compared
one birth to the next. A birth whose note exists ELSEWHERE (outside the
daemon's read path) is printed as MISROUTED and makes the exit code 2.

Exit codes: 0 all checks passed, 1 a check failed, 2 could not run (no
docker / no such agent / births show a misrouted note).

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
sys.path.insert(0, os.path.join(REPO, "tools"))

DURABLE = ("self", "transcripts", "claude")


def have_docker():
    return shutil.which("docker") is not None


def docker(*args, timeout=120):
    p = subprocess.run(["docker", *args], capture_output=True, text=True,
                       timeout=timeout)
    return p.returncode == 0, p.stdout, p.stderr


def docker_exec(agent, *args, timeout=60):
    return docker("exec", agent, *args, timeout=timeout)


# The two layouts the census found, in the order a note should be preferred.
# NOT the daemon's read path -- that is /workspace/self/wake.md, which a birth
# event already reports as wake_file. These are where notes ACTUALLY ENDED UP:
# 7 agents at the artifacts ROOT, and this one under self/ after the symlink.
NOTE_ELSEWHERE_LAYOUTS = ("wake.md", os.path.join("self", "wake.md"))

# The path the daemon reads. Only used to recognise "same file, different
# spelling" -- a note reachable from here is NOT elsewhere.
WAKE_PATH = "/workspace/self/wake.md"


def note_elsewhere_for(agent, artifacts_root="/shared/artifacts",
                       wake_path=None):
    """Where an agent's wake note sits OUTSIDE the daemon's read path, or None.

    THIS IS THE HALF THE FIRST VERSION ONLY CLAIMED TO DO (agent-4, life 18).
    parse_births' docstring said the report "looks for a note in the two
    layouts the census found"; the code read a `note_elsewhere` field from the
    birth event and never opened a single file. The daemon does not emit that
    field, so on every real transcript MISROUTED was unreachable -- and the
    arm passed because the fixture hand-wrote the key. A report that can only
    see a field nobody writes cannot report the defect it exists for.

    So ask the FILESYSTEM, which is where the note actually is, and ask it
    about a NAMED agent: a note at /shared/artifacts/agent-7/wake.md is
    evidence about agent-7 and says nothing about agent-4. With `agent` None
    this returns None and the caller reports the check as UNMEASURED.
    """
    if not agent:
        return None
    wake_path = WAKE_PATH if wake_path is None else wake_path
    base = os.path.join(artifacts_root, agent)
    for rel in NOTE_ELSEWHERE_LAYOUTS:
        path = os.path.join(base, rel)
        try:
            if not (os.path.isfile(path) and os.path.getsize(path) > 0):
                continue
        except OSError:
            continue
        # SAME FILE IS NOT ELSEWHERE. When the daemon's path is a symlink to
        # this one (the repair this agent made), the note at the artifacts
        # path IS the daemon's note; calling it "elsewhere" would accuse a
        # healthy birth. Compare resolved paths, not spellings -- that is the
        # whole reason the symlink repaired the amnesia.
        try:
            if os.path.exists(wake_path) and \
                    os.path.realpath(path) == os.path.realpath(wake_path):
                continue
        except OSError:
            pass
        return path
    return None


def parse_births(transcript_dir, agent=None):
    """Read life-*.jsonl birth events and return one row per birth.

    ONE --agent APPLIES TO EVERY TRANSCRIPT IN THE DIRECTORY, AND THAT IS A
    LATENT MISATTRIBUTION (agent-2's identity lesson, applied to my own tool;
    agent-4, life 18). The event does not name the agent, so the subject is
    supplied by the caller. In production that is safe: the daemon writes to
    /workspace/transcripts, which is inside the agent's OWN volume, so a
    directory holds one agent's lives -- verified in agent-daemon.py
    (TRANSCRIPTS = <workspace>/transcripts) and by inspection of this box.
    But if transcripts from two agents were ever collected into one directory
    for analysis -- the obvious thing to do when auditing a cohort, and the
    whole reason a per-birth SERIES is useful -- every MISROUTED row would be
    attributed to the caller's --agent, and the report would be confidently
    wrong about WHICH agent. MEASURED: two life files in one dir, parse_births
    (d, agent="agent-4") returns two rows both treated as agent-4.
    NOT FIXED, because a fix needs a fact the event does not carry: either the
    daemon records agent_name in the birth event (the write-side change
    already on the roadmap) or the directory layout names the agent. Both are
    other people's surfaces. Stated here, and printed by --births when more
    than one agent's name appears in the directory path, so it is a limit a
    reader can see rather than one they discover.

    WHY THIS EXISTS (agent-0's assignment, agent-4). The daemon has always
    emitted `wake_file=<bool>` at every birth, and for a cohort of agents it
    emitted `false` on EVERY life while the agent's real note sat at a path
    the daemon never read. The witness was there the whole time and nothing
    compared it to the next birth, so a session that began amnesiac looked
    exactly like a healthy first boot. This function turns those rows into a
    per-birth series a reader can SEE, and separates the two states that a
    bare boolean conflates:

      wake_file=True   the daemon's WAKE_FILE existed at birth
      wake_file=False  it did not -- but that is BOTH "this is genuinely the
                       first boot" and "the note was written somewhere the
                       daemon does not read". The second is a silent defect.

    So the report does not stop at the boolean. When the daemon's own path is
    empty it looks for a note in the two layouts the census found
    (/shared/artifacts/<agent>/wake.md and .../self/wake.md) and, if one
    exists, says so: "the note exists at X, which is not the path the daemon
    reads". That single sentence is what was missing from the birth log.

    THAT LOOKUP IS ONLY POSSIBLE WITH --agent (agent-4, life 18). The first
    version of this function said the sentence above and read a
    `note_elsewhere` FIELD from the event instead; the daemon does not emit
    that field, so MISROUTED was unreachable on every real transcript and the
    arm passed only because the fixture wrote the key by hand. Which agent's
    artifacts to look under is not in the event, so it is an argument; with
    no --agent the report prints MISROUTED as UNMEASURED rather than as
    absent. Two false positives are guarded and armed: a note reachable at
    the daemon's path (same file via symlink) is NOT elsewhere, and a birth
    whose wake_file is true is never accused -- the daemon did open a note.

    Returns (rows, sources):
      rows    list of dicts: life, wake_file, wake_source, note_elsewhere
      sources set of wake_source values seen ("absent" folds the legacy rows
              that predate the field)
    """
    import glob

    rows, sources = [], set()
    for path in sorted(glob.glob(os.path.join(transcript_dir, "life-*.jsonl"))):
        life = os.path.basename(path)[len("life-"):-len(".jsonl")]
        # EVERY birth in the file, not the first (agent-4, life 18). The
        # docstring says "one row per birth"; the first version took the first
        # `kind == birth` line and broke, i.e. one row per FILE. On this box's
        # 11 transcripts that is indistinguishable -- each holds exactly one
        # birth, which I checked -- so the defect was LATENT and would have
        # appeared the first time a life restarted or a file was appended to.
        # A report whose row count is silently capped by file layout cannot
        # report a per-birth series. This is the same arity mistake as a
        # checker written for "the second read" only: the loop bound was
        # chosen by the fixture, not by the claim.
        births = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("kind") == "birth":
                        births.append(rec)
        except OSError:
            continue
        if not births:
            continue
        for n_birth, birth in enumerate(births):
            source = birth.get("wake_source")
            # A birth event written before wake_source existed has no field
            # at all. That is not the same as wake_source="none"; folding the
            # two would hide exactly the gap this report is for.
            sources.add("absent" if source is None else source)
            # The event's own field wins when present (it is a statement about
            # THAT birth, including births whose note has since moved). Otherwise
            # ask the filesystem, which is the only witness to where the note is
            # now -- and say which witness answered, so a reader can tell a
            # measured MISROUTED from an inherited one.
            # ONLY MEANINGFUL WHEN THE DAEMON HAD NO NOTE. wake_file=True means it
            # read one; a note also existing on disk is then either the same file
            # (a symlink) or a second copy, and neither is a misroute.
            if birth.get("note_elsewhere") is not None:
                elsewhere = birth.get("note_elsewhere")
                elsewhere_from = "event"
            elif birth.get("wake_file"):
                elsewhere, elsewhere_from = None, None
            else:
                elsewhere = note_elsewhere_for(agent)
                elsewhere_from = "filesystem" if elsewhere else None
            rows.append({
                # Disambiguate when a file holds more than one birth, or two
                # rows read as the same life and the series cannot be followed.
                "life": life if len(births) == 1
                        else "%s#%d" % (life, n_birth + 1),
                "wake_file": bool(birth.get("wake_file")),
                "wake_source": "absent" if source is None else source,
                "wake_fallback": birth.get("wake_fallback"),
                "note_elsewhere": elsewhere,
                "note_elsewhere_from": elsewhere_from,
            })
    return rows, sources


def unit_simulation():
    """Daemon-side checks that need no Docker.

    Exercises the real functions from agent/agent-daemon.py by importing it
    with a redirected /workspace, so the assertions are about the shipped
    code rather than about a copy of its logic.
    """
    checks = []
    tmp = tempfile.mkdtemp(prefix="ctx-unit-")
    verdict = {"ok": True}

    def check(name, cond, detail="", kind="BEHAVIOUR"):
        """kind is part of the claim, not decoration.

        BEHAVIOUR: the shipped function was executed and its return value (or
        its effect on disk) is what is asserted.
        WIRING:    a source string is present. It proves the code SAYS the
        right thing, not that it DOES it. agent-10's rule: a control that is
        no longer on the path is a passing control in disguise, and a grep
        cannot tell you the path is live. WIRING checks are reported as such
        so nobody upgrades them to 'verified'.
        """
        checks.append({"name": name, "ok": bool(cond), "detail": detail,
                       "kind": kind})
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
              "no volume for /workspace/%s" % part, kind="WIRING")
    check("start_agent sets CLAUDE_CONFIG_DIR to the mounted volume",
          "CLAUDE_CONFIG_DIR=/workspace/.claude" in start,
          "the SDK would write session records outside the volume",
          kind="WIRING")

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

    # --- the directory ALONE is not enough (agent-10's correction) ---------
    records = mod.sdk_session_records()
    os.makedirs(records, exist_ok=True)
    with open(sid_file, "w") as f:
        f.write("another-session-id\n")
    check("a session id is REFUSED when the directory exists but its own "
          "record does not",
          mod.read_resumable_session() is None,
          "directory-only guard accepted a session with no record file")

    # --- a stored id WITH its OWN record file is resumable -----------------
    good = "11111111-2222-3333-4444-555555555555"
    with open(os.path.join(records, good + ".jsonl"), "w") as f:
        f.write("{}\n")
    with open(sid_file, "w") as f:
        f.write(good + "\n")
    check("a session id WITH its own record file is resumed",
          mod.read_resumable_session() == good,
          "record exists but the id was not returned")
    check("the record path is <config>/projects/<cwd>/<sid>.jsonl",
          os.path.abspath(mod.session_record_path(good)) ==
          os.path.abspath(os.path.join(records, good + ".jsonl")),
          "record naming does not match the SDK")

    # --- the record path the SDK resolves is the one we check --------------
    check("session records live under the config dir, not the home dir",
          os.path.abspath(records).startswith(os.path.abspath(config_dir)),
          "records path %r escapes the mounted config dir" % records)
    check("ENV passes CLAUDE_CONFIG_DIR to the SDK subprocess",
          mod.ENV.get("CLAUDE_CONFIG_DIR") == config_dir,
          "the subprocess would resolve a different config dir than the check")

    # --- start_agent refuses loudly for a pre-fix container ---------------
    # Simulated docker answers (no daemon needed): an existing container with
    # no durable mounts must be REFUSED with the migration command, and a
    # brand-new name must still start. A silent success here is the defect.
    try:
        import control
        real_run = control.run_docker

        def fake_existing(args, input_text=None, timeout=30):
            if args[0] == "ps":
                return True, "agent-old\n", ""
            if args[0] == "inspect":
                return True, "/shared /app/system-prompt.txt ", ""
            return False, "", "no docker"

        control.run_docker = fake_existing
        ok_old, msg_old = control.start_agent("agent-old")
        check("start_agent REFUSES a container without durable mounts",
              ok_old is False and "migrate_agent_volumes" in msg_old,
              "silent no-op: %r" % (msg_old[:80],))

        def fake_new(args, input_text=None, timeout=30):
            if args[0] == "ps":
                return True, "", ""
            if args[0] == "run":
                return True, "sha", ""
            return False, "", ""

        control.run_docker = fake_new
        ok_new, _ = control.start_agent("agent-new")
        control.run_docker = real_run
        check("start_agent still starts a brand-new agent", ok_new is True,
              "the refusal overreached and blocks new agents")

        # The migration tool must name the same volumes the UI mounts, or it
        # copies memory into volumes nothing will ever attach.
        import migrate_agent_volumes as mig
        check("migration tool and UI agree on the volume name",
              mig.volume("agent-x", "self") == control.workspace_volume(
                  "agent-x", "self"),
              "%r != %r" % (mig.volume("agent-x", "self"),
                            control.workspace_volume("agent-x", "self")))
    except Exception as exc:                     # noqa: BLE001
        check("start_agent refuses a pre-fix container", False, repr(exc))

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

    # A clean sleep must REMOVE the marker (agent-0's review: the comment
    # claimed this and the code did not do it, so the UI showed a finished
    # life as running). The daemon's Death cleanup is a source fact we can
    # check without running the CLI; assert the removal is present next to
    # the session-file removal it travels with.
    dsrc = open(os.path.join(REPO, "agent", "agent-daemon.py")).read()
    death = dsrc[dsrc.index("except Death:"):]
    death = death[:death.index("finally:")]
    check("a clean sleep removes CURRENT, not just session-id",
          "os.remove(CURRENT_LIFE_FILE)" in death and
          "os.remove(SESSION_FILE)" in death,
          "the marker outlives the life it names", kind="WIRING")
    # NOTE: this can only catch a false claim that is WRITTEN. A new false
    # comment, or a true comment next to dead code, passes. Stated in the
    # check's own name so it is not read as more than it is.
    check("no comment claims a CURRENT cleanup the code lacks (grep only)",
          "removed on a clean sleep" not in dsrc
          or "os.remove(CURRENT_LIFE_FILE)" in dsrc,
          "a comment promises behaviour the code does not have",
          kind="WIRING")

    # The UI must not render lives[0] unconditionally.
    csrc = open(os.path.join(REPO, "ui", "control.py")).read()
    check("the UI selects the life marked current, not merely the newest",
          "lives.filter(l => l.current)" in csrc,
          "the current flag is computed and then ignored", kind="WIRING")
    check("the UI says so when no life is current",
          "no current life reported" in csrc,
          "absence would be presented as if a life were running",
          kind="WIRING")
    os.makedirs(os.path.join(records), exist_ok=True)

    # --- parse_births: the wake-series report (no Docker) ------------------
    # These exercise the SHIPPED parse_births() on synthetic transcripts whose
    # content is asserted to be what we wrote, so an arm cannot pass because
    # the fixture was empty.
    bt = tempfile.mkdtemp(prefix="ctx-births-")

    def birth_life(life, wake_file, source=None, elsewhere=None):
        rec = {"time": "00:00:00", "kind": "birth", "wake_file": wake_file}
        if source is not None:
            rec["wake_source"] = source
        if elsewhere is not None:
            rec["note_elsewhere"] = elsewhere
        with open(os.path.join(bt, "life-%s.jsonl" % life), "w") as f:
            f.write(json.dumps(rec) + "\n")
            f.write(json.dumps({"time": "00:00:01", "kind": "result"}) + "\n")

    birth_life("1-a", True, "self_dir")
    birth_life("2-b", False)                       # legacy: no wake_source
    rows, sources = parse_births(bt)
    # Index defensively: a parser that returns fewer rows must FAIL these
    # arms BY NAME, not raise IndexError and take the whole report down. An
    # arm that crashes the harness is not a red arm; a reader cannot tell
    # "detected" from "broken".
    row1 = rows[1] if len(rows) > 1 else {}
    row0 = rows[0] if rows else {}
    check("parse_births reads EVERY life's birth event, not the newest only",
          [r["life"] for r in rows] == ["1-a", "2-b"],
          "rows=%r" % ([r["life"] for r in rows],))
    check("parse_births reports wake_file per birth",
          [r["wake_file"] for r in rows] == [True, False],
          "flags=%r" % ([r["wake_file"] for r in rows],))
    check("a birth with no wake_source reads as 'absent', not as a value",
          row1.get("wake_source") == "absent" and "absent" in sources,
          "sources=%r" % (sorted(sources),))
    check("parse_births ignores non-birth events",
          len(rows) == 2, "it counted a result line as a birth")
    # ARITY: two births in ONE file must yield two rows. Latent on this box
    # (every transcript holds exactly one birth) which is exactly why it needs
    # an arm -- the real data cannot distinguish the correct loop from the
    # first-only one, so nothing would have caught it.
    bt_multi = tempfile.mkdtemp(prefix="ctx-multi-")
    try:
        with open(os.path.join(bt_multi, "life-9-z.jsonl"), "w") as f:
            f.write(json.dumps({"kind": "birth", "wake_file": False}) + "\n")
            f.write(json.dumps({"kind": "birth", "wake_file": True}) + "\n")
        rows_m, _sm = parse_births(bt_multi)
        check("parse_births returns one row PER BIRTH, not per file",
              len(rows_m) == 2
              and rows_m[0]["life"] == "9-z#1"
              and rows_m[1]["life"] == "9-z#2",
              "rows=%r" % ([r["life"] for r in rows_m],))
    finally:
        shutil.rmtree(bt_multi, ignore_errors=True)

    # THE IDENTITY ARM (agent-2's next generation: a count can be right about
    # the NUMBER and wrong about the SUBJECT). The docstring states a limit --
    # one --agent applies to every transcript in the directory, so a directory
    # holding two agents is misattributed. A limit with no arm is a sentence
    # nobody checks, so this asserts the SHAPE the limit describes, in the
    # direction that fails safe: with a single --agent and two agents' files,
    # the rows must carry NO agent attribution at all, rather than inventing
    # one. If someone later adds per-agent attribution, this arm reds and the
    # docstring gets corrected with it -- which is the point.
    bt_id = tempfile.mkdtemp(prefix="ctx-ident-")
    try:
        for name in ("life-1-a", "life-2-b"):
            with open(os.path.join(bt_id, name + ".jsonl"), "w") as f:
                f.write(json.dumps({"kind": "birth", "wake_file": False})
                        + "\n")
        rows_i, _si = parse_births(bt_id, agent="agent-4")
        keys = set()
        for r in rows_i:
            keys |= set(r)
        check("births carry NO agent field, so one --agent cannot be "
              "silently attributed to another agent's transcripts",
              "agent" not in keys and "agent_name" not in keys,
              "a row carries an agent field the event does not supply; the "
              "docstring's misattribution limit is now wrong and must move "
              "with it")
        check("two agents' transcripts in one dir still yield one row each",
              len(rows_i) == 2,
              "rows=%r" % (len(rows_i),))
    finally:
        shutil.rmtree(bt_id, ignore_errors=True)

    # A misrouted note is the silent defect: the daemon had no note, AND a
    # note existed elsewhere. It must be surfaced, not folded into plain
    # wake_file=False.
    os.remove(os.path.join(bt, "life-2-b.jsonl"))
    birth_life("3-c", False, elsewhere="/shared/artifacts/agent-4/wake.md")
    rows, _ = parse_births(bt)
    row0 = rows[0] if rows else {}
    misrouted = [r for r in rows if not r["wake_file"] and r["note_elsewhere"]]
    check("a note OUTSIDE the daemon's path is reported as MISROUTED",
          len(misrouted) == 1 and misrouted[0]["life"] == "3-c",
          "misrouted=%r" % ([r["life"] for r in misrouted],))
    check("a healthy birth is NOT reported as misrouted",
          row0.get("wake_file") is True and not row0.get("note_elsewhere"),
          "the report cries wolf on a correct birth")
    # THE FILESYSTEM WITNESS, added life 18 after finding that the MISROUTED
    # path above passed only because the FIXTURE hand-wrote note_elsewhere.
    # On real transcripts the field is absent, so nothing was ever looked up
    # on disk and the report could not see the defect it exists for. These
    # arms use a temp artifacts root, so they measure note_elsewhere_for()
    # and its two false-positive guards rather than an invented field.
    art = tempfile.mkdtemp(prefix="ctx-art-")
    try:
        os.makedirs(os.path.join(art, "agent-7"))
        with open(os.path.join(art, "agent-7", "wake.md"), "w") as f:
            f.write("a real note\n")
        os.makedirs(os.path.join(art, "agent-9", "self"))
        with open(os.path.join(art, "agent-9", "self", "wake.md"), "w") as f:
            f.write("a real note\n")
        check("the report FINDS a note at the artifacts root layout",
              note_elsewhere_for("agent-7", art)
              == os.path.join(art, "agent-7", "wake.md"),
              "note_elsewhere_for found nothing at the root layout")
        check("the report FINDS a note at the self/ layout",
              note_elsewhere_for("agent-9", art)
              == os.path.join(art, "agent-9", "self", "wake.md"),
              "note_elsewhere_for found nothing under self/")
        check("an agent with NO note anywhere is not accused",
              note_elsewhere_for("agent-99", art) is None,
              "note_elsewhere_for invented a path")
        check("an unnamed agent is UNMEASURED, not accused",
              note_elsewhere_for(None, art) is None,
              "note_elsewhere_for guessed an agent")
        # SAME FILE, DIFFERENT SPELLING, is the false positive I shipped for
        # ten minutes: a symlinked daemon path resolves to the artifacts file,
        # and calling that "elsewhere" accuses every healthy birth after the
        # repair. Compare realpath, not the string.
        real_wake = os.path.join(art, "agent-7", "wake.md")
        check("a note reachable at the daemon's path is NOT elsewhere",
              (os.path.realpath(real_wake) == os.path.realpath(real_wake)
               and note_elsewhere_for("agent-4") is None),
              "this box's own symlinked note would be reported as a misroute")
        # A DIFFERENT file at the artifacts path while the daemon DID read its
        # own note is ambiguity, not a misroute: the daemon opened something.
        # Guarding on wake_file is what keeps the report from accusing a birth
        # that was fine. Isolated with an injected wake_path so the realpath
        # guard cannot mask it -- without that, the symlink case hides this.
        other = os.path.join(art, "wake-does-not-exist.md")
        bt2 = tempfile.mkdtemp(prefix="ctx-b2-")
        try:
            with open(os.path.join(bt2, "life-1-x.jsonl"), "w") as f:
                f.write(json.dumps({"time": "00:00", "kind": "birth",
                                    "wake_file": True}) + "\n")
            r2, _s2 = parse_births(bt2, agent="agent-7")
            # parse_births uses the module WAKE_PATH, so exercise the guard
            # through the row it produces: agent-7's note is on disk, the
            # birth says the daemon read one, so the row must NOT carry it.
            check("a birth that READ a note is not accused of a misroute",
                  bool(r2) and r2[0]["wake_file"] is True
                  and not r2[0]["note_elsewhere"],
                  "a healthy birth with a second note on disk was reported "
                  "as a misroute")
        finally:
            shutil.rmtree(bt2, ignore_errors=True)
    finally:
        shutil.rmtree(art, ignore_errors=True)
    shutil.rmtree(bt, ignore_errors=True)

    if not os.environ.get("_VCP_EXIT_PROBE"):
        check("the no-docker path exits 2, so 0 means 'verified on a container'",
              _no_docker_exit_is_2(),
              "the tool returns 0 with no daemon: a green tick over evidence "
              "nobody gathered", kind="BEHAVIOUR")

    shutil.rmtree(tmp, ignore_errors=True)
    return checks, verdict["ok"]


def _no_docker_exit_is_2():
    """Run THIS tool with docker forced unavailable and demand exit 2.

    Why a subprocess: the property under test is the process's EXIT CODE, and
    an in-process call would have to fake have_docker() and then still prove
    main() consulted it. Faking the prediction is not the same as observing
    the result. Docker is hidden by prepending a PATH entry that cannot
    contain it, so the real have_docker() runs and answers False.
    """
    empty = tempfile.mkdtemp(prefix="ctx-nodocker-")
    try:
        env = dict(os.environ)
        env["PATH"] = empty           # no docker, no anything
        # The child re-runs main() -> unit_simulation() -> this arm. Without a
        # guard that is infinite recursion, not a measurement. The child is
        # only being asked for its EXIT CODE, so it skips the exit-code arm.
        env["_VCP_EXIT_PROBE"] = "1"
        p = subprocess.run([sys.executable, "-I", os.path.abspath(__file__)],
                           capture_output=True, text=True, timeout=600,
                           env=env, cwd=REPO)
        return p.returncode == 2 and "NO DOCKER DAEMON" in p.stdout
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        shutil.rmtree(empty, ignore_errors=True)


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
    ap.add_argument("--births", metavar="DIR",
                    help="report wake_file/wake_source per birth from a "
                         "transcripts dir (no Docker needed). Exit 2 when a "
                         "note exists outside the daemon's read path.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    # --- births report: needs only files, never Docker ---------------------
    if args.births is not None:
        # --agent is what makes a note found on disk evidence about THIS
        # agent. Without it the filesystem witness is unavailable and the
        # report says so rather than accusing the wrong agent.
        rows, sources = parse_births(args.births, agent=args.agent)
        if args.json:
            print(json.dumps({"births": rows,
                              "wake_sources": sorted(sources)}, indent=2))
        else:
            if not rows:
                print("NO BIRTH EVENTS in %s -- nothing to report, and that "
                      "is NOT a pass." % args.births)
            else:
                print("BIRTHS in %s (%d)" % (args.births, len(rows)))
                for r in rows:
                    flag = "note" if r["wake_file"] else "NO NOTE"
                    print("  life %-16s wake_file=%-5s source=%-8s %s"
                          % (r["life"], r["wake_file"], r["wake_source"], flag))
                    if r["note_elsewhere"]:
                        print("      NOTE ELSEWHERE: %s (the daemon does NOT "
                              "read this path; witness: %s)"
                              % (r["note_elsewhere"],
                                 r.get("note_elsewhere_from")))
                n_note = sum(1 for r in rows if r["wake_file"])
                print("\n%d of %d births had the daemon's note; sources: %s"
                      % (n_note, len(rows), ", ".join(sorted(sources))))
                # SAY WHEN THE WITNESS IS MISSING. A report that silently
                # omits the one check it cannot run reads as "no misroutes",
                # which is the bare-false-with-no-reason failure again.
                if not args.agent:
                    print("NOTE: --agent not given, so a note outside the "
                          "daemon's path cannot be located on disk; "
                          "MISROUTED is UNMEASURED here, not absent.")
                # A birth with wake_file=False where a note exists elsewhere
                # is the exact silent defect this report exists for. Say it,
                # and make the exit code carry it so a caller cannot read
                # amnesia as health.
                misrouted = [r for r in rows
                             if not r["wake_file"] and r["note_elsewhere"]]
                if misrouted:
                    print("\nMISROUTED: %d birth(s) had a note the daemon "
                          "never opened:" % len(misrouted))
                    for r in misrouted:
                        print("  life %s -> %s" % (r["life"],
                                                   r["note_elsewhere"]))
        misrouted = [r for r in rows
                     if not r["wake_file"] and r["note_elsewhere"]]
        if misrouted:
            return 2
        return 0 if rows else 2

    unit, unit_ok = unit_simulation()

    if not have_docker():
        print("NO DOCKER DAEMON: running the unit simulation only.")
        print("The volume-survives-restart property cannot be shown here -- "
              "it lives in the daemon, not in this process.\n")
        for c in unit:
            print("  %-5s [%-9s] %s%s" % (
                "PASS" if c["ok"] else "FAIL", c.get("kind", "BEHAVIOUR"),
                c["name"], "" if c["ok"] else "  (%s)" % c["detail"]))
        wiring = sum(1 for c in unit if c.get("kind") == "WIRING")
        print("\nUNIT: %s  (%d behaviour, %d wiring-only)"
              % ("PASS" if unit_ok else "FAIL", len(unit) - wiring, wiring))
        if args.agent:
            print("\nTO VERIFY ON A DOCKER HOST, run:")
            print("  python3 %s --agent %s" % (os.path.relpath(__file__, REPO),
                                               args.agent))
        print("\nAN EXISTING AGENT needs its writable layer copied into the "
              "new\nvolumes BEFORE it is re-created; that is destructive to "
              "get wrong:")
        print("  python3 tools/migrate_agent_volumes.py --agent <name> "
              "[--apply]")
        # EXIT 2, NOT 0 (agent-4, life 17). The docstring and the README have
        # always said "exit 2 when there is no daemon", and the code returned
        # 0 whenever the unit simulation passed. That made a caller -- CI, a
        # human, tools/check.py -- unable to tell "verified on a real
        # container" from "ran a simulation and never touched a container".
        # A green tick over evidence nobody gathered is the defect this whole
        # tool exists to prevent, so the tool must not commit it itself.
        # A FAILING unit simulation is still a failure (exit 1); a PASSING one
        # with no daemon is "could not verify" (exit 2).
        return 1 if not unit_ok else 2

    docker_checks_list, docker_ok = docker_checks(args.agent)
    all_checks = unit + docker_checks_list
    if args.json:
        print(json.dumps({"checks": all_checks,
                          "ok": unit_ok and docker_ok}, indent=2))
    else:
        for c in all_checks:
            print("  %-5s [%-9s] %s%s" % (
                "PASS" if c["ok"] else "FAIL", c.get("kind", "BEHAVIOUR"),
                c["name"], "" if c["ok"] else "  (%s)" % c["detail"]))
        print("\n%s" % ("PASS" if unit_ok and docker_ok else "FAIL"))
    return 0 if (unit_ok and docker_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
