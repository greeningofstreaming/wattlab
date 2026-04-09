# WattLab — Project Journal

## About
WattLab is GoS's live energy measurement platform. It makes the energy cost of real-world content generation and manipulation visible, credible, and reproducible — using primary measurement data, not estimates. Not a dashboard. Not a calculator. A lab.

Scope: device layer only (GoS1). Network, CDN, and CPE explicitly excluded.

---

## Session 12 — 2026-04-10

### What we did

**Preset overhaul · Full GPU pipeline · VAAPI fix · meridian_120s · Confidence hints · Guided tour RAG step · Queue badge · Bug fixes**

#### Video preset UI restructure
- Three codec rows (H.264 / H.265 / AV1), each with CPU / GPU / Both cards
- Preset details now collapsible via `<details class="pdesc">` — arrow toggles `▸/▾`, muted grey text
- `.pspec` class for codec spec line (was inline style); `DEFAULT` badge removed from H.264 Both
- Source picker: meridian_120s added between upload and full meridian

#### Full GPU pipeline — significant methodology change
Previously all GPU presets used a **partial pipeline**: ffmpeg CPU software-decoded the 4K input, then GPU-encoded the result. This meant the CPU was working hard during "GPU" jobs — and appeared hotter during GPU runs than CPU runs (counterintuitive but correct: software decode + PCIe DMA heats the IOD).

Now all three GPU codecs use a **full pipeline**:
```
-hwaccel vaapi -hwaccel_output_format vaapi -extra_hw_frames 32 -vaapi_device /dev/dri/renderD128
```
Frames stay GPU-resident from decode through scale through encode. This represents real live-encoding workflows (Harmonic, Ateme) and cuts CPU thermal load during GPU jobs.

**Impact on energy results** — dramatic:
| Mode | Duration | Energy | ΔW |
|---|---|---|---|
| H.264 CPU | 30.6s | 0.664 Wh 🟢 | 78.2W |
| H.264 GPU (full) | 17.6s | 0.376 Wh 🟢 | 76.8W |
| AV1 CPU | 28.2s | 0.586 Wh 🟢 | 74.8W |
| AV1 GPU (full) | 14.5s | 0.284 Wh 🟢 | 70.5W |

GPU is now faster **and** more energy efficient (H.264: 43% less energy; AV1: 51% less). Old partial-pipeline result (GPU 9.7% more energy) is superseded.

#### VAAPI surface pool fix
GPU encodes were failing with `Cannot allocate memory` at frame ~7178/7193 (99.8% through a 2-min 4K clip). Root cause: Mesa VA-API exhausts the DMA surface pool when `scale_vaapi` flushes at end-of-stream. The error is in teardown — the muxer had already written the full output file (confirmed: `Lsize=11188kB` in stderr before "Conversion failed!").

Two fixes:
1. `scale_vaapi=w=-2:h=1080:format=nv12` — explicit pixel format prevents EOS format-renegotiation failure
2. `out_size_mb` now checks `file.exists() and size > 0` instead of `success=True` — output is valid even when ffmpeg exits non-zero from the EOS teardown error

#### meridian_120s — 2-minute demo extract
Generated with `ffmpeg -y -ss 0 -i meridian_4k.mp4 -t 120 -c copy meridian_120s.mp4` (123MB). Full Meridian 4K gave only ~8 polls on short GPU jobs (🟡). The 120s extract gives 14–30 polls per job, all 🟢. Added to source picker and `sources.py`.

#### Confidence hint
`confidence()` now returns an optional `hint` string when signal is strong (ΔW > green threshold) but the task ran too briefly for enough polls (poll count < `conf_green_polls`). Example: *"Strong signal (49× noise floor) — task too short for 🟢. Use a longer clip or batch mode."* Rendered in single and both result cards beneath the flag.

#### Guided tour update
- 7 steps (was 5): RAG step inserted as step 4 before the Confidence step
- Page X/Y counter on all steps
- Previous buttons on steps 1–6
- Confidence step updated to variance-relative language with formula `noise = (variance%/100) × W_base`
- RAG result card shows model size, input/output tokens, retrieval_ms, mWh/token, tok/s, confidence per mode

#### Queue badge
Always-visible fixed bottom-right badge on all pages: polls `/power` + `/queue` every 5s, shows e.g. "52.3 W · ⏱ 2 jobs". Shows watts even when queue is empty.

#### Bug fixes
- `h265_both`/`av1_both`/`av1_gpu` missing from `STAGES`/`STAGE_MAP` → "Cannot read properties of undefined (reading 'starting')" crash on any new preset. Fixed by adding all new types to both tables.
- Variance calibration save-before-run: `runVarianceCalibration()` now calls `await saveSettings()` before queuing — previously the live value wasn't saved and the old settings.json value was used.
- Queue page resume link 404 for variance jobs: `resumeLink()` now skips variance-type jobs.
- Previous runs: codec displayed (e.g. "H.264 CPU vs H.264 GPU"); `persist.py` both-mode summary includes `cpu_preset`/`gpu_preset`.

### Technical notes
- Full GPU pipeline: add `-extra_hw_frames 32 -vaapi_device /dev/dri/renderD128` before `-i`; use `scale_vaapi=w=-2:h=1080:format=nv12` (not `scale_vaapi=-2:1080`)
- `variance_cooldown_s` serves double duty: CPU→GPU gap within a pair AND GPU→next pair gap. 10s is too short (CPU reaches 60°C during H264). Recommended: 60s minimum.
- Variance calibration uses full `meridian_4k.mp4` (not the 120s extract). With 10 runs + 60s cooldown: ~68 min total.
- VAAPI "Cannot allocate memory" is an EOS bug in Mesa VA-API, not actual VRAM exhaustion. Output file is valid.

### Deferred (carried forward)
- DNS + SSL (blocked on DNS rebuild)
- GPU image generation: first clean measurement run
- Image page elapsed time in progress bar
- phi4 (14B): `ollama pull phi4`
- Transcoding profile documentation (apples-to-apples bitrate/GOP/profile)
- Confidence multiplier grounding with Tanya

---

## Session 11 — 2026-04-09

### What we did

**Methodology page · Variance-based confidence · ffmpeg command preview + edit · Variance calibration tool · CLAUDE.md updates**

