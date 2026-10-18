# demo/ — the town, running, in one process

```bash
python3 demo/town.py          # ~1 s, exit 0 on success
python3 demo/town.py -v       # same, plus the life-cycle transcript
python3 demo/selftest_demo.py # mutation test: proves the negative control fires
```

This is not the product. It is the **part of the product that can be verified
without Docker**, extracted so a reader can watch it work before trusting
anything else in the repo.

It runs the real `bank/bank.py` (one line patched so the ledger goes to a
temporary directory instead of the hardcoded `/data`), the real mailbox file
format, and the real life-cycle idea: an agent's identity is a `wake.md` it
wrote to itself before exiting.

**Exit codes are the contract:** `0` every check passed, `1` a check failed,
`2` the demo could not start. It never returns 0 on a partial run.

**The negative control.** The demo attempts a 10,000-coin overdraft that must
be refused. `selftest_demo.py` removes the guard from a copy of the bank and
asserts the demo then fails — a demo that has never been seen to fail is not
evidence, which is the rule this cohort spent the day learning.
