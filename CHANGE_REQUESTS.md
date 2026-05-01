# OWL Change Requests

Design / change requests captured for later implementation. Each entry has a status, a problem statement, the agreed direction, and any open questions. Implementation lives in JOURNAL.md once it lands.

---

## CR-001 · Two-tier OWL: anonymous public + authenticated members

**Status:** captured 2026-05-01 — awaiting implementation slot.
**Triggered by:** demo today (2026-05-01) — discussion of opening OWL to the wider streaming community at the sustainable-streaming conference (mid-June 2026, OWL's first public showing).
**Refined 2026-05-01 (training-prep transcript):** anonymous tier explicitly *can* upload (capped at 100 MB, 1 concurrent job per visitor); LAN/SSH-tunnel auto-detection already implements the Lab tier today; quotas restated below.

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
| Custom video upload | ✓ (≤100 MB, 1 concurrent job) | ✓ (no size cap, programmatic / scheduled allowed) | ✓ |
| Custom prompts / custom ffmpeg commands | — | ✓ | ✓ |
| All-codecs / batch / compare-modes | — | ✓ | ✓ |
| RAG corpus upload | — | ✓ | ✓ |
| CSV / JSON export of own runs | — | ✓ | ✓ |
| Per-user run history, named presets | — | ✓ | ✓ |
| `/settings`, variance calibration | — | — | ✓ |

**Anonymous upload rationale:** 10 MB was floated and rejected — too small, jobs run too fast to lift power above the P110 measurement floor and produce a usable green-light reading. 100 MB sized to give a 1080p clip ~30 s+ of transcode wall-time, comparable to the bundled `meridian_120s` test asset (~123 MB).

The line that holds against feature creep: **public sees results, members shape inputs.**

### Architecture

- **One systemd unit, one nginx vhost, one cert.** Don't fork.
- **`audience.py` module** — single source of truth. `audience.tier(request)` returns one of `Anonymous | Member | Lab`. Every route declares the tier it needs (`@requires(Member)` or similar). The grep target `requires(` is the security audit.
- **Auth: magic-link email** (libraries like `mailauth`, `magic-link-auth`) preferred over GitHub OAuth — no "create account" step, single click from email. Member email allowlist is a small JSON file (~tens of GoS members, no DB needed).
- **Replace `WATTLAB_GATE_PASSWORD`** entirely. The shared-password gate is the wrong shape for this model.
- **Per-tier rate limiting and queue caps** (must, since same hostname):
  - **Anonymous:** 1 concurrent job per visitor (single slot in the queue at a time — no parallel anonymous runs from the same browser session). 100 MB upload cap. nginx-level backstop ~1 measurement/5min per IP.
  - **Member:** relaxed per-user pool — programmatic / scripted / scheduled-weekend runs explicitly allowed (this is part of the membership pitch).
  - **Lab:** uncapped. **Already implemented today** via `_is_local()` — a member SSH-tunneling from outside back to localhost is auto-detected as Lab tier. CR-001 just generalises the existing carve-out into a named tier.
  - Conference-day spike from anonymous can't drain the queue and starve members.
- **Public-side hardening:**
  - Upload route reachable for Anonymous, but capped at 100 MB and gated through `queue_control.enqueue_for(request, …)` so the 1-concurrent-job-per-visitor limit is enforced at the single chokepoint.
  - Length caps on any free-text input (RAG question, prompt).
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

---

## CR-002 · Methodology page accuracy pass

**Status:** captured 2026-05-01 — pre-conference must-fix.
**Triggered by:** training-prep walkthrough of the methodology page (transcript ~T+790s) + owner notes.

### Problem

Three inaccuracies on `/methodology` need fixing before the page is shown to a public audience:

1. **P110 power resolution stated as "1 W" — incomplete and misleading.** The Tapo P110 reports power at **1 W resolution via its public API** (which is what we currently poll), but **1 mW resolution via direct device read** (the underlying instrument is far better than the API exposes). The page should state both numbers and be explicit about which one this deployment uses.
2. **`baseline_polls` hard-coded as `10` in the prose, but `settings.json` defaults to `5`** — disconnect between docs and behaviour. Either render the setting at request time (preferred — single source of truth) or at minimum drop the hard number and refer the reader to `/settings`.
3. **"From energy to CO₂e" section names ElectricityMaps as the only live source** — Eco2mix was added later as the primary live source for France, with ElectricityMaps now a backup. Section needs updating to reflect the actual fallback ladder: **Eco2mix (RTE/Etalab) → ElectricityMaps → Ember 2024 static**. (The result-card formula footer was updated; the methodology page copy was missed.)

### Agreed direction

Single editing pass on the `_METHODOLOGY_HTML` block in `main.py`. No new features — accuracy patch only. Where possible, render values from settings/code at request time so future drift is impossible.

### Pre-conference: must.

---

## CR-003 · Iso-energy bitrate sweep ("I want to spend X Wh, what are my options?")

**Status:** captured 2026-05-01 — likely post-conference.
**Triggered by:** Dom (transcript ~T+1126s and ~T+2398s).

### Problem

OWL currently fixes the bitrate per codec (4 Mbps H.264, 2 Mbps H.265, 1.5 Mbps AV1 — chosen to match real-world ABR ladders) and reports the energy that produces. The inverse question is more interesting for an industry audience: **"given a fixed energy budget, what bitrate / quality options do I have across codecs?"**

Inverts the typical framing — instead of "this codec at this bitrate uses N Wh", asks "if I have N Wh to spend on a one-minute encode, here are my codec/bitrate options". Dom flagged this as IBC white-paper material; owner called it press-worthy.

### Agreed direction

New video-test mode (`video_iso_energy` or similar). Iterates a bitrate range across H.264 / H.265 / AV1 — long-running, intended for overnight or weekend execution — finds the bitrates per codec that produce equivalent energy. Output: chart/table of "for X Wh budget, your options are H.264@Y kbps / H.265@Z kbps / AV1@W kbps."

Possibly pair with a quality metric (mean PSNR / SSIM / VMAF) so the result is "for X Wh, here's your bitrate AND quality across codecs" — Simon flagged in transcript that quality scoring should accompany this.

### Open questions

- Quality metric to use? VMAF is the streaming-industry standard but adds dependency.
- Bitrate sweep granularity? Logarithmic vs linear?
- White-paper scope: just CPU? Just GPU? Both? Cross-grid?

### Pre-conference: unrealistic (long test runs needed). Post-conference: strong candidate, especially as IBC submission.

---

## CR-004 · Visual graphing in OWL

**Status:** captured 2026-05-01 — pre-conference nice-to-have.
**Triggered by:** Dom (transcript ~T+1657s) + owner notes.

### Problem

OWL currently renders all results as metric tables. Trend, variance, and shape are visible only by reading numbers row-by-row. Visitors are visual thinkers; demos land harder with a chart than a table.

### Agreed direction

Add chart rendering to result pages. Three candidates in priority order:

1. **Per-run power trace** — line chart of P110 polls (1s cadence) across the run, showing baseline → ramp-up → workload → cooldown. Makes the ΔW computation visually obvious. Single canvas per result card.
2. **Comparison-mode side-by-side** — bar chart for both/all-codecs/compare-models results, energy + CO₂e + duration on the same axis or stacked. Replaces or supplements the existing summary table.
3. **Historical trend** — small chart on the home page or `/queue-status` showing last N runs' energy across run timestamp. Gives a feel for how stable the lab is over time.

Library choice is open: chart.js (small, easy), uPlot (faster, smaller, ugly defaults), pure SVG (no dep, more code). Probably chart.js.

### Pre-conference: nice-to-have. Would visibly improve demo impact.

---

## CR-005 · Software fan-speed control during tests

**Status:** captured 2026-05-01 — pre-conference nice-to-have.
**Triggered by:** Dom + owner (transcript ~T+1796s, ~T+1840s) + owner notes.

### Problem

The GoS1 server lives in the owner's sitting room with fans set conservatively low for noise reasons. The 2% baseline drift currently visible in calibration runs is partly thermal (the chassis runs warmer over a session). Manually pre-cooling with a desk fan would help but isn't scientific or repeatable.

### Agreed direction

Programmatic fan-speed control around tests:
1. **Before a test starts:** raise fan speed to an aggressive profile (e.g. AMD `pp_dpm_fclk` / `fancontrol` / `nbfc`-style sysfs writes — exact mechanism TBD on AMD/Linux).
2. **After the test ends:** restore the default quiet profile.
3. **Configurable in `settings.json`** — `focus_mode_fan_profile: "aggressive" | "default" | "off"`. Default `"off"` so users who don't want their server howling aren't surprised.
4. **Bonus:** capture the fan-profile-used in the result JSON for reproducibility.

### Open questions

- Exact mechanism on this hardware (Ryzen 9 7900 + RX 7800 XT + the chassis fans) — needs investigation. Likely a combination of GPU PWM (via `/sys/class/drm/card0/device/hwmon/`) and chassis fans (motherboard EC, possibly out of reach without IPMI/BMC).
- Should this be exposed as part of focus mode (sudoers-gated stop-timers script) or as a separate sub-feature? Probably bundled into focus mode for cohesion.

### Pre-conference: nice-to-have, improves measurement quality (lower baseline drift = tighter green-light thresholds).

---

## CR-006 · Move AI workloads (LLM, RAG, image-gen) to a "beta / skunkworks" area

**Status:** captured 2026-05-01 — pre-conference, important for visitor framing.
**Triggered by:** Dom (transcript ~T+2516s; owner agreed).

### Problem

OWL's home page currently presents Video / Image / LLM / RAG as equal first-class workloads. The video work is mature, repeatable, and on-mission for GoS (streaming impact). The AI workloads are exploratory, sometimes below the P110 measurement floor (TinyLlama short-task), and at risk of diluting GoS's streaming focus when shown to a streaming-industry audience.

### Agreed direction

Restructure the navigation so:
- **Primary, prominent:** Video (transcoding) — the main GoS story.
- **Beta / Skunkworks (visually de-emphasised, separate section):** LLM, RAG, Image generation. Still fully accessible, but framed as "exploratory work, energy/quality/faithfulness tradeoffs we're investigating" rather than "here's our authoritative answer."

Affects:
- Home page nav structure (move AI links into a labelled "Beta" or "Exploratory" group).
- Guided Tour ordering — video stays as the headline; AI workloads may move later or to a separate skunkworks tour.
- Possibly methodology page sectioning (clearer "production vs. exploratory" framing).

### Pre-conference: important — shapes what conference visitors see first.

---

## CR-007 · Carbon variance study over time-of-day / season / location

**Status:** captured 2026-05-01 — possible pre-conference talking point if scoped tight.
**Triggered by:** Simon + Dom (transcript ~T+2029s onwards).

### Problem

OWL now reports gCO₂e against live grid intensity, but **the variance of that intensity itself** isn't characterised. Dom raised the right framing: if the carbon intensity of the grid varies by 1000% across the day, optimising your code by 1% is noise. If the grid varies by 1% and your code variation drives 50%, your code matters more. Without knowing which regime you're in, optimisation effort is mis-targeted.

### Agreed direction

Background or one-shot job:
1. Take a **standard fixed-energy reference workload** (e.g. exactly 1 Wh of compute — could be a calibrated transcode or a synthetic CPU hold).
2. Pull historical Eco2mix data for the last N months.
3. Compute the resulting gCO₂e variance for that 1 Wh workload as a function of:
   - Hour of day
   - Day of week
   - Season
   - Comparison location (UK / Germany / Poland — using their available historical data)
4. Render: a chart (CR-004 territory) plus a punchy summary line ("your 1 Wh workload, run in France, swung from X g to Y g over the last 6 months — Z× spread").

Possible deliverable: a methodology-page sub-section, a separate `/grid-variance` page, or a one-off white paper.

### Output value

Strong conference talking point — speaks directly to Simon's "schedule your work to carbon-efficient times" thesis. Could become guidance for operators / regulators on workload scheduling. *"Move your workload to this time slot for X% lower carbon."*

### Pre-conference: candidate if scoped tight. Worth a half-day spike to assess.

---

## CR-008 · REM ↔ OWL integration

**Status:** captured 2026-05-01 — branding step pre-conference, full integration is post.
**Triggered by:** Dom + owner across the transcript (~T+486s, ~T+2160s, ~T+3151s, ~T+1014s).

### Problem

GoS now has two measurement tools that were built independently:
- **OWL** — single-server, fine-grained, encoder-side energy + CO₂e.
- **REM** — multi-machine, end-to-end streaming workflow, less granular.

They're **complementary, not competing**, but currently they look like separate projects. For a GoS audience, the right framing is "REM = end-to-end at scale; OWL = deep dive at the encoder; together they cover the streaming pipeline."

### Agreed direction (multi-step)

1. **(Pre-conference)** Pull REM source code into the Claude project context so cross-understanding is possible. Owner action item from transcript.
2. **(Pre-conference)** Update REM with **OWL branding and visual style** — same owl mark, same `#00ff99` accent, same dark theme — so they read as one coherent GoS system. Dom's request.
3. **(Post-conference)** Genuine data interoperability — OWL exporting in a format REM can ingest, or vice versa. Mash-up view where 100s of homes report from REM and 1-2 contribute high-resolution OWL-style local measurements; visualised together.
4. **(Long-term, exploratory)** OWL acting as the encoder in a REM-orchestrated end-to-end test (encoder → intermediary server [Linode / TNO / Bristol] → client). Auto-hackathon workflow (see CR-009).

### Pre-conference: branding pass is feasible; data integration is post.

---

## CR-009 · Cross-platform web client test bay

**Status:** captured 2026-05-01 — post-conference.
**Triggered by:** Dom (transcript ~T+1431s, ~T+2940s); Simon flagged the long-standing problem this solves (~T+1394s).

### Problem

Real-user-measurement (RUM) at the client side is the missing piece in GoS's measurement coverage. Hackathons have done it manually with TVs. Simon's prior attempts at automation hit a wall: when the encoder switches codecs/bitrates mid-stream, **media players have to be restarted**, which can't easily be done on a TV remotely. So every hackathon needs a human pressing play between tests.

### Agreed direction

Web-based test client that uses page-reload as the "restart media player" mechanism:
- **Server side:** runs a 9-minute test sequence inside a 10-minute slot.
- **Client side:** a thin web app that auto-refreshes the page every 10 minutes (using AJAX / `setTimeout` / page reload). Each refresh loads a fresh `<video>` element with the next stream's URL. Synchronised by clock, not by event.
- **Cross-platform:** runs in any browser — iOS, Android, Roku, Apple TV, Samsung. No native app needed.
- **Anonymous contribution flow:** "Test your device now" button on OWL's public landing — visitor leaves their browser open for an hour or two, contributes data, sees their result.
- **Booking model (Dom):** since you can only have one active client tester at a time (to keep variance bounded), a slot-booking page lets contributors pick a 2-3 hour window over the weekend.

### Action items from transcript

- Dom to share his prior auto-refresh / autoplay code with Ben.
- Simon to dig out his earlier server-side automation work (he had Cron-based scheduling on the server but never the client).

### Effort estimate

Dom guessed five days of Claude Code work. Probably correct order of magnitude. Cross-platform browser quirks (autoplay policies on iOS especially) will eat real time.

### Pre-conference: unrealistic.
### Post-conference: high leverage — turns OWL into a contribution-driven RUM platform, not just a single-server lab.

---

## Caught during the session but **not** new CRs

For the record, several items came up that don't warrant new CR entries:

- **Bug: `/settings` page rendered empty mid-run** (~T+338s) — owner observed this when trying to demo settings during a queued calibration. Filed as a bug to investigate, not a CR. May be related to job-state machine showing the page in a transient state. Repro: start a calibration, immediately reload `/settings`.
- **Confidence multipliers (5× / 2×) need statistical grounding from Tanya** — already in CLAUDE.md "Open Questions" / Deferred. No new CR.
- **Codec apples-to-apples equivalence (GOP, profile)** — already in CLAUDE.md Deferred. No new CR.
- **Long-term mash-up of REM + OWL data for 100s of homes** — covered as the post-conference phase of CR-008. No separate CR.
- **"Counter for OWL's own compute footprint"** (Dom, ~T+3319s, in passing) — fun meta-toy, not load-bearing. Skip.
- **The 5-minute training narrative was generated mid-meeting** — captured separately if needed. No CR; deliverable not infrastructure.