#### /methodology page
- New standalone page at `/methodology` with full measurement methodology documentation
- Covers: scope, measurement principle, protocol (8 steps), energy formulas, confidence framework, hardware disclosure, test type descriptions, known limitations, open questions
- Linked from home nav utility row (alongside Queue and Settings)
- Static HTML embedded in `main.py` as `_METHODOLOGY_HTML` string constant (no f-string — avoids CSS brace escaping)
- HTML written externally on MacBook, transferred via `scp -P 2222` and embedded

#### Variance-based confidence framework (major change)
**Old:** Fixed absolute ΔW thresholds — 🟢 >5W, 🟡 ≥2W. Problem: does not reflect actual measurement system noise; arbitrary and not grounded in empirical data.

**New:** Variance-relative thresholds anchored to empirically measured system noise.
- `noise_w = (variance_pct / 100) × w_base` — noise in watts, computed at measurement time from baseline power
- 🟢 ΔW > `variance_green_x × noise_w` AND polls ≥ `conf_green_polls`
- 🟡 ΔW ≥ `variance_yellow_x × noise_w` OR polls ≥ `conf_yellow_polls`
- 🔴 below yellow threshold
- Defaults: `variance_pct=2.0%`, `variance_green_x=5.0×`, `variance_yellow_x=2.0×`
- At 55W idle: noise_w ≈ 1.1W, green threshold ≈ 5.5W, yellow ≈ 2.2W — similar to old values but now scale correctly with idle power and adapt as variance is calibrated
- Variance captures total system noise: P110 quantisation + OS background processes + Wi-Fi polling jitter + thermal drift combined
- `confidence()` function updated in all four modules: `video.py`, `llm.py`, `image_gen.py`, `rag.py` — new signature: `confidence(delta_w, poll_count, w_base)`
- Old settings keys `conf_green_delta_w` and `conf_yellow_delta_w` removed from `settings.py`

#### New settings keys (settings.py + settings page)
Added to `DEFAULTS` and Settings page Confidence section:
- `variance_pct` — measured system variance as % of baseline; auto-updated by calibration
- `variance_green_x`, `variance_yellow_x` — multiplier thresholds (default 5×, 2×)

Three read-only calibration output fields shown in the Confidence thresholds section (above the editable `variance_pct`):
- `variance_idle_pct`, `variance_cpu_pct`, `variance_gpu_pct` — display "—" until first calibration run
- Visually distinct (dimmer label, no input control); `calib_field()` helper in settings_page()
- These are always read-only (even on LAN) — only updated by a calibration run

New Settings page section **Variance calibration** with:
- `variance_runs` slider (5–100, step 5) — number of H264-CPU + H265-GPU run pairs
- `variance_cooldown_s` slider (10–300, step 10) — cooldown between each pair
- `variance_cpu_cmd` textarea — editable ffmpeg command template (H.264 CPU, `{input}`/`{output}` substituted at runtime)
- `variance_gpu_cmd` textarea — editable ffmpeg command template (H.265 GPU)
- **▶ Run variance calibration** button (LAN only) — queues the calibration job
- Settings page gains `slider_field()` and `textarea_field()` helpers alongside existing `field()`

#### Variance calibration job (`/variance/run`)
- POST endpoint, LAN-only (403 on public)
- Queues a job labelled "Variance calibration — system offline"
- `run_variance_calibration()` in `video.py`: runs N × (H264-CPU baseline→encode + cooldown + H265-GPU baseline→encode) on Meridian
- **Three separate CVs** (revised after first run showed 24.62% — root cause: original code pooled H264 ΔW ~30W and H265 ΔW ~70W together, so CV was measuring workload difference not instrument noise):
  - `variance_idle_pct` — CV of raw P110 readings across all inline baseline polls
  - `variance_cpu_pct` — CV of ΔW across all H264-CPU encode runs
  - `variance_gpu_pct` — CV of ΔW across all H265-GPU encode runs
  - `variance_pct` — mean of the three (the operative noise estimate used for confidence thresholds)
- All four values written to `settings.json`; three read-only calibration fields shown in Settings page above the editable `variance_pct` field
- Stage labels visible in queue status: `run_1/N_cpu_encode`, `run_1/N_cooldown`, `run_1/N_gpu_encode`, etc.

#### ffmpeg command preview + edit on video page
- Before clicking Run, the ffmpeg command that would be executed is shown below the preset selector
- `/video/preview-cmd?preset=<key>` endpoint returns command template(s) with `{input}` and `{output}` placeholders
- On LAN: editable `<textarea>` (single preset: one box; "both" mode: CPU and GPU boxes stacked)
- On public: read-only `<pre>`-style code block
- Preset selection triggers `fetchCmdPreview()` JS; initial preview loads on page load
- Edited command sent as `custom_cmd` (single) or `custom_cmd_cpu`/`custom_cmd_gpu` (both) in form POST
- Server substitutes `{input}`/`{output}` at run time via `apply_custom_cmd()` in `video.py`
- `run_single()`, `run_video_measurement()`, `run_both_measurement()` all accept optional custom cmd params, threaded through `run_job()`, `/video/upload`, `/video/use-source`
- `IS_LAN` constant injected server-side into page JS so render is request-aware

#### CSV export updated
- `persist.py` `_video_result_row()` now includes `ffmpeg_cmd` column
- Fieldnames updated to match

#### /methodology confidence section updated
- Rewrote to explain variance-relative thresholds with formula block (`noise_w = variance_pct/100 × W_base`)
- Added explanation of why variance-relative is better than fixed thresholds
- Updated P110 noise floor callout to describe total system noise (not just P110 hardware)
- Open question updated: confidence multipliers (5×/2×) acknowledged as judgement-based pending statistical grounding session with Tanya

#### CLAUDE.md / SSH tunnel note
- Added "See also: GOS1_INFRA.md" reference line after Last updated
- Updated disk free: 221GB (April 2026)
- Clarified SSH tunnel: access via `http://localhost:8000/` not `http://192.168.1.62:8000/` — LAN IP is unreachable from outside the home network

