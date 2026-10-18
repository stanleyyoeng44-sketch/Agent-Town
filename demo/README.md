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

**The negative control tests an invariant, not a value.** The demo attempts
overdrafts of `balance+1`, 10,000 and 1,000,000 — every one must be refused —
and then asserts the aggregate properties: no balance is negative, and a
refused transfer creates no coins. An earlier version tested only 10,000, and
agent-2 defeated it with a bank that refuses overdrafts *only* at 10,000.

**Four mutation tests**, in `selftest_demo.py`, all of which must hold:

| mutation | expected | what it proves |
|---|---|---|
| overdraft guard disabled | demo fails (rc=1) | the control fires |
| single-value guard (`amount == 10000`) | demo fails (rc=1) | the control tests the invariant |
| bank fixed to read `BANK_DATA` | demo passes (rc=0) | a good bank change does not break the demo |
| ledger path shape unknown | demo exits 2 | it refuses rather than measuring the wrong ledger |

A demo that has never been seen to fail is not evidence.
