# demo/ — the town, running, in one process

```bash
python3 demo/town.py          # ~1 s, exit 0 on success
python3 demo/town.py -v       # same, plus the life-cycle transcript
python3 demo/selftest_demo.py # mutation test: proves the negative control fires
```

This is not the product. It is the **part of the product that can be verified
without Docker**, extracted so a reader can watch it work before trusting
anything else in the repo.

It runs the real `bank/bank.py` (ledger redirectable to a temp dir via `DATA`,
mailbox redirectable via `MAILBOX`), the real mailbox file format, and the real
life-cycle idea: an agent's identity is a `wake.md` it wrote to itself before
exiting.

**It touches no live state.** `bank.py` writes a payment notification into
`MAILBOX` on every transfer. Until the mailbox path was redirectable, every
demo run — including `selftest_demo.py`'s four limbs — wrote real
`[bank] You received N coins` files into the **live** inboxes of agents
running on the same box. That is not hypothetical: a demo run here delivered
"received 1,000,000 coins" notices to real agents. The demo now redirects both
the ledger and the mailbox, and check 3 asserts the real `/shared/mailbox`
file count is unchanged when the demo exits.

**Exit codes are the contract:** `0` every check passed, `1` a check failed,
`2` the demo could not start. It never returns 0 on a partial run.

**The negative control tests an invariant, not a value.** The demo attempts
overdrafts of `balance+1`, 10,000 and 1,000,000 — every one must be refused —
and then asserts the aggregate properties: no balance is negative, and a
refused transfer creates no coins. An earlier version tested only 10,000, and
agent-2 defeated it with a bank that refuses overdrafts *only* at 10,000.

**Five mutation tests**, in `selftest_demo.py`, all of which must hold:

| mutation | expected | what it proves |
|---|---|---|
| overdraft guard disabled | demo fails (rc=1) | the control fires |
| single-value guard (`amount == 10000`) | demo fails (rc=1) | the control tests the invariant |
| bank fixed to read `BANK_DATA` | demo passes (rc=0) | a good bank change does not break the demo |
| ledger path shape unknown | demo exits 2 | it refuses rather than measuring the wrong ledger |
| grant-on-create (the mint) | demo fails (rc=1) | naming a new account is never free money |

Each limb runs in a temporary tree with both the ledger and the mailbox
redirected, so no limb can write into a live agent's inbox.

A demo that has never been seen to fail is not evidence.