### Technical notes
- `confidence()` now takes `w_base` as third argument in all modules — any future module must pass this
- `variance_pct` in settings.json is the live calibration value; change it manually or via calibration run
- `{input}` and `{output}` placeholders are substituted by `apply_custom_cmd()` (shlex-split after substitution)
- Calibration output files are written to `/tmp/wattlab_uploads/` and deleted after each pass

### Deferred (carried forward)
- DNS + SSL (blocked on DNS rebuild)
- GPU image generation: first clean measurement run
- Image page elapsed time in progress bar
- phi4 (14B): `ollama pull phi4`
- Transcoding profile documentation (apples-to-apples)
- CPU temp under GPU load: investigation
- Confidence multiplier grounding: working session with Tanya (thresholds now variance-relative but multipliers still by judgement)

---

## Session 10 — 2026-04-07

### What we did

**Video upload fix · Centralized power cache · FFmpeg audit · Home nav restructure · Meeting debrief**

#### Video upload 413 fix
- nginx `client_max_body_size` was defaulting to 1MB — any upload over that returned a 413 HTML error page, which the JS tried to parse as JSON → `SyntaxError: Unexpected token '<'`
- Added `client_max_body_size 2g` to the HTTP server block in `infra/wattlab.nginx.conf` (and to the commented HTTPS block for when SSL goes live)
- Root cause of "fix didn't work first time": `systemctl reload nginx` does a graceful restart — old workers (from Apr 05) kept running with the old 1MB config. Required `systemctl restart nginx` to kill all workers and spawn fresh ones with the new config.
- JS error message improved: now context-aware — only shows "file too large (nginx limit)" when it was actually an upload AND actually a 413. Other failures show `Failed (HTTP NNN)` without the misleading hint.

#### Centralized power cache
- Previously every browser session independently polled the P110 every 10s on page load and every 3–5s during measurement display. Multiple simultaneous users saw different wattage values and the P110 was hammered concurrently.
- Added `_power_cache: dict` global and `power_poller()` background coroutine (started at app startup alongside `queue_worker()`). Polls P110 every 5s, updates cache. On transient errors, stale value is kept.
- `/power` endpoint now returns from cache (dict read, no I/O). Home page reads from cache on page load — no direct P110 call on HTTP request path.
- Measurement workers (`video.py`, `llm.py`, etc.) still poll P110 directly at 1s intervals — measurement accuracy unchanged.
- Result: all browser sessions see the same value; P110 is polled at a steady 5s cadence regardless of how many users are connected.

#### FFmpeg command in result JSON and UI
- `transcode()` in `video.py` now returns `ffmpeg_cmd` (the full command string including `nice -n -5`) in the result dict.
- Surfaced in the result card (single and both modes) as a collapsible `▶ ffmpeg command` disclosure element under the Encode section.
- Addresses the Stan/meeting question: "what exactly is happening to the input file?" — the exact command is now visible and saved in the result JSON for auditability and reproducibility.
- Note: only new runs (post this session) will have the field. Old saved results show nothing for the ffmpeg section.

#### GPU PPT explanatory note
- GPU self-reported power (PPT from `amdgpu PPT power1_average`) was already captured and shown in result cards, but the discrepancy with P110 ΔW was confusing (meeting: "GPU reported 44W but P110 delta showed 85W — why?").
- Added a one-line note beneath the PPT row in single and both-mode result cards: *"GPU self-reported power (PPT). P110 ΔW above is the full system delta — includes CPU, RAM, drives."*

#### Home nav restructure
- Video promoted to its own full-width row beneath Guided Tour.
- Image / LLM / RAG grouped under a dim "AI WORKLOADS" label in a secondary row.
- Queue / Settings demoted to a utility row (smallest, dimmest).
- Reflects meeting consensus: GoS's core story is video transcoding; AI workloads are secondary.

#### DNS situation
- DNS table was wiped during Wix ownership transfer from Dom to Ben.
- `wattlab.greeningofstreaming.org` A record needs to be re-added once DNS is rebuilt.
- In the meantime, `http://176.148.88.254` (public IP, no DNS needed) is working and was used for the dry run.
- SSL cert deferred until DNS is restored.

#### Meeting debrief (WattLab Monthly, Apr 07)
Attendees: Ben, Stan (IABM), Barbara Lange, Carl (Akamai). Key feedback:

**Methodology gaps raised by Stan:**
- FFmpeg pipeline: does it decode to baseband? What intermediate format? → Fixed (ffmpeg command now logged).
- Apples-to-apples: all presets must use comparable profiles (same bitrate target, GOP, profile level). Currently undocumented beyond the command string. To follow up with Tanya/Simon.

**GPU PPT vs P110 delta (~18min):**
- GPU self-reported ~44W, P110 system delta ~85W during GPU encode. Explained: CPU also active during GPU encode (loading/sending data). Added explanatory note to UI.

**CPU heats more under GPU load than CPU load:**
- Unexplained observation from the demo. Hypothesis: CPU handles memory transfers for GPU. To investigate.

**Audio measurement question:**
- Can we measure the energy impact of audio volume? Ben tested informally (TV plug, full vs min volume) — delta within P110 noise floor (~1W on a 50–200W device). Stan will contact an audio expert (AES Canada chapter).

**Image gen and LLM scope:**
- Stan and Ben agreed these are off-brand as primary features. Moved to "AI workloads" secondary section in nav.

**Public access:**
- Upload worked for all testers once the 413 fix was deployed.
- Barbara confirmed settings page was read-only (by design).
- Queue worked correctly under concurrent load.

**Confidence flags:**
- Ben wants a working session with Tanya to make the thresholds more statistically rigorous.

**Deferred / action items from meeting:**
- CPU temp under GPU load: investigate and document
- Transcoding profile documentation (apples-to-apples): work with Simon/Tanya
- Audio measurement: Stan to contact audio expert
- DNS rebuild: whenever Dom/Ben can access DNS panel
- SSL cert: after DNS
- Akamai meeting rescheduled: Apr 23, 3pm UK, Simon to be invited

### Deferred (carried forward)
- DNS + SSL (blocked on DNS rebuild)
- GPU image generation: first clean measurement run
- Image page elapsed time in progress bar
- phi4 (14B): `ollama pull phi4` — for RAG quality comparison
- Confidence threshold refinement: working session with Tanya
- Transcoding profile documentation (apples-to-apples across H.264/H.265/AV1)
- CPU temp under GPU load: investigation

