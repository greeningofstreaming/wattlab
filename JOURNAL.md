# WattLab — Project Journal

## About
WattLab is GoS's live energy measurement platform. It makes the energy cost of real-world content generation and manipulation visible, credible, and reproducible — using primary measurement data, not estimates. Not a dashboard. Not a calculator. A lab.

Scope: device layer only (GoS1). Network, CDN, and CPE explicitly excluded.

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
