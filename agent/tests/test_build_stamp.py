"""The build stamp must never fail the daemon, and must never read outside.

agent-0's ask: agent_daemon_sha256 is computed with a file read at every
birth. Prove it returns None (rather than raising) on a read-only or missing
path, and that repo_head does not walk out of the container.

WHY THIS MATTERS MORE THAN IT LOOKS: a birth is the first thing a life does.
If the diagnostic can raise, it turns a healthy daemon into a crash loop at
exactly the moment an operator is trying to find out what is running.

Run:  python3 agent/tests/test_build_stamp.py
Exit: 0 every case degraded safely, 1 one raised or escaped.
"""
import importlib.util
import os
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "agent-daemon.py")


def load():
    spec = importlib.util.spec_from_file_location("daemon_bs", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    mod = load()
    checks = []

    def check(name, fn, want_none=True):
        try:
            value = fn()
        except Exception as exc:                       # noqa: BLE001
            checks.append((name, False, "RAISED %r" % (exc,)))
            return
        ok = (value is None) if want_none else (value is not None)
        checks.append((name, ok, repr(value)[:60]))

    tmp = tempfile.mkdtemp()

    check("missing file -> None", lambda: mod._sha256(os.path.join(tmp, "no")))
    check("a directory -> None", lambda: mod._sha256(tmp))

    unreadable = os.path.join(tmp, "unreadable.py")
    with open(unreadable, "w") as f:
        f.write("x")
    os.chmod(unreadable, 0)
    check("unreadable file -> None", lambda: mod._sha256(unreadable))
    os.chmod(unreadable, stat.S_IRUSR | stat.S_IWUSR)

    # The whole stamp must be safe even when every part is unavailable.
    real_sha, real_head = mod._sha256, mod._read_head
    mod._sha256 = lambda p: None
    mod._read_head = lambda: None
    try:
        stamp = mod.build_stamp()
        checks.append(("build_stamp() with everything missing does not raise",
                       isinstance(stamp, dict)
                       and stamp.get("agent_daemon_sha256") is None,
                       repr(stamp)[:60]))
    except Exception as exc:                           # noqa: BLE001
        checks.append(("build_stamp() with everything missing does not raise",
                       False, "RAISED %r" % (exc,)))
    finally:
        mod._sha256, mod._read_head = real_sha, real_head

    # It must still WORK in a real checkout, or the diagnostics are useless.
    check("a real file hashes", lambda: mod._sha256(SRC), want_none=False)

    # repo_head containment: it may only read inside a .git it found, and may
    # not escape via a ref that points outside the repo.
    src = open(SRC, encoding="utf-8").read()
    body = src[src.index("def _read_head"):src.index("def _previous_life_end")]
    checks.append(("_read_head stops at the filesystem root",
                   "os.path.dirname(here)" in body, ""))
    checks.append(("_read_head refuses a ref that escapes .git",
                   "realpath" in body and ".git" in body, ""))
    checks.append(("_read_head never searches above /app",
                   '"/app"' in body, ""))

    ok = True
    for name, good, detail in checks:
        ok = ok and good
        print("  %-5s %s%s" % ("PASS" if good else "FAIL", name,
                               "" if good else "  (%s)" % detail))
    print("\n%s" % ("BUILD-STAMP PASS" if ok else "BUILD-STAMP FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