---

## Session 9 — 2026-04-06

### What we did

**RAG energy test · Compare 3 modes · Shared progress component · Home nav restructure**

#### RAG energy test page (`/rag`)
New test type measuring the energy cost of Retrieval-Augmented Generation vs plain LLM inference:
- Three modes: **baseline** (cold LLM, no retrieval), **rag** (top_k=3 chunks), **rag_large** (top_k=8)
- Backend: ChromaDB + `all-MiniLM-L6-v2` sentence-transformer embeddings (singletons, loaded once)
- Corpus: PDF files from `settings.rag_corpus_path`, chunked at ~512 tokens with 64-token overlap
- Index build: `/rag/build-index` endpoint + status polling; index persists in `.chroma/` across restarts
- Same P110 measurement protocol as other tests (baseline → task → ΔW/ΔE/mWh/token)
- New module `rag.py`, new `persist.py` branches for RAG result summary and CSV export
- Supports TinyLlama, Mistral 7B, Phi-4 model selection

#### RAG — Compare 3 modes
**▶▶ Compare 3 modes** button runs baseline → rag → rag_large sequentially in one job:
- Single baseline measurement shared across all three modes (unload + re-baseline between each)
- Live progress: shows current mode, stage (baseline/inference), live wall power, elapsed time
- Result: three side-by-side cards (or stacked on mobile) with energy, tokens/sec, confidence badge per mode
- Answer text collapsible per card (toggle button); answers saved in result JSON for quality comparison
- Backend: `run_rag_compare_job()` coroutine, `/rag/run-compare` endpoint

#### Shared `_PROGRESS_JS` component
Progress display factorized out of all 4 test pages into a single `_PROGRESS_JS` plain-string constant:
- `wlRenderProgress({header, stagesHtml, watts, elapsed, extraHtml})` — renders 2.5rem live wall power, stage list, elapsed timer into `#status` div
- `wlStageList(stages, currentStage)` — renders coloured pip list
- `wlRenderQueued(position)` — renders queue position banner
- `wlFormatElapsed(ms)` — formats elapsed time as `Ns` or `Nm Ns`
- All 4 pages (video, LLM, image, RAG) inject `{_PROGRESS_JS}` and call shared functions

#### Home nav restructure
New three-tier layout (mobile-friendly, `flex-wrap` on all rows):
1. **◆ Guided Tour** — solid green filled button, most prominent
2. **Primary row** — Video · Image · LLM (outlined green, ordered by visual weight)
3. **Secondary row** — RAG · Queue · Settings (muted grey, smaller text)

#### Bug fixes
- **RAG JS syntax error** (page unresponsive after first implementation): `\'` in Python triple-double-quoted f-strings outputs `'` not `\'`, causing `getElementById('' + answerId + '')` — adjacent JS string literals → SyntaxError. Fixed by using `data-id` attribute pattern (`data-id="..." onclick="toggleAns(this.dataset.id)"`) — no nested quote escaping needed.
- **RAG Internal Server Error** (prior session): `{{}}` double-brace escaping used in plain Python functions (not f-strings) → `unhashable type: dict`. Fixed by removing double-braces from all endpoint functions.

### Deferred
- DNS: table lost during Wix ownership transfer (Dom → Ben). A record `wattlab.greeningofstreaming.org → 176.148.88.254` needs to be re-added once DNS is rebuilt. SSL cert follows after that.
- GPU image generation: first clean measurement run still needed
- Image page elapsed time in progress bar
- phi4 (14B): `ollama pull phi4` (9.1GB) — for RAG quality comparison

---

## Session 8 — 2026-04-05

### What we did

**Peer review response · README · Confidence flags · Guided Tour polish · Password gate · Queue resume**

#### External code audit (another AI)
Received a structured review of the codebase. Agreed findings acted on this session:
- Missing README (fixed)
- Confidence flag description too buried (fixed — popover + Guided Tour step)
- Guided Tour felt like a repackaged lab screen (fixed — three-band structure)
- `confidence()` flag values flagged as potentially empty — confirmed clean (🟢/🟡/🔴 correct in all three modules), no fix needed

Deferred (valid but not pre-demo priority): main.py refactor into routes/, Jinja templates, typed models, tests.

#### README added
- What WattLab measures and explicitly doesn't (network, CDN, training cost)
- Hardware spec, key findings table, access instructions (public vs SSH tunnel)
- Links to WATTLAB_SPEC.md and JOURNAL.md
- How to run locally

#### Guided Tour: three-band structure per step
Each measurement step (Video, LLM, Image) restructured into three explicit bands:
1. **What this shows** — the insight, 1–2 sentences
2. **What we're doing** — concrete action + methodology in collapsible drawer
3. **Result** — action button / result card + limitation note (scope + what the figure does not mean)
Added `.band`, `.band-label`, `.limitation` CSS classes. Fixed step 3 which used undefined `.step-intro` / `.method-box` classes.

#### Guided Tour: confidence flag step
New step 4 "How We Flag Confidence" — explains P110 noise floor (~1W), the three-level system with thresholds, and why those specific values (5:1 SNR reasoning, batch mode as correct response to yellow/red). Findings promoted to step 5. Nav updated to 6 dots.

#### Confidence flag popover on all result pages
`_CONF_HELP_WIDGET` — a plain-string constant (not f-string) injected into video, LLM, image, and tour pages. Clicking any 🟢 🟡 🔴 badge opens a fixed popover with all three thresholds and ΔW definition. Event delegation so it works on dynamically rendered badges. `.conf-badge` gets `cursor:pointer` via injected `<style>` tag.

#### Password gate
Cookie-based gate for private preview period:
- First visit → password form ("WattLab · Private preview")
- Correct password → 30-day httponly cookie, full access
- Password stored in `.env` as `WATTLAB_GATE_PASSWORD` (gitignored)
- FastAPI middleware, exempts `/gate` paths only

#### `_is_local()` security fix
Previous check (`"greeningofstreaming.org" not in host`) allowed direct public IP access (e.g. phone over 5G to raw IP:8000) — treated as local. Replaced with IP-based check: uses `X-Real-IP` (set by nginx) when present, otherwise `request.client.host`. Returns True only if loopback or RFC-1918 private address.

