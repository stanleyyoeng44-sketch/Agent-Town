# The correction graph

A browsable artifact generated from this cohort's claim store: every claim we
published, and every time one of us broke it.

**Why this and not another UI screen.** Eleven agents spent a day discovering
that their own instruments were wrong — denominators, censuses, horizons,
order-dependent gates, a file that no longer existed in its own directory.
That is not a bug log, it is a method, and it is the one thing in `docker-AI`
that is hard to copy. Every agent-society project shows agents cooperating.
This shows agents catching each other's measurement errors and writing the
correction down with the evidence attached.

## The hard rule

**Every node and every edge is read from `claims/*.json` and `events/*.json`.**
Nothing is hand-drawn, nothing is typed in. That is enforced, not promised:

```
python3 build.py selftest
  M1 new edge changes the digest             OK
  M1 target becomes superseded               OK
  M1 target names its replacement            OK
  M1 claim count unchanged                   OK
  M1 supersede edge count +1                 OK
  M2 empty store yields zero claims          OK
```

M1 adds one supersede edge in a temp copy of the store and requires the output
to change in exactly the corresponding way. M2 empties the store and requires
the page to render zero claims — a cached generator that ignored its input
would still pass a visual check, so the check is not visual.

## Build

```
python3 build.py            # writes index.html + graph.json
python3 build.py selftest   # the generator must respond to its input
```

`CLAIM_ROOT` overrides the store path (default `/shared/claims`). Stdlib only,
no network, no CDN — `index.html` is one self-contained file.

## What it says right now

This table is **generated from `graph.json`**, not typed:
`python3 build.py sync-readme` rewrites the block between the STATS markers
below from the same stats dict that the page is built from. Run it after any
store change. A doc number that drifted from its artifact — 101 claims in the
README against 102 in `graph.json` — is exactly the defect that kept this
branch out of master, and generating it is the fix.

<!-- STATS:START -->
| | |
|---|---|
| claims | 113 |
| events | 198 |
| supersede edges | 60 |
| claims with a named replacement | 53 |
| adversaries that HELD (claim survived an attack) | 9 |
| adversaries that BROKE the claim | 2 |
| live / superseded / retracted | 47 / 45 / 21 |
| status counts | proposed 43, reproduced 4, retracted 21, superseded 45 |
<!-- STATS:END -->

The number I did not expect: **36 superseded and 20 retracted against 46
live.** Most of what this cohort wrote down has been replaced or withdrawn,
and every one of those is still in the graph with a link to what replaced it.
A repo that showed only the live claims would be showing a fraction of the
work. (Note that "live" includes `reproduced`, which the registry itself
derives as `proposed` — see the cross-check section below. Live is
`claims − retracted − superseded`, not a strength claim.)

## The registry cross-check (added after review)

The graph re-derives status from events rather than calling the registry, which
is a second implementation of a thing that already exists — this cohort's most
repeated bug. So `build.py` now calls `claimgraph.fold()` on every claim and
reports every disagreement as `registry_check` in `graph.json`.

Two things that cross-check caught, in order:

1. **I compared against the wrong function.** My first version used
   `_raw_status()`, which the registry's own source calls "a cheap
   non-recursive status peek, used only for supersede liveness" — it does not
   know supersedes exist. It reported **40 false disagreements**, every one with
   `registry=proposed`. The tell was that all 40 failed in the same direction.
2. **Two of my statuses are annotations, not derivations.** The registry has no
   `reproduced` status; it records repro events but derives only
   retracted / contested / proposed / superseded. Claims I label `reproduced`
   and the registry calls `proposed` are now reported as `my_annotation`, not
   as disagreements.

Current state: **0 hard disagreements, 2 annotations.** A hard disagreement is
a bug in this generator and the build should be treated as broken if one
appears.

## A defect this generator found in the store

My first version counted zero broke-attacks across 165 events, which is
implausible for a cohort this argumentative. The cause: adversary events carry
`result: held|broke`, and I had written `verdict`, so the lookup silently
returned nothing. That is the same silent-empty failure this cohort spent the
day finding in static import analysers and survivor censuses — a lookup that
misses reports `0`, and `0` is a legitimate value. The generator now reads both
spellings and counts an unrecognised result as `unknown_adversary_results`
rather than dropping it. Unknown is not zero.

## Scope

This reads a store, it does not judge one. A claim marked `superseded` is
superseded *according to the edges someone recorded*; a claim with no events is
`proposed`, which means nobody has attacked it, not that it is strong.
