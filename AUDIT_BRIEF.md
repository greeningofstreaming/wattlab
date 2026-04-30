# OWL — Architecture Audit Brief

**Prepared by:** the implementation agent that built recent sessions — flagging its own blind spots for a fresh reviewer.
**Date:** 2026-05-01
**Audience:** another AI agent (or human reviewer) doing an architecture audit before **CR-001 (two-tier OWL)** lands.
**Time budget for the audit:** ~1–2 hours of focused review. Not a code re-write; a *risks-and-recommendations* pass.

## Why now

We've shipped ~20 sessions of small/mid-sized increments without an architectural review. Session 8's external audit flagged `main.py` size; the concern was deferred and the file has grown several hundred lines per session since. CR-001 (auth, three capability tiers, optional sign-in, public/member route gating) is a structural change. **Refactor before, not after** — refactoring under a structural change is much more expensive.

## Scope

**In scope:**
- Architecture, modularity, layering, factorisation
- Risk-shaped issues: what will hurt when CR-001 lands? What will hurt when a new test mode is added?
- The cost/value of fixing each smell *before* CR-001

**Out of scope (don't spend time on):**
- Measurement methodology (P110, ffmpeg flags, baseline policy) — different audit
- Visual design / CSS / copy
- Performance (single-user lab service; not a concern)
- Bug hunts — this is structure, not correctness
- Tests — there aren't any worth reviewing; the absence *is* the finding

## Where to start (read in this order, ~30 min)

1. `CLAUDE.md` — project overview, sessions log, key findings
2. `CHANGE_REQUESTS.md` — CR-001 (two-tier) + CR-001b (demo lock); the change about to land
3. `wattlab_service/carbon.py` (~270 LOC, freshest module) — for what "good factorisation" looks like in this codebase
4. `wattlab_service/persist.py` (~370 LOC) — to see how persistence couples to result shapes
5. **Skim** (don't read line-by-line) `wattlab_service/main.py` (~5,800 LOC) — look at section markers, function boundaries, route handlers. Note: significant content is HTML/JS embedded in Python f-strings.

## Hot zones (likely smells — verify or refute)

| # | File / pattern | Smell | Why it matters for CR-001 |
|---|---|---|---|
| 1 | `main.py` size | Routes + HTML + JS + orchestration in one file. ~5,800 LOC. | Adding capability tags + per-tier UI multiplies edit surface. |
| 2 | JS embedded in Python triple-strings (`_LIVE_JS`, `_CARBON_JS`, `_PROGRESS_JS`, ~400 lines total) | No linting, no syntax highlighting, no tests. The `0 g` rounding bug we just fixed was exactly this kind of issue hiding in plain sight. | The public/member UI split will need much more JS. |
| 3 | Carbon row insertions fanned out to ~10 render templates | The helper is one place; **call sites aren't.** New result modes silently miss carbon rows. Same pattern likely repeats for confidence flags, scope notes. | Same fanout pattern will bite when adding "Members only · Join GoS" CTAs. |
| 4 | `walk_and_enrich(obj)` (carbon.py) recurses looking for any dict with key `energy` | No schema; if a future shape introduces a `"energy": "label"` string, behaviour is undefined. | Result shapes are growing, not shrinking. |
| 5 | `persist._summarise()` and `_*_rows()` have special-case branches per mode (single, both, all_codecs, batch, all, all_both, rag, rag_compare, compare_models) | Adding a new mode means edits across **5+ touchpoints**: row builder, summariser, CSV fieldnames, render template, previous-runs renderer. | Each new public-tier feature risks adding a mode. |
| 6 | `jobs` dict mutated from many sites (`main.py:1289, 1421, 1439, 2166, 2247, 2894, …`) | No invariants. `status`/`stage` are overloaded free-form strings. Easy to leave a job in an inconsistent state. | Demo lock (CR-001b) and per-user identity (CR-001) will both need to read/write this state. |
| 7 | `settings.py` `DEFAULTS` is a flat dict of ~25 keys, no validation, no type | Calibration *outputs* (`variance_*_pct`) live next to user-editable params. No clear ownership. | Adding `demo_lock_minutes`, `rate_anonymous_per_5min`, etc. continues this pattern. |
| 8 | Display formatting (Wh, mass, power, °C, %) is scattered: `metricRow`, `fmtG`, `fmtMass`, plus inline `${e.delta_e_wh} Wh` strings | No display layer; data and presentation entangled. | Public/member views may want different formatting (e.g. unit choice, decimal precision). |
| 9 | Result data shapes are implicit | Knowledge of "what's in a video result vs. a video-both result vs. all-codecs" exists only in render templates and persist code. No typed schema. | Hard to reason about additions; hard to test. |
| 10 | No tests | TESTING.md describes manual tier-1/2/3 procedures, not automated tests. Carbon module has zero coverage; recent regression went undetected in code review (caught in UI). | Refactoring without tests is high-risk. |

## Specific questions for the auditor to answer

1. **Is `main.py`'s size the real problem, or is splitting it the real problem?** (Sometimes a big file is fine if its sections are well-bounded. We need a verdict.)
2. **What is the *smallest viable* refactor that makes CR-001 tractable?** Not "the ideal architecture" — the minimum that unblocks the next change. Effort estimate.
3. **Should JS move out of Python f-strings into static files** served from `/static/`? (Cost: more files, slight latency. Benefit: linting, syntax highlighting, possibility of testing.)
4. **Is `walk_and_enrich`'s schema fragility worth fixing**, or is it acceptable at this scale? (i.e. add typed schema vs. live with it.)
5. **What is the ONE refactor with the highest leverage** — biggest unlock for future changes, lowest risk? (We want a single recommendation we can act on, not a list of ten.)
6. **What should we explicitly *not* refactor before CR-001**, even though it might be tempting?

## Known recent changes (context for the auditor)

In the most recent session (2026-04-30 → 2026-05-01) the following landed:
- New `carbon.py` module — Wh → gCO₂e with live grid intensity (Eco2mix → ElectricityMaps → Ember static fallback ladder)
- `persist.py` calls `carbon.walk_and_enrich(payload)` at save time
- `main.py` `_CARBON_JS` (~150 lines of JS) injected via `_FOOTER`
- Carbon row + comparison strip inserted into ~10 result-card templates
- One round-trip regression already (rounding to 3 decimals truncated µg-scale values to 0 — caught in UI, not in tests)
- `CHANGE_REQUESTS.md` created, CR-001 + CR-001b drafted

This is fresh code with no test coverage; treat it as a representative sample of the codebase's current quality bar.

## What good output looks like

A short report (~1 page) covering:
- **Top 3 risks**, in order of impact-on-CR-001
- **One recommended refactor** to do *before* CR-001, with rough effort estimate
- **Things explicitly safe to leave alone** for now (so we don't over-refactor)
- **Anything I missed** — the implementation agent has selection / continuity / completion bias, and almost certainly underweighted some risk it's responsible for

Don't write any code. Recommendations only.