#### Navigation cleanup
- `_BACK` renamed: "← Dashboard" → "← Home" across all pages
- "← Lab mode" button removed from Guided Tour welcome step (redundant with ← Home)
- "Lab mode" link removed from Guided Tour Findings step (same reason)

#### Queue resume
- `enqueue()` now stores `type` and `label` in `jobs` dict (previously lost when job was popped from `pending_queue` to start running)
- `/queue` endpoint exposes `type` and `label` on running job
- Queue page: "↩ Resume" link on each card → `/video?job=id`, `/llm?job=id`, `/image?job=id`
- Video / LLM / Image pages: check `?job=` param on load, call existing poll function — handles in-progress and already-done cases without extra logic

### Tags
- `v1.0.0` — first public-ready commit (Session 7 + security fix)
- `v1.1.0` — README + Guided Tour three-band + confidence popover (Session 8)

### Deferred
- DNS A record + SSL cert (after Easter, pending Wix admin access)
- GPU image generation measurement figures (next clean run)
- Image page elapsed time in progress bar
- RAG experiment — prototype on MacBook first (corpus there, faster iteration), then port to GoS1 as new test type if energy trade-off is measurable

---

## Session 7 — 2026-04-05

### What we did

**Demo renamed to Guided Tour · Settings read-only on public internet · nginx rate limiting · Queue cap**

#### Demo mode → Guided Tour
- Nav link on home page: "◆ Demo mode" → "◆ Guided Tour"
- Page `<title>` updated: "WattLab — Guided Tour · Greening of Streaming"
- Welcome step button changed: "Start Tour →"
- URL unchanged: `/demo`

#### Settings: graceful read-only from public internet
Previous approach was a hard 403. Replaced with a friendlier read-only view:
- Public visitors (Host header contains `greeningofstreaming.org`) see all values as plain-text spans, no inputs, no Save button
- Banner: "🔒 Read-only — settings can only be modified from the lab network or SSH tunnel."
- Subtitle: "WattLab · GoS1 · Read-only" (vs "WattLab · GoS1 · Lab mode" on LAN/SSH tunnel)
- POST `/settings` still returns 403 if not local — belt and suspenders
- `_is_local(request)` remains the single gating function for both GET and POST

#### nginx: removed /settings 403, added rate limiting
- Removed `location /settings { return 403; }` — no longer needed since FastAPI degrades gracefully
- Added `limit_req_zone` (4 job submissions/min per IP, burst 2) and `limit_conn_zone` (3 simultaneous per IP)
- New rate-limited location block for job submission endpoints: `/video/use-source`, `/video/upload`, `/llm/run`, `/llm/run-all`, `/image/start`
- HTTPS server block moved to commented-out section (uncomment after cert is issued)

#### Queue: hard cap added
- `MAX_QUEUE_DEPTH = 8` (total queued + running)
- `enqueue()` now returns `None` when full instead of always returning a position
- All 4 submit endpoints check for `None` and return HTTP 429 "Queue full — try again later."

#### UI navigation cleanup
- Added `_BACK` global: "← Dashboard" link used on all sub-pages
- Added `_FOOTER` global: GoS logo in a footer `<footer>` element, consistent across pages
- Home: removed fixed-position logo div, logo now in footer
- Video + LLM pages: replaced inline `{_LOGO}` with `{_BACK}` at top, old "← Back to power monitor" anchor removed, `{_FOOTER}` added at bottom

### Deferred (unchanged from Session 6)
- DNS A record + SSL cert (after Easter, pending Wix admin access)
- GPU image generation measurement figures (next clean run)
- Image page elapsed time in progress bar

---

## Session 6 — 2026-04-05

### What we did

**Phase 6 progress + GPU image gen confirmed + bug fixes**

#### Phase 6 — Public access progress
- nginx setup script run on GoS1 (Step 1 complete)
- BouyguesBox port forwarding configured: TCP 80 + 443 → 192.168.1.62 (named `wattlab-http` / `wattlab-https`)
  - Pre-existing rules `apache` (port 80) and `ssh` (port 22) deleted first — both pointed to 192.168.1.1 and were left over from the owner's son's personal projects. Port 80 conflict would have silently broken nginx.
- DNS A record blocked until after Easter — requires Wix domain admin access not yet granted
- Confirmed: GoS1 auto-starts correctly after reboot (wattlab + ollama both `systemctl enabled`)
- Confirmed: uploaded test videos are deleted after transcoding (`delete_after=True` in `run_job`)

#### GPU image generation — first confirmed run
- SD-Turbo float16, ROCm, batch of 5 images, 20 steps, 512×512 — works correctly
- Image displays in result, prompt variation working
- (Measurement figures to be added once a clean run is recorded)

#### Image Previous Runs — bug fixes
- **Missing thumbnails for "both" mode:** `_summarise` was looking for `generation.b64_png` at top level; for "both" mode results it's nested under `cpu.generation` / `gpu.generation`. Fixed.
- **No CPU/GPU label:** mode not included in summary or template. Fixed — now shows CPU / GPU / CPU+GPU.
- **"both" mode showed only one row:** template rendered a single entry regardless of mode. Fixed — "both" runs now render two rows (CPU and GPU) each with their own thumbnail, confidence badge, Wh, and time.
- **Ordering:** `list_results` was sorting by filename (date + UUID), so runs within the same day appeared in arbitrary order. Fixed — now sorts by `saved_at` ISO timestamp, newest first. Date display also upgraded to `YYYY-MM-DD HH:MM` for disambiguation.

### Deferred
- DNS A record + SSL cert (after Easter, pending Wix admin access)
- GPU image generation measurement figures (next clean run)
- Image page elapsed time in progress bar (still outstanding from session 5)

---

## Session 5 — 2026-04-05

### What we built

**Deferred items catchup + Phase 6 prep**

#### LLM — prompt textarea visibility
- Label changed from dim `#555` to `#aaa` with `✎ Edit prompt` text
- Textarea border brightened to `#444` with green left accent (`#00ff9966`)
- Reset button relabelled "Reset to default"

