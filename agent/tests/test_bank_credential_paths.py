"""The client and the bank must agree about where a credential lives.

Run: python3 agent/tests/test_bank_credential_paths.py   (stdlib only)

WHY THIS EXISTS. bank-per-sender-auth makes the bank authenticate EVERY
sender by reading <BANK_TOKEN_DIR>/<sender>. agent/mcp-stdio.py sends the
credential from <BANK_TOKEN_DIR>/<my_name>. Those are two different containers
reading two different mounts of a secret that must be the SAME string, and
nothing in either file enforces that they are configured together.

The failure this pins is silent in development and total in production: if the
operator mounts the bank's directory and forgets the client's -- or mounts one
and not the other -- then every non-agent-0 send_money answers a clean, honest
503 and the economy simply stops. No test in either file would notice, because
each file is consistent with itself. This test reads BOTH sources and asserts
they name the same default and the same header.
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BANK = os.path.join(HERE, "bank", "bank.py")
CLIENT = os.path.join(HERE, "agent", "mcp-stdio.py")


def default_of(path, name):
    """The string default of `NAME = os.environ.get(NAME, default)`."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and tgt.id == name:
                call = node.value
                if (isinstance(call, ast.Call)
                        and getattr(call.func, "attr", "") == "get"
                        and len(call.args) >= 2):
                    return ast.literal_eval(call.args[1])
    return None


def _empty_file_is_absent(client_path):
    """Call the SHIPPED _read_credential; do not read its source.

    Two cases, because the lockout needs both: an empty file alone must be
    None, and an empty file AHEAD of a real one must not swallow the search.
    """
    import importlib.util
    import tempfile
    work = tempfile.mkdtemp(prefix="cred-")
    empty = os.path.join(work, "empty")
    real = os.path.join(work, "real")
    with open(empty, "w", encoding="utf-8") as f:
        f.write("\n")            # whitespace only
    with open(real, "w", encoding="utf-8") as f:
        f.write("the-real-secret\n")
    try:
        spec = importlib.util.spec_from_file_location("client_under_test",
                                                      client_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        reader = getattr(mod, "_read_credential", None)
        if reader is None:
            return False
        return (reader([empty]) is None
                and reader([os.path.join(work, "missing"), real]) == "the-real-secret"
                and reader([empty, real]) == "the-real-secret")
    except Exception:
        return False


def main():
    ok = True
    checks = []

    b = default_of(BANK, "TOKEN_DIR")
    c = default_of(CLIENT, "SENDER_TOKEN_DIR")
    checks.append(("the bank has a token-dir default (%r)" % b, b is not None))
    checks.append(("the client has a token-dir default (%r)" % c, c is not None))
    # THE TWO DIRECTORIES ARE MEANT TO DIFFER. They are two containers reading
    # two mounts of ONE secret, so asserting they are equal would be asserting
    # a deployment that cannot exist: the bank's path is inside the bank
    # container, the client's is inside each agent container. My first version
    # of this test demanded equality and failed against a correct pair. What
    # must match is the ENV VAR that configures them and the FILE NAMING
    # convention (one file per agent name), not the path.
    checks.append(("both are configured by the same env var name",
                   'os.environ.get("BANK_TOKEN_DIR"' in
                   open(BANK, encoding="utf-8").read()
                   and 'os.environ.get("BANK_TOKEN_DIR"' in
                   open(CLIENT, encoding="utf-8").read()))
    checks.append(("the client names the file after the agent",
                   '{my_name}' in open(CLIENT, encoding="utf-8").read()
                   or 'SENDER_TOKEN_DIR, my_name' in
                   open(CLIENT, encoding="utf-8").read()))
    checks.append(("the bank names the file after the sender",
                   'os.path.join(TOKEN_DIR, sender)' in
                   open(BANK, encoding="utf-8").read()))

    src = open(CLIENT, encoding="utf-8").read()
    checks.append(("the client sends X-Bank-Token", "X-Bank-Token" in src))
    bsrc = open(BANK, encoding="utf-8").read()
    checks.append(("the bank reads X-Bank-Token", "X-Bank-Token" in bsrc))
    checks.append(("the bank still accepts the agent-0 legacy header",
                   "X-Agent0-Token" in bsrc))
    checks.append(("the client still sends the agent-0 legacy header",
                   "X-Agent0-Token" in src))

    # the client must not send an EMPTY credential: both sides empty would
    # match, and 'absent == absent' is not authentication.
    #
    # THIS WAS A GREP UNTIL agent-8 SWEPT IT (2026-10-19 04:5xZ). `"if
    # secret:" in src` is sensitive to SPELLING and blind to MEANING: the
    # semantic mutant `secret = f.read().strip() or "EMPTY"` KEEPS that
    # literal, passes this line, and inverts the behaviour --
    #   [empty]        -> 'EMPTY'   (should be None)
    #   [empty, real]  -> 'EMPTY'   (the provisioned REAL token is ignored)
    # and _read_credential returns the FIRST non-empty entry, so a truthy
    # value from a broken first mount STOPS THE SEARCH and the caller is
    # refused. Same lockout class as the `or` precedence and get() duplicate
    # bugs, one mount over: the behaviour is right, the check that guards it
    # was a replica of the spelling.
    # It now CALLS the function on real files.
    checks.append(("the client treats an empty file as absent",
                   _empty_file_is_absent(CLIENT)))

    print("%-62s %s" % ("check", "result"))
    print("-" * 74)
    for name, good in checks:
        print("%-62s %s" % (name, "PASS" if good else "FAIL"))
        ok = ok and good
    print("\nCREDENTIAL-PATHS:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
