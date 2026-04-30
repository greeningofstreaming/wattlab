# OWL Change Requests

Design / change requests captured for later implementation. Each entry has a status, a problem statement, the agreed direction, and any open questions. Implementation lives in JOURNAL.md once it lands.

---

## CR-001 · Two-tier OWL: anonymous public + authenticated members

**Status:** captured 2026-05-01 — awaiting implementation slot.
**Triggered by:** demo today (2026-05-01) — discussion of opening OWL to the wider streaming community at the sustainable-streaming conference (mid-June 2026, OWL's first public showing).

### Problem

OWL is currently single-tier: one shared password (`WATTLAB_GATE_PASSWORD`) gates everything. Two pressures:

1. **Conference / public visitors** may not have the technical background for the full UI's depth (settings, calibration, custom ffmpeg commands, CSV export, etc.). The first public showing should be approachable.
2. **GoS membership needs a value proposition.** If the public version is identical to what members get, there's no incentive to join. Membership should unlock real benefits.
3. **Security posture differs.** A public-facing version needs much harder limits (rate-limit, no uploads, no custom commands) than the trusted-member version.

But also:
- We do *not* want to fork the codebase (double maintenance, guaranteed drift).
- We do *not* want to gate the *measurement* itself behind auth — the whole GoS mission is making energy / CO₂e visible. If a casual visitor can't run a workload, the project fails its mission.
- A GoS member showing OWL to a colleague at a conference booth shouldn't have to "find the password" — auth should be optional, additive, low-friction.

### Strategic intent — OWL as a GoS membership funnel

OWL has a **dual purpose**, and the two reinforce each other:

1. **Technical mission (always was):** make the energy and CO₂e cost of streaming workloads visible, on real hardware, against real grid data — earning streaming-industry credibility for GoS. *"Not eco-warriors. Just people who dislike waste."*
2. **Recruitment mission (now explicit with two-tier):** OWL is **a sales tool for GoS itself.** Every public visitor sees not only what OWL measures but also — through the locked rows in the capability matrix — what becomes possible if they join GoS. The capability matrix is **product copy first, security model second.** The locks are the pitch, not the punishment.

**Implications that flow from "OWL is a sales channel":**

- **Conversion design matters.** "Want to Join GoS" CTA copy, placement, and click-through destination (presumably `greeningofstreaming.org/join` or equivalent) is a deliberate design decision, not an afterthought. Decide before the conference launch.
- **Friction on the public side is strategic, not regrettable.** The point of locking custom-upload, calibration, etc. behind membership is not "to keep visitors out" — it's to give them a reason to join. Public capabilities should be **genuinely useful** (measurements are credible, results citable) so visitors form a positive impression *before* they hit a lock.
- **Public usefulness is the recruitment funnel's top-of-funnel.** A weak public version means fewer visitors → fewer membership clicks. This argues for keeping pre-baked content rich enough to demonstrate real insight (Meridian transcode + LLM tasks + image gen + RAG, all the same workloads members get), and never gating *measurement quality* — only inputs and bookkeeping.
- **Worth instrumenting.** The change request should include a basic conversion metric: count "Members only · Join GoS" CTA clicks per week. That's the lagging indicator of whether OWL is actually working as a funnel — and it tells GoS leadership whether to invest more in OWL's public surface.
- **Conference launch (mid-June 2026) is positioned as the funnel's first real test.** First public showing → first wave of streaming-industry visitors → first measurable membership clicks. Worth treating the conference as a launch event for the funnel, not just a demo.

The line that holds against feature creep — *public sees results, members shape inputs* — is also the line that keeps the membership pitch credible. If everything's available publicly, "join GoS" has no answer to "why?".

### Agreed direction

**One deployment, one hostname, three capability tiers** gated at the route layer:

| Tier | How identified | Default landing |
|---|---|---|
| **Anonymous** | No auth cookie | Public landing page (= today's `/demo` Guided Tour, promoted) |
| **Member** | Signed in via magic-link email or GitHub OAuth | Same landing page; "Sign in" affordance flips to member view |
| **Lab** | Request from LAN IP (existing `_is_local()` check) | Full settings + calibration access |

**Key UX framing — a single landing page everyone sees:**
- Anonymous and members arrive at the same page.
- Locked features are *visible* with a "Members only · Join GoS" affordance — the capability matrix becomes the GoS sales pitch.
- Members don't *have* to sign in to demo OWL to a colleague — they get the public experience by default, sign in only when they want a member-only feature.
- Conference / booth demos: zero auth friction.

### Capability matrix (locked = sales pitch for joining GoS)

Measurement quality is identical across tiers — only inputs and bookkeeping differ.

| Capability | Anonymous | Member | Lab |
|---|---|---|---|
| Pre-baked workloads (Meridian, fixed prompts, sample image gens) | ✓ | ✓ | ✓ |
| Live wall-power, CO₂e, comparison strip | ✓ | ✓ | ✓ |
| Guided tour, methodology, Eco2mix mix breakdown | ✓ | ✓ | ✓ |
| Browse public recent runs (anonymised) | ✓ | ✓ | ✓ |
| Custom video upload | — | ✓ | ✓ |
| Custom prompts / custom ffmpeg commands | — | ✓ | ✓ |
| All-codecs / batch / compare-modes | — | ✓ | ✓ |
| RAG corpus upload | — | ✓ | ✓ |
| CSV / JSON export of own runs | — | ✓ | ✓ |
| Per-user run history, named presets | — | ✓ | ✓ |
| `/settings`, variance calibration | — | — | ✓ |

The line that holds against feature creep: **public sees results, members shape inputs.**

### Architecture

- **One systemd unit, one nginx vhost, one cert.** Don't fork.
- **`audience.py` module** — single source of truth. `audience.tier(request)` returns one of `Anonymous | Member | Lab`. Every route declares the tier it needs (`@requires(Member)` or similar). The grep target `requires(` is the security audit.
- **Auth: magic-link email** (libraries like `mailauth`, `magic-link-auth`) preferred over GitHub OAuth — no "create account" step, single click from email. Member email allowlist is a small JSON file (~tens of GoS members, no DB needed).
- **Replace `WATTLAB_GATE_PASSWORD`** entirely. The shared-password gate is the wrong shape for this model.
- **Per-tier rate limiting and queue caps** (must, since same hostname):
  - Anonymous: shared global pool, e.g. queue cap 3 across all anonymous, ~1 measurement/5min per IP.
  - Member: relaxed per-user pool.
  - Lab: uncapped.
  - Conference-day spike from anonymous can't drain the queue and starve members.
- **Public-side hardening:**
  - No upload routes reachable for Anonymous tier.
  - Length caps on any free-text input that does survive (RAG question, prompt).
  - Strict CSP, no `eval`, etc.
  - Aggressive nginx rate limits as a backstop to in-app caps.

### Implementation order (deliverable before mid-June 2026)

1. **`audience.py` + capability tags** (~1 day). Promote `_is_local()` to the richer tier helper. Tag every existing endpoint. No behaviour change yet — this is just the audit harness.
2. **Magic-link auth + member allowlist JSON** (~1 day with library, ~3 if rolling). Replace `WATTLAB_GATE_PASSWORD` cookie with per-user identity. Default state = Anonymous (no redirect to login).
3. **Public landing page + locked-feature UI** (~½–1 day). The capability matrix above, rendered as visible product copy. "Members only · Join GoS" affordances on locked rows. Promote `/demo` content to `/`.
4. **Per-tier rate limits + queue caps** (~½ day). Configured in `settings.json` (`rate_anonymous_per_5min`, `queue_anonymous_cap`, etc.).

### Open questions

- Magic-link email vs. GitHub OAuth — pick by GoS member technical fluency. Default lean: magic-link.
- "Anonymised public runs" feed — is that a feature day-1, or post-conference? Risk of cluttering with low-quality/test runs.
- Member allowlist mechanism: pure JSON file vs. self-serve "request access" flow? Manual approval is fine at this scale (~tens).
- **CTA copy + destination for "Join GoS"** — what does the button actually say, and where does it send the visitor? Coordinate with whoever runs greeningofstreaming.org membership flow.
- **Conversion instrumentation** — count CTA clicks at minimum; do we also want to capture which locked feature triggered the click (e.g. did they click on "custom upload" vs "calibration"), so GoS knows which pitch is landing?

---

## CR-001b · Demo lock (sub-feature of CR-001)

**Status:** captured 2026-05-01 — must ship with or before CR-001.
**Triggered by:** owner running important demos and needing exclusive control of the queue.

### Problem

For high-stakes live demos (conference stage, sponsor pitch, press), the owner needs **exclusive write access** to the OWL queue — only they can run jobs. Anyone else hitting "Run" sees a clear "demo in progress" message.

The risk we want to avoid: forgetting to turn the lock off after the demo. Q&A or hallway conversation can stretch an hour, and by the time the owner remembers, the system has been silently unusable for users in the meantime.

### Agreed direction

**Demo-lock flag with an auto-expire timeout**, modelled on the existing `/tmp/owl-paused` queue-pause flag (introduced session 14 for the local-model router) — same shape, different semantics:

- **Pause flag (`/tmp/owl-paused`):** existing, halts the entire queue. External tool sets/clears.
- **Demo lock (new):** restricts the queue to a single owner identity. Auto-expires.

### Mechanic

- **Flag file:** `/tmp/owl-demo-lock` (parallel to `/tmp/owl-paused`).
  - Contents: JSON `{"owner": "<member_id_or_email>", "started_at": <epoch_s>, "expires_at": <epoch_s>}`.
  - Presence of file = lock active.
  - Absence (or `expires_at` in the past) = lock inactive.
- **Auto-expire:** the lock expires `demo_lock_minutes` after `started_at` (default 60 min, configurable in `settings.json`). The `queue_worker` checks `time.time() < expires_at` before honouring the lock; once past, it ignores the file (and a janitor sweep deletes it).
- **Enforcement point:** in `enqueue()` (single-place). If lock active and `request.user != lock.owner`, return HTTP 423 (Locked) with a friendly "Demo in progress · ends ~13:42 (in 18 min)" message. Already-running jobs continue uninterrupted.
- **UI affordances:**
  - Owner sees a prominent "DEMO LOCK · ACTIVE · expires 13:42 [extend] [end now]" banner on every page (sibling of the existing pause-flag banner on `/queue-status`).
  - Other users see "Demo in progress · jobs queued for you will start at ~13:42" instead of the normal "Run" button — keeps page browsable, only blocks `enqueue`.
  - Floating telemetry badge (`_QUEUE_BADGE`) gains a `🔒 demo` pill (alongside `⏸ paused`).
- **Trigger UI:** owner-only button on `/queue-status` (or `/settings`) — "Start demo lock" → POST `/demo-lock/start`. Sets the flag with current time + `demo_lock_minutes`. "End demo lock now" — DELETE the flag.

### Settings (added to `settings.json`)

- `demo_lock_minutes` (default `60`) — auto-expire window in minutes.
- `demo_lock_owner` — optional fixed owner identity if not deriving from auth (e.g. while CR-001 isn't shipped yet, a hardcoded value works as a stopgap).

### Why this shape

- Same idiom as the existing pause flag (filesystem flag, queue_worker checks on each tick) — no new infrastructure, no new auth model.
- Auto-expire is the safety mechanism for the "I forgot to unlock" failure mode — owner explicitly named this risk, the system handles it without their attention.
- Configurable expiry from `settings.json` so different demo formats (5-min flash demo vs. 90-min workshop) work without code changes.

### Implementation order

Can ship **before** CR-001 — uses today's auth model (the gate password = the implicit owner). When CR-001 lands, the `lock.owner` field becomes a real member identity instead of a stopgap.

### Open questions

- "Extend" button — by `demo_lock_minutes` again, or by 15 min? Probably 15 min — extending in big chunks defeats the auto-expire safety.
- Notification on auto-expire? Probably no — it's intended as a silent safety net. If owner needed to know, they wouldn't have left it on.