#### LLM — batch result card response text (bug fix)
- `renderLLMBatch` was missing the generated text from the last run
- Added "Response preview (last run)" section: `r.runs[r.runs.length-1].inference.response`

#### LLM — Run All Tasks (T1+T2+T3) feature
- New "Run All Tasks (T1+T2+T3)" button alongside the existing Run Measurement button
- New backend: `run_llm_all_job()` runs T1 → T2 → T3 sequentially, each with cold baseline
- Supports CPU / GPU / Both ⚡ (via the existing device selector)
  - **Both mode** (`mode: "all_both"`): runs all 3 tasks on CPU, then all 3 on GPU
  - Produces a comparison table: T1/T2/T3 rows × CPU tok/s / GPU tok/s / CPU mWh/tok / GPU mWh/tok, green = winner
- New `/llm/run-all` endpoint (POST, accepts `model_key`, `warm`, `device`)
- New JS: `runAllTasks()`, `pollLLMAll()`, `renderLLMAll()`, `renderLLMAllBoth()`
- Progress display shows T1/T2/T3 pips + current device badge + live wall power

#### Previous runs null record fix (bug fix)
- `persist.py _summarise()` only handled `mode: "single"` — returned null for batch/both/all/all_both
- Fixed to handle all LLM modes:
  - `single`: top-level energy/inference (unchanged)
  - `batch`: uses aggregate mean stats
  - `both`: uses GPU side energy/inference
  - `all`: uses T3 as representative, shows "T1+T2+T3" as task label
  - `all_both`: uses GPU T3, shows "T1+T2+T3 · CPU vs GPU"
- `_llm_rows()` also fixed for all modes — CSV export now correct for batch/both/all/all_both

#### Live wall power — generalised across all test pages
- Video page (`pollJob` + `renderProgress`): now fetches `/power` in parallel with job status, displays live W during measurement
- LLM page (`pollLLM`, `pollLLMAll`, `renderProgress`): same
- Image page already had this; video and LLM now match

#### Tapo P110 SessionTimeout fix
- Root cause: browser-side `/power` polling (new, every 3s) ran concurrently with internal 1s measurement polling, overwhelming the P110's single-session limit
- Fix: 3-attempt retry with 1s sleep in all four `get_power_watts()` implementations (`main.py`, `video.py`, `llm.py`, `image_gen.py`)
- Transient session conflicts recover silently within one retry

#### Phase 6 — Public access (GoS1 side complete, pending router + DNS)

**Architecture:**
```
Internet → BouyguesBox (forward 80+443) → nginx on GoS1
  :80  → ACME challenge passthrough + proxy (or redirect to HTTPS once cert live)
  :443 → reverse proxy to WattLab :8000, /settings blocked 403
Nextcloud snap → moved to :8080 (off :80)
```

**GoS1 public IP:** `176.148.88.254`

**Files written:**
- `infra/wattlab.nginx.conf` — nginx vhost config (HTTP + HTTPS blocks, /settings 403, proxy_pass to :8000, ACME challenge dir)
- `infra/setup-nginx.sh` — one-shot setup script (run as sudo)

**`/settings` double-blocked:**
- nginx: `location /settings { return 403; }`
- FastAPI: `_is_local(request)` checks `Host` header — returns 403 if `greeningofstreaming.org` in host, on both GET and POST

**What's already done (GoS1):**
- nginx config written and ready at `infra/wattlab.nginx.conf`
- Setup script ready at `infra/setup-nginx.sh`
- FastAPI `/settings` block implemented and deployed

### Deferred (noted for next session)
- **Image page progress bar:** missing elapsed time (video + LLM pages both show it). Standardise elapsed time + live wall power across all three test pages.
- **GPU image generation:** code is complete and should work (SD-Turbo float16 needs ~2-3 GB VRAM, well within the 11.1 GB available). Just needs a first run to confirm and record the measurement.

---

## Phase 6 — Resumption instructions (for next session)

### Step 1 — Run setup script on GoS1 (needs sudo)
```bash
sudo bash /home/gos/wattlab/infra/setup-nginx.sh
```
This:
1. Moves Nextcloud snap from :80 → :8080
2. Installs nginx + certbot + python3-certbot-nginx
3. Deploys nginx config to `/etc/nginx/sites-available/wattlab`
4. Creates symlink in sites-enabled, removes default site
5. Tests config (`nginx -t`) and starts nginx

After this: Nextcloud accessible at `http://192.168.1.62:8080/` on LAN.

### Step 2 — BouyguesBox port forwarding (do from home)
Admin panel → port forwarding → add:
- TCP **80** → `192.168.1.62:80`
- TCP **443** → `192.168.1.62:443`

**Pre-existing rules to clean up first:** Two old rules were found pointing to `192.168.1.1` (the router itself — initially misread as `192.161.1.1`): one called "apache" forwarding port 80, and one called "ssh" forwarding port 22. Both were left over from the owner's son's personal projects and no longer needed. The port 80 conflict would have silently broken nginx, so delete both before adding the new rules. The active SSH rule (port 2222 → GoS1) is separate and stays.

After this: WattLab reachable at `http://176.148.88.254/` (IP, no DNS needed to test).

### Step 3 — DNS record (wherever greeningofstreaming.org is managed)
```
wattlab.greeningofstreaming.org  A  176.148.88.254
```
TTL: 300 or lower to propagate fast. May take up to 24-48h.

### Step 4 — Issue SSL cert (once DNS has propagated)
Verify DNS first:
```bash
dig wattlab.greeningofstreaming.org A +short
# should return 176.148.88.254
```
Then issue cert:
```bash
sudo certbot --nginx -d wattlab.greeningofstreaming.org
```
Then enable HTTPS redirect — edit `/etc/nginx/sites-available/wattlab`, uncomment the `return 301` line and comment out the proxy block in the HTTP server block, then:
```bash
sudo nginx -t && sudo systemctl reload nginx
```

### Step 5 — Update CLAUDE.md
Add `wattlab.greeningofstreaming.org` to Current URLs section, mark Phase 6 complete.

---

## Session 1 — 2026-04-03/04

