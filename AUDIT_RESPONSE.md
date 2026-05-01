# OWL — Response to the External Audit

**Date:** 2026-05-01
**Inputs:** `AUDIT_BRIEF.md` (brief sent to the auditor) + the audit reply received from ChatGPT (summarised below).
**Purpose:** capture the implementation agent's take on the audit before any refactor work begins, so the decision path isn't lost between sessions.

## What the audit recommended (one-line summary)

Before CR-001, do exactly one structural pass: introduce **`audience.py`** + **`capabilities.py`** + **`queue_control.py`** as a small access spine. Tag every route. Route every `enqueue` operation through one function. **No CR-001 features yet** — just the structural carve-out. Then CR-001 lands on top.

After CR-001, the next most valuable cleanup is moving JS to static files and extracting result-card rendering helpers.

## Implementation agent's reaction

The audit converged on what the brief flagged — reassuring, means the brief was honest rather than selectively self-deprecating — and added one sharp insight beyond it.

### Strongest call from the audit

> *The issue isn't `main.py`'s size, it's the coupling.* Routing + auth + queueing + HTML + JS + capability policy all share one edit surface, and CR-001 multiplies the number of places that need coordinated edits.

Splitting `main.py` mechanically wouldn't help. Introducing an *access spine* of 3 small modules consolidates the policy in one place. That's the right answer.

### Sharpest insight beyond the brief

> *"The documentation is compensating for missing code boundaries."*

A smell the brief hadn't named. As soon as `capabilities.py` exists, it should **be** the truth — `CAPABILITIES.md` shouldn't have to mirror it. Same for `audience.py`. Hold this line when CR-001 lands.

## Three places to push back or refine

### 1. The audit's 1–2 day estimate is optimistic

ChatGPT hasn't seen how scattered the existing capability checks are: `_is_local()` in some places, ad-hoc "if not LAN, render read-only" patterns in others, cookie handling sprinkled through middleware and routes. Tagging every existing route + verifying by hand is more like **2–4 days**, mostly route-inventory + manual smoke testing without a test suite to catch regressions.

### 2. Demo lock (CR-001b) bundling

The audit folds CR-001b into CR-001 work. The change-request doc argues it can ship **earlier** using a `settings.json` `demo_lock_owner` stopgap — since today's shared-password gate already establishes an implicit owner. Useful if a high-stakes demo lands before the full access spine. Worth keeping that option open rather than gating the demo lock behind the larger refactor.

### 3. Capability declarations should be typed enums, not strings

The audit suggests `@requires("custom_upload")`. Prefer `@requires(Capability.CUSTOM_UPLOAD)` — a typo on a string silently fails open; a typo on an enum is a compile-time error. Cheap correctness win at zero ergonomic cost.

## Two things the audit missed worth adding

### Tests should land with the access spine, not later

`audience.py`, `capabilities.py`, and (already-shipped) `carbon.py` have zero external dependencies — they're easy unit-test targets, and they're exactly where regressions hurt most. Adding ~30 lines of pytest at this scope is cheap and high-value. Putting tests off until "later" means they never happen.

### Refactor in a branch with a manual smoke checklist

No automated tests means we need to walk every major path before merging:
- Video: single (CPU and GPU presets), both, all-codecs
- LLM: single, both, all, all-both, batch
- Image: single, both, compare-models
- RAG: single, compare-3-modes
- Settings: load, save, variance calibration
- Queue: enqueue, pause, status

~1 hour of manual smoke testing, worth doing once before merging the spine refactor.

## Recommended next action

When ready to move forward:

1. **Open a branch** (e.g. `access-spine`).
2. **Build the spine** — 3 modules, no behaviour change, just structural carve-out:
   - `wattlab_service/audience.py` — `tier(request)` helper, returns one of `Anonymous | Member | Lab`
   - `wattlab_service/capabilities.py` — typed `Capability` enum + per-tier capability matrix
   - `wattlab_service/queue_control.py` — thin façade over `enqueue()` enforcing capability + lock + per-tier caps
3. **Tag every route** with `@requires(Capability.X)` (or equivalent).
4. **Add unit tests** for the three modules (~30 lines pytest).
5. **Manual smoke checklist** before merging.
6. **Merge spine.**
7. **Then start CR-001** (auth, public landing page, member features). Implementation now becomes much smaller and lower-risk.

Effort estimate end-to-end (spine only, before CR-001 features): **2–4 days**.

## What explicitly NOT to do before CR-001

(Per the audit, these would expand scope and delay the conference launch):
- Don't split `main.py` mechanically by line count.
- Don't rewrite persistence or invent full Pydantic schemas yet.
- Don't redesign the measurement modules.
- Don't introduce a database (flat files still adequate at this scale).
- Don't over-document every route — self-documenting capability declarations + a short `ARCHITECTURE.md` (1–2 pages) is enough.

## Doc structure target (per audit)

```
ARCHITECTURE.md      1–2 pages: modules, request flow, queue flow, result flow
CAPABILITIES.md      tier matrix kept aligned with capabilities.py (or generated from it)
CLAUDE.md            operational context only
CHANGE_REQUESTS.md   product/change intent only
```

Keep docs short. Put truth in code where possible.

## Status

- Audit received and reviewed: 2026-05-01.
- Spine refactor: **not started.** Awaiting decision to begin.
- CR-001 (two-tier OWL): blocked on spine refactor.
- CR-001b (demo lock): can ship before the spine if needed, using `demo_lock_owner` stopgap.
- Conference (first public showing of OWL): mid-June 2026 — currently ~6 weeks out.