### What we built
1. **Live power display** — P110 via local API, auto-refresh 10s, systemd service
2. **Video transcode test** — CPU vs GPU H.264 comparison, P110 + thermals, side-by-side report, server-reported progress stages, Meridian 4K pre-loaded
3. **LLM inference test** — Ollama, TinyLlama + Mistral 7B, fixed prompts, cold inference protocol, energy per token
4. **Focus mode** — 8 background timers suppressed during measurement
5. **Infrastructure** — Git/GitHub, SSH keys, Nighthawk AP mode, Claude Code on GoS1

### Key Video Findings

**H.264 1080p from 4K source (Meridian, 4 runs) — 🟢 Repeatable**

| | CPU (libx264) | GPU (h264_vaapi) |
|---|---|---|
| Duration (mean) | 174.3s 🏁 | 114.0s |
| Energy (mean) | 4.06 Wh ✓ | 4.42 Wh |
| Peak delta | ~85W | ~139W |
| Variance | 7.3% (3.4% ex. outlier) | 0.2% |

GPU 34.5% faster, 9.7% more energy. CPU wins on energy efficiency.

Crossover exists: GPU wins on short clips (<10s transcode), CPU wins on long. The crossover point is between 10-60s transcode duration for this workload.

**Methodology note:** CPU baseline drifts 51-58W between runs (OS thermal state). GPU baseline stable (~54W). Focus mode and 60s cooldown reduce but don't eliminate CPU variance.

### Key LLM Findings

**Cold inference (model unloaded before each run)**

| Model | Task | Tok/s | mWh/token | Confidence |
|---|---|---|---|---|
| Mistral 7B | T2 Medium | 59.3 | 1.028 | 🟢 |
| Mistral 7B | T3 Long | 47.6 | 0.943 | 🟢 |
| TinyLlama | T3 Long | 209.3 | 0.061 | 🟡 |

TinyLlama ~15x more energy efficient per token than Mistral. TinyLlama too fast (1-4s) for reliable P110 measurement — batching needed.

**Warm vs cold:** A contaminated warm run showed 161W delta vs 219W cold — 26% lower. Cold measurement (first-request cost) is more honest for real-world scenarios.

---

## Session 4 — 2026-04-04

### What we built

**LLM CPU vs GPU comparison**
- Added Backend selector to `/llm` page: CPU / GPU / Both ⚡
- GPU mode: standard Ollama inference (ROCm, default)
- CPU mode: `"options": {"num_gpu": 0}` forces Ollama to use CPU only
- Both mode: CPU pass → cooldown → re-baseline → GPU pass → side-by-side result card with winner highlighting
- New `run_llm_both_measurement()` in `llm.py`, new `_analyse_llm()` for comparison
- New `renderLLMBoth()` JS function: speed winner (green) vs loser (grey), energy winner highlighted

**Image generation CPU vs GPU**
- `HSA_OVERRIDE_GFX_VERSION=11.0.0` set at module level and in systemd service — required for RX 7800 XT (gfx1101) with PyTorch ROCm 2.5.1
- GPU strategy: batch of 5 images × 20 steps (~10s total) → `wh_per_image = total_energy / 5`
  (GPU generates in ~2s/image — too fast for reliable P110 measurement at 1s polling interval)
- CPU strategy unchanged: 8 steps, ~12s per image, single image
- Added `device` param to `generate_image()`, `run_image_measurement()`, new `run_image_both_measurement()`
- Added `_analyse_image()` for comparison: energy_winner, speed_winner, speed/energy diff %
- Added CPU / GPU / Both radio selector to `/image` page
- New `renderImageBoth()` JS: side-by-side CPU/GPU cards, winner badge, batch note
- Both pages now fully symmetric: same UI pattern as LLM page

### Key GPU Image Finding (first run — to be measured)
- GPU JIT compilation: ~74s one-time cost on first PyTorch ROCm call (kernels cached to disk)
- Expected GPU: ~2s/image × 5 batch = ~10s measurement window, ≥10 P110 polls
- Expected CPU: ~12s/image, ≥10 P110 polls

### Architecture notes
- `IMAGE_STEPS_CPU = 8`, `IMAGE_STEPS_GPU = 20`, `GPU_BATCH_SIZE = 5`
- `_run_single_image()`: shared internal helper for both-mode passes
- `_calc_energy()`: shared energy calculation, handles batch normalisation
- Energy measurement uses `gen_s` (generation only), not `total_s` (excludes model load)

---

## Session 3 — 2026-04-04

### What we built (Phase 4 + Phase 5)

**Phase 4 — Demo Mode**
- `/demo` guided 4-step journey (Video → LLM → Summary → Findings)
- GoS visual identity on every page: logo, `#00ff99` accent, monospace data / system-ui narrative
- Inline methodology explanations, anti-slideware proof points
- "Previous run" instant-result option in demo flow

**Phase 5 — Image Generation**
- Upgraded Ollama 0.18.3 → 0.20.2 (native image generation support)
- Pulled `x/z-image-turbo` (12GB, 10.3B FP8) and `x/flux2-klein` (5.7GB, MLX)
- **VRAM constraint:** z-image-turbo requires 11.9 GiB; RX 7800 XT has 12 GiB total but only 11.1 GiB available after driver + Ollama overhead — 800 MB short. flux2-klein uses MLX runner which requires CUDA (AMD incompatible). Both blocked on GPU.
- **Solution:** CPU diffusion via Python `diffusers` + `stabilityai/sd-turbo`, 8 inference steps, 512×512
- **Measurement result:** 0.2063 Wh/image, ~12s generation, 🟢 Repeatable
- New module `image_gen.py` with same measurement protocol as video/LLM (P110 polling, baseline, focus mode, thermals, confidence)
- New `/image` page: prompt input, random colour/mood modifier appended per run (anti-slideware proof), live wall power during generation, result card with generated image + energy metrics
- Results saved to `results/image/` with base64 PNG embedded in JSON
- Previous runs browser with 80×80 thumbnail previews
- New module `persist.py` for flat-file result persistence (all types), `settings.py` for configurable parameters

### Key Image Generation Finding

**SD-Turbo CPU, 8 steps, 512×512 (first run) — 🟢 Repeatable**

| Metric | Value |
|---|---|
| Energy / image | 0.2063 Wh |
| Generation time | 12.15s |
| Delta above idle | ~30W |
| Backend | CPU (Ryzen 9 7900, 24 cores) |
| Model | stabilityai/sd-turbo |

GPU image generation deferred: z-image-turbo needs 11.9 GiB VRAM, card has 12 GiB but only 11.1 GiB available after overhead. GPU measurement possible if overhead reduced or larger card added.

### Bugs fixed this session
- `/power` endpoint had `{{...}}` double-brace escaping (leftover from f-string edit) → `TypeError: unhashable type: 'dict'` crashing JS poll loop
- Image page used `r["date"]` (doesn't exist) and `r["data"]` (doesn't exist) from `list_results` summaries — fixed to use `r["saved_at"]` and direct summary fields; added `"image"` branch to `persist._summarise`
- Image JS polled `/job/{id}` — endpoint doesn't exist; correct path is `/image/job/{id}` — added endpoint and fixed JS

### Also built this session
- **FIFO queue system:** central `pending_queue` + `queue_event` + `queue_worker` coroutine (startup task). All three test endpoints enqueue instead of returning 409. Job status includes `queue_position`. Each test page shows "⏱ Queued — position N" while waiting, auto-transitions when slot opens. `/queue-status` HTML page (auto-refresh 4s) shows running job + queue depth. `/queue` JSON endpoint for programmatic access.
- **Fixes to prior gaps:** image_gen.py now calls `focus_mode_enter/exit`; image results export (JSON + CSV) enabled; image step added to `/demo` as step 3 (before summary); CLAUDE.md roadmap checkboxes corrected; `queue-status` link added to home nav.

### Deferred (carried forward)
- GPU image generation (needs VRAM headroom or larger card)
- LLM result text display in result card (last batch iteration)
- All-tasks batch launch (T1+T2+T3 in one click)
- Prompt textarea visibility improvement
- UI polish / visual design pass (flagged for next session)

---

## Session 2 — 2026-04-04

### What we built (Phases 1–3)
- **Phase 1 — Research Integrity:** JSON result persistence (`results/video/`, `results/llm/`), CSV + JSON export endpoints, previous-runs browser (last 10 per type, inline on each page)
- **Phase 2 — Measurement Quality:** LLM streaming inference (token-by-token via Ollama stream API), warm/cold toggle, editable prompts with reset-to-default, batch mode (1×/3×/5×, 10s rest, aggregate + stddev), H.265 CPU/GPU + AV1 CPU video presets
- **Phase 3 — Settings:** `/settings` page with 8 configurable parameters (baseline polls, video cooldown, LLM rest, unload settle, confidence thresholds), `settings.json` persistence

### Bug noted mid-session
LLM response was truncated at 500 chars (leftover from original non-streaming `run_inference`). Fixed: `run_inference_streaming` now stores full response; response box height raised to 500px with `white-space:pre-wrap`.

### Deferred change requests
**Prompt visibility:** The editable prompt textarea was not obvious to first-time users — it only appeared after the service was restarted, and its styling (dark background, subtle border) may make it easy to miss. Consider making the prompt section more prominent or adding a visual label like "Edit prompt ↓" to draw attention to it.

**LLM result text display:** The generated text from inference should be displayed in the result — at minimum the last iteration in batch mode. Currently the response preview may not be prominent enough or may not always render.

**All-tasks batch launch:** Add a single "Run all tasks" button that fires T1 + T2 + T3 sequentially for the selected model, producing a combined report. Useful for a complete per-model benchmark in one click.

---

## Product Planning — 2026-04-04

### Two-mode architecture agreed

**Lab mode** (Simon, Tania, Dom, internal): full controls, editable prompts, export, settings, SSH tunnel access.

**Demo mode** (partners, CTOs, public): guided journey, curated, no settings exposed, proof-of-reality mechanisms, GoS visual identity.

### Data persistence decision
**Flat JSON files, not SQLite.** Each completed job writes `results/{type}/{date}_{job_id}.json`. Survives restarts, directly loadable by pandas/clean_measures.py, no schema migrations, stays agile. SQLite deferred indefinitely.

### Prioritised roadmap

**Phase 1 — Research Integrity** (next session)
- JSON result persistence
- CSV + JSON export
- Previous results browser (last 10 runs, expandable inline)

**Phase 2 — Measurement Quality**
- LLM batched mode: load once, rest, run N times, measure aggregate
- LLM warm vs cold toggle
- LLM editable prompts + streaming word-by-word output display
- Video H.265/HEVC + AV1 presets

**Phase 3 — Settings & Lab Config**
- `/settings` page (lab only)
- Configurable: baseline duration, cooldown, repeats, rest time, confidence thresholds
- `settings.json` persistence

**Phase 4 — Demo Mode**
- `/demo` guided journey with Next flow
- GoS visual identity (logo on every page, link to greeningofstreaming.org)
- Inline methodology explanations, "more info" expanders
- "See previous run" instant result option
- Anti-slideware proof points: streaming LLM output, varied image prompts per run

**Phase 5 — Image Generation**
- Diffusion model via Ollama or ComfyUI
- Energy per image metric
- Live display as generated, varied prompt per run

**Phase 6 — Public Access**
- nginx + Let's Encrypt
- `wattlab.greeningofstreaming.org` domain (preferred)
- `/settings` blocked from public URL

### Open questions
- Confirm domain with GoS — `wattlab.greeningofstreaming.org` or `gos1.duckdns.org`?
- Which image generation runtime to install?
- Should video crossover point (GPU vs CPU efficiency) be a published GoS finding?
- Results directory — add to `.gitignore` or commit selected runs to repo as reference data?

---

## What's Running (end of Session 1)

| Service | URL | Notes |
|---|---|---|
| Live power + nav | `http://192.168.1.62:8000` | LAN / SSH tunnel |
| Video test | `http://192.168.1.62:8000/video` | CPU/GPU/Both + Meridian 4K |
| LLM test | `http://192.168.1.62:8000/llm` | TinyLlama + Mistral, 3 tasks |
| Ollama | `localhost:11434` | systemd, ROCm GPU |
| Remote tunnel | `ssh -p 2222 -L 8000:localhost:8000 user@gos1.duckdns.org` | Simon, Tania, Dom |
