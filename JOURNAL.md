# WattLab — Project Journal

## About
WattLab is GoS's live energy measurement platform. It makes the energy cost of real-world content generation and manipulation visible, credible, and reproducible — using primary measurement data, not estimates. Not a dashboard. Not a calculator. A lab.

Scope: device layer only (GoS1). Network, CDN, and CPE explicitly excluded.

---

## Session 15 — 2026-04-29

### What we did

**Readability + visual consistency pass · RAG bug fixes · RAG polish · Corpus doc browser · Demo prep for 2026-04-30**

#### `_BASE_STYLES` — single source of truth for content colours
The site shipped with 396 inline `color:#xxx` declarations across `main.py`, dominated by `#555` (used 112×, ~3.3:1 contrast on `#0a0a0a` — fails WCAG AA). User feedback flagged grey-on-black as hard to read on mobile. Fixed in two passes:

1. Added `_BASE_STYLES` constant (`main.py:~276`) — a single `<style>` block defining `:root` CSS variables for all content colours (`--text` / `--text-2..5`), accents (`--accent` / `--warn` / `--err`), backgrounds (`--bg` / `--panel`), and borders. Plus base body sizing and a `@media (max-width:600px)` block that bumps the smallest sub-label fonts on phones. Injected via `_FOOTER` (covers all standard pages) and directly into the `/gate` page.
2. Bulk migration via Python regex script: 620 mechanical replacements of `color:#xxx`, `background:#xxx`, `border-color:#xxx`, and `1px solid #xxx` literals → `var(--*)`. Worst offender `#555` → `--text-3` = `#8a8a8a` (~6.6:1, AA). Alpha-channel variants like `#00ff99XX` intentionally left as literals — they're translucent overlays semantically distinct from `--accent`.

Future readability tweaks are now one place: edit `--text-3` / `--text-4` / `--text-5` and the whole site shifts together.

#### Visual consistency — owl logo + Guided Tour findings
- Replaced the inline `← Home` link on `/queue-status` with the shared `_BACK` snippet (now uses `""" + _BACK + """` concat pattern, matching how `_FOOTER` is wired in the same page).
- Added the owl SVG before the existing GoS logo in the `/methodology` topbar — project mark + org credit visible together. (User flagged that the methodology topbar layout still differs from other pages — recorded as a deferred "factorise headers/footers" item; the symptom is left-justified logo + title + bespoke `.topbar` div predates the `_BACK`/`_FOOTER` consolidation.)
- Refactored `buildSummary` (`main.py:~4223`) on the Guided Tour final step: video transcoding now leads as the headline (`<h2>` + scope sentence + flat table), with LLM / Image / RAG demoted to collapsible `<details>` blocks under an "OTHER WORKLOADS MEASURED" subhead. Reflects the GoS thesis that video is the streaming-impact story; AI workloads are interesting but secondary.

#### RAG Compare 3 Modes — bug fixes
User reported `-0.0133 mWh/tok` for TinyLlama on rag_large in a Compare 3 Modes run. Investigation surfaced three bugs in `run_rag_compare_job` (`main.py:~2846`):

1. **No cooldown between modes.** Loop ran `run_rag_measurement()` back-to-back. With TinyLlama's sub-2s inference, the rag_large baseline was contaminated by residual heat from the rag run (w_base inflated from 53 W cold → 64 W after rag), making `delta_w` go negative. Fixed: added `await asyncio.sleep(s["llm_rest_s"])` between iterations (skipped after the last). Reused existing `llm_rest_s` setting (default 10s) — no new settings field.
2. **Stage-name collision.** Outer loop set `jobs[job_id]["stage"] = rag_mode` ("baseline"/"rag"/"rag_large"), then the inner `run_rag_measurement` immediately overwrote with its own "baseline"/"inference"/"done" stages. The JS `RAG_STAGE_IDX` map only knew the inner three, so it silently fell back to index 0 for "rag"/"rag_large" — the progress bar appeared to reset between modes. Fixed: outer loop no longer touches `stage`, only `current_mode` and a new `mode_index`.
3. **Stage list undersized for the 9-phase flow.** Deferred — current 3-stage display works for single mode, and compare-mode renderer (`renderCompareProgress`) shows mode-level progress with the new "⏱ Cooling down" row, which is sufficient.

#### RAG polish for tomorrow's demo
- Renamed "Baseline" → "Without RAG" in the RAG section UI only (display labels: mode card, single-mode `ragModeLabels`, compare-mode `MODE_LABELS` ×2, Guided Tour `buildSummary` ragRows). Internal `baseline` mode key kept — renaming would break stored result files and CSV schemas.
- Pre-populated `/rag` question textarea with **"What is REM (Remote Energy Measurement)?"**. Corpus-grounded (the GoS REM whitepaper is in the index), and surfaces a strong demo finding: all three model sizes retrieved the same correct chunks for this question, but TinyLlama hallucinated "REM is a framework provided by the European Commission" (blending the GoS source with an adjacent JRC sustainability framework chunk), while Gemma 3 12B and Phi-4 14B stayed faithful. Headline insight: **RAG retrieval ≠ RAG quality. Hallucination is a third axis on the energy/quality tradeoff.**
- Added an inline `<details>` callout under the question textarea explaining (1) why this question, (2) what visitors will see, and (3) the headline insight. Visible inline rather than buried in methodology.
- Replaced the previous "What year did your training data end?" demo question — it was a meta-question about the model's own state, but RAG floods context with dated corpus documents, causing models to conflate "this PDF is dated 2025" with "my training cutoff is 2025". Misleading demo of RAG capability.
- Dropped the hardcoded `(10s)` from the RAG progress bar label — actual baseline duration honours `s["baseline_polls"]` (now 7s in user's settings). Label is now just `'Baseline poll'` so it can never go stale regardless of settings.

#### LLM CSV — response column
JSON files already include `inference.response` (full LLM output) but CSV export at `persist.py:71-76` excluded it. Added `response` to fieldnames and to the `_row` helper inside `_llm_rows`. CSV-quoting handled by `csv.DictWriter` defaults — newlines preserved inside quoted fields. Applies to single, batch, both, all, all_both, rag, and rag_compare modes.

For `mode: rag_compare`, also confirmed structure: there's no top-level `inference.response` (no top-level `inference` dict at all) — each mode result lives under `results.<mode>.inference.response` (and also `results.<mode>.answer`). Both contain the full LLM output. No normalisation needed.

#### RAG corpus document browser
New `corpus_list()` in `rag.py` returns `[{name, rel_path, size_kb, indexed}]` — cross-referenced against the ChromaDB collection's source-filename metadata to show indexed vs pending. New `GET /rag/corpus-list` endpoint wraps it with totals. Collapsed `<details>` panel on `/rag` (between index bar and Model section); on first open it sorts pending first, renders a scrollable list with green ● / amber ○ status dots, per-doc size and tag, and a footer note explaining how to add docs.

Demo angle this unlocks: "the GoS REM whitepaper is right here in the index, alongside 92 other PDFs — anyone can drop a paper in and rebuild." Concrete, visible, anti-slideware.

#### Stale-box audit
Tidied three Phase 6 boxes (`CLAUDE.md:173-175`) that were marked `[ ]` despite being completed in session 13: DNS A record, Let's Encrypt SSL, HTTP→HTTPS redirect. Same fix in memory: removed `project_phase6.md` and `project_deferred.md` (the latter pointed to "image elapsed time" which had been silently fixed before being ticked).

#### Testing strategy — `TESTING.md`
Wrote a three-tier testing strategy doc as the project's first quality plan. Sweet-spot principle: tests get *run* (not avoided), so we deliberately keep the bar low. Tier 1 is a 30-second bash smoke (imports + page 200s + JSON shapes + two pure-function checks); Tier 2 is a 2–5 min integration check (persistence + CSV round-trip, RAG `corpus_list` metadata sync, no full RAG rebuild); Tier 3 is a 5 min manual UI checklist with concrete click-paths for video / LLM / RAG / Guided Tour. Includes a decision matrix ("typo → Tier 1 only; pre-demo → all three") and an explicit "what we're NOT testing and why" section so the bar stays sustainable. Bash skeletons for `scripts/smoke.sh` and `scripts/integration.sh` are embedded inline — implementation deferred until first time we feel friction.

### Files touched
- `wattlab_service/main.py` — `_BASE_STYLES` constant + injection via `_FOOTER` and `/gate`; bulk hex → `var(--*)` migration; `_BACK` swap on `/queue-status`; owl logo on `/methodology` topbar; Guided Tour findings refactor (per-section row strings + collapsible AI workloads); RAG `/rag` page polish (Without RAG labels, REM question pre-fill, faithfulness `<details>` callout, dropped `(10s)`); `run_rag_compare_job` cooldown + stage-collision fix; `MODE_LABELS` cooldown row; corpus browser `<details>` panel + `loadCorpus()` JS; `GET /rag/corpus-list` endpoint
- `wattlab_service/persist.py` — `response` added to LLM CSV fieldnames + `_row` helper
- `wattlab_service/rag.py` — `corpus_list()` function (cross-references ChromaDB metadata for indexed status)
- `TESTING.md` — new file, three-tier testing strategy with bash skeletons
- `CLAUDE.md` — Session 15 entry; 8 Deferred items ticked; 4 new Deferred items added with `[LOW]`/`[MID]` priority tags on the two RAG follow-ups; stale Phase 6 trio cleared; `TESTING.md` added to See also + Repo Structure
- `JOURNAL.md` — this entry
- Memory (`~/.claude/projects/-home-gos-wattlab/memory/`) — removed `project_deferred.md` and `project_phase6.md` (stale)

### Open items coming out of this session
- Restart `wattlab` systemd service after pulling these changes (sudo required)
- Demo on 2026-04-30 — pre-warm Video Compare All Codecs (3 min) and RAG Compare 3 Modes with Phi-4 (~80s) before going live, so Previous Runs are populated as backup
- Watch the new readable-on-mobile breakpoint after demo — `@media (max-width:600px)` block bumps base + sub-label fonts; if it overshoots on tablets we can narrow the breakpoint
- After demo: see Deferred section for sized-and-prioritised follow-ups (RAG visitor upload `[MID]`, individual PDF view `[LOW]`, Findings step redesign, header factorisation)

---

## Session 14 — 2026-04-24

### What we did

**Larger LLM tier · SDXL-Turbo + Compare Models · VRAM leak fix · Progressive-disclosure UX pilot · Methodology refresh**

#### LLM tiers — adding a "large" option
Previous tiers: TinyLlama 1.1B (small), Mistral 7B (mid). Added **Gemma 3 12B** as the large tier via `ollama pull gemma3:12b` (8.1GB Q4_0). Choice rationale: same distillation family story (Google alongside Meta/Mistral), fits cleanly in 12GB VRAM without offload, knowledge cutoff ~Aug 2024. SDK knowledge cutoff is past the model's actual cutoff — when asked to write a dated report it filled in 2023-10-26 (a notorious pretraining-corpus default), which is an LLM artefact not a WattLab bug.

Also confirmed `phi4:latest` (14B) was already pulled but missing from `llm.py` — it's in `rag.py` already, so added there too. No HTML changes to `/llm` or `/rag`: both pages iterate `MODELS` so new entries auto-render as selector cards.

#### SDXL-Turbo + Compare Models
Added `stabilityai/sdxl-turbo` (~3.5B params) as a second diffusion model. `image_gen.py` gained an `IMAGE_MODELS` registry and `generate_image()` now takes a `model_key` parameter; entry points `run_image_measurement` and `run_image_both_measurement` were extended accordingly.

New `run_image_compare_models_measurement()` runs both models on GPU with same prompt + seed, both at 512×512 and both at their native 4-step operating point (SD-Turbo batch 30, SDXL-Turbo batch 15). Result is rendered side-by-side on `/image` — image quality is subjective (no single metric), energy per image is measured for each.

#### SDXL-Turbo on Navi31 — investigation
Three issues surfaced:

1. **Black images with fp16 VAE.** SDXL's VAE overflows in fp16 on our RX 7800 XT (known issue). Diffusers auto-detects this and upcasts the VAE to fp32 for decode (via deprecated `upcast_vae`). We leave `force_upcast=True`.
2. **VRAM OOM at 1024×1024.** The fp32 VAE decode allocates 4.5 GB for a single conv, exceeding our 12 GB budget when UNet + text encoders are resident. We tried `enable_vae_tiling()` but the default SDXL `tile_latent_min_size=128` uses a strict `>` check, so a 1024-output latent (exactly 128×128) fails to trigger tiling. Forced `tile_latent_min_size=64` triggers tiling but makes decode 100× slower (~115s per image).
3. **Resolution.** Picked 512×512 as the operating point — fp32 VAE decode fits comfortably, no tiling needed. Bonus: 512 is native for SD-Turbo, so Compare Models becomes apples-to-apples at same resolution with model size as the only variable.

#### VRAM leak in `generate_image`
Observed the uvicorn worker holding 9.67 GB of VRAM with no active jobs. Root cause: pipelines created in `generate_image()` weren't being released between calls — Python GC isn't timely, and ROCm's HIP allocator holds cached memory. Added `try/finally` with `del pipe`, `gc.collect()`, `torch.cuda.empty_cache()`. Latent bug, present since the image module was added; now fixed.

#### Compare Models — step parity correction
Initial Compare Models implementation ran SD-Turbo at its solo-mode 20 steps and SDXL-Turbo at 4 steps. Caught mid-conversation: this is not apples-to-apples. SD-Turbo at 20 steps is 5× over-sampled relative to its native 1–4 range (it's set high in solo mode to give P110 enough runtime). In a model comparison, SD-Turbo was being charged for over-sampling that doesn't improve its distilled output.

Fix: added `compare_steps`/`compare_batch` fields to `IMAGE_MODELS`, and `generate_image()` accepts optional `steps_override`/`batch_override`. Compare Models now runs both at 4 steps; SD-Turbo batch 30 to keep wall time ≈ 10s for P110 reliability. Solo mode unchanged for historical continuity.

#### Progressive-disclosure UX pilot
Question raised: should WattLab have two UI modes (power-user vs visitor)? Considered, rejected. Two full modes means 2× HTML to maintain, 2× copy that drifts, and `/demo` (Guided Tour) + `/methodology` already serve the visitor audience. Instead:

- Replaced verbose `.info` blocks on `/image`, `/video`, `/llm`, `/rag` with collapsed `<details>` ("ⓘ About this test") — default collapsed for lab use, expands for first-time visitors.
- Added a subtle "First time here? Try the Guided Tour →" link near the top of each test page.
- Subtitle (one-line hardware/scope summary) stays always visible.
- All control rows (model picker, preset cards, prompt editor, buttons) untouched — lab workflow unaffected.

Worth revisiting if visitor feedback says the collapsed default is too hidden.

#### Methodology page refresh
`/methodology` bumped to version 0.2 (last updated 2026-04-24):
- **Video section** rewritten for ABR rate control + full VAAPI pipeline + `all_codecs` preset.
- **Image section** rewritten for SD-Turbo + SDXL-Turbo + Compare Models, with step-count rationale.
- **LLM section** updated to list the three size tiers (TinyLlama / Mistral / Gemma 3).
- **Hardware table** updated for new codecs, models.
- **Removed** stale "CPU thermal cross-talk" limitation (resolved by full GPU pipeline in session 12).
- **Removed** stale "GPU energy crossover" open question (superseded by session 13 ABR findings — GPU is now 43–81% less energy across codecs).
- **Rewrote** "Transcoding quality equivalence" open question to reflect ABR progress (bitrate now controlled; GOP/profile still TBD).
- Added matching `← Home` links top and bottom of content (`.home-link` class — same style as the `_BACK` link on other pages).

#### Live telemetry — modular refactor
Demo feedback surfaced a request: CPU + GPU temperatures alongside the P110 wattage, updated live. Done in a way that makes adding future live metrics a one-line change.

- `power.py` gained `read_sensors_dict()` — single subprocess read of `sensors -j`, returns `{cpu_tctl, gpu_junction, gpu_ppt_w}`.
- `main.py` extended `_power_cache` with the sensor keys + queue depth. Two background tasks populate it: `power_poller` at 5s (P110 rate-limit) and `sensors_poller` at 2s (subprocess is cheap, temp changes matter during workloads).
- New `/live` endpoint bundles everything into one JSON fetch. `/power` kept for backwards compatibility — multiple pages still read it during active measurements.
- `_LIVE_JS` shared JS block polls `/live` every 3s and updates any DOM element carrying `data-live="<key>"`. Formatters live in a single `FMT` table: adding a new metric is one cache key + one FMT entry.
- Floating badge (bottom-right, every page) now shows `watts · CPU °C · GPU °C · queue depth`. Home page gets a 3-cell row under the big watts display: CPU Tctl / GPU junction / GPU PPT. Both use the same declarative `data-live` hooks — no bespoke JS per page.

#### "Report an issue" link
- `_FOOTER` gained a subtle "Spotted a bug or have a feature request? Open an issue on GitHub →" line — visible on every page that includes the footer.
- `/methodology` gained a visible "Source on GitHub" + "Report an issue" pair of links near the top of the content, in addition to the existing footer mention. GitHub repo is the canonical feedback channel.

#### Queue pause flag for external tools
A companion experiment at `/home/gos/claude-local-router/` runs Ollama-backed local models (`qwen2.5-coder:14b` now, was `gemma3:12b` originally) that compete with WattLab's SDXL-Turbo image jobs for the 12 GB VRAM. Per the spec at `OWL_INTEGRATION_PROPOSAL.md` in that repo: a coarse file-flag `/tmp/owl-paused` gates the queue worker between jobs without killing the service.

- `queue_worker` checks `Path(PAUSE_FLAG).exists()` before each `pop(0)`. In-flight jobs are untouched — only between-jobs transitions are gated. `queue_event` semantics unchanged so enqueues-during-pause wake the worker correctly when the flag clears.
- `/queue` JSON grew a `"paused"` key; `/queue-status` renders an amber banner when paused.
- Mitigation for the "forgotten flag → silent wedge" failure mode: `/live` also surfaces `paused`, and `_LIVE_JS`'s FMT table renders a `⏸ paused` pill in the floating badge on *every* page. So a user who runs a video job while paused sees the reason immediately without having to navigate to `/queue-status`.
- Router side already handles flag lifecycle (`touch` on launch, `trap EXIT rm -f` on exit).

#### WattLab owl logo
Commissioned a project mark (geometric owl, teal/green palette adjacent to the GoS `#00ff99` accent but not identical — the org mark and project mark coexist rather than compete).

- SVG at `wattlab_service/static/owl.svg` (2.4 KB).
- FastAPI `StaticFiles` mount at `/static`; gate middleware whitelists `/static/*` so the favicon loads on the `/gate` login page before the user has authenticated.
- Favicon swapped on all 10 pages from the Wix-hosted GoS PNG to the local owl SVG. Browser tabs now read as WattLab.
- `_BACK` upgraded from a bare "← Home" text link to `[owl] WattLab ← Home` — one change, propagates to every test / settings / methodology page.
- Home page gets a 72 px owl + wordmark block above the big live watts display.
- Footer `_LOGO` retains the GoS mark. Org credit stays on every page.

### Files touched
- `wattlab_service/power.py` — `read_sensors_dict()` helper
- `wattlab_service/llm.py` — Gemma 3 12B added to `MODELS`
- `wattlab_service/rag.py` — Gemma 3 12B added to `MODELS`; pre-existing Phi-4 entry kept
- `wattlab_service/image_gen.py` — `IMAGE_MODELS` registry; `model_key`, `steps_override`, `batch_override` params; `run_image_compare_models_measurement`; `_analyse_models`; VRAM cleanup in `finally`
- `wattlab_service/main.py` — `/image/start` accepts `model_key` + `compare_models` device; model picker + Compare Models button + `renderCompareModels()` on `/image`; progressive-disclosure collapsibles on `/image`, `/video`, `/llm`, `/rag`; methodology rewrite + home links; `/live` endpoint + `sensors_poller`; `_LIVE_JS` shared poller; badge + home page live hooks; `_ISSUES_LINK` in `_FOOTER`; methodology GitHub links; `PAUSE_FLAG` queue gate + banner + live pill; `StaticFiles` mount + owl favicon + `_BACK` wordmark + home hero
- `wattlab_service/static/owl.svg` — new project mark (2.4 KB)
- `wattlab_service/persist.py` — `compare_models` branch in `_summarise`
- `CLAUDE.md` — session 14 entry, deferred list tidied
- `JOURNAL.md` — this entry

### Open items coming out of this session
- Restart `wattlab` systemd service to pick up changes (sudo required — can't do from this agent)
- Once restarted, validate Compare Models end-to-end with a real measurement (can't test from the agent's process because the running uvicorn worker still holds leaked VRAM from pre-fix state)
- Watch whether the progressive-disclosure default is too hidden for visitors — if so, revisit with a visible density toggle or make the "ⓘ About this test" open by default on first visit via `localStorage`

---

## Session 13 — 2026-04-10

### What we did

**ABR benchmark · Compare all codecs · HTTPS · CSV/output fixes · Deferred roadmap tidy**

#### ABR rate control — methodology fix
All six video presets (H.264/H.265/AV1 × CPU/GPU) previously used different rate-control modes: CRF for software encoders, QP for hardware. These are not equivalent — CRF is adaptive (targets quality), QP is fixed (targets quantisation). Output file sizes differed, meaning CPU and GPU were not being given the same task.

Fixed by switching all presets to ABR (`-b:v Nk`) with a shared bitrate target per codec:
- H.264: 4000 kbps · H.265: 2000 kbps · AV1: 1500 kbps

Targets are stored in settings (`h264_bitrate_kbps`, `h265_bitrate_kbps`, `av1_bitrate_kbps`) and editable in the Settings page. CPU and GPU now produce near-identical output file sizes, displayed in results as confirmation.

PRESETS refactored: `cmd(i,o)` → `cmd_fn(i,o,bps)` + `detail_fn(bps)` + `bitrate_key`. Helper `_preset_bps(preset_key, s)` resolves the correct setting at runtime.

#### Compare all codecs
New `all_codecs` preset mode runs all six presets sequentially in three codec pairs (H.264 CPU→GPU, H.265 CPU→GPU, AV1 CPU→GPU) with cooldown between each pair.

Backend:
- `run_all_measurement()` in `video.py`: queues 3 pairs, collects per-codec results
- `analyse_all(codecs)`: cross-codec summary — most energy-efficient preset, fastest preset, per-codec winner
- `persist.py`: `_summarise` and `_video_rows` updated for `all_codecs` mode
- Stage map: 12-stage `_ALL_STAGES` + `_ALL_MAP` wired into STAGES/STAGE_MAP

UI result card (`renderAllCodecs()`):
- Matrix table: Codec × (CPU time / CPU energy / CPU out / GPU time / GPU energy / GPU out / Conf)
- Output size in separate per-side columns (not a combined column) — confirms bitrate parity
- Highlights: most efficient preset + fastest preset
- Collapsible per-codec detail cards with full thermal breakdown
- Footnote: "CPU out / GPU out should match — confirms same bitrate target"

#### HTTPS
DNS A record for `wattlab.greeningofstreaming.org → 176.148.88.254` restored (was wiped during Wix domain transfer). Certbot provisioned: `sudo certbot --nginx -d wattlab.greeningofstreaming.org` + `sudo systemctl restart nginx`. Service now live at https://wattlab.greeningofstreaming.org.

#### CSV and output size fixes
- `output_size_mb` added to video CSV (`_video_result_row` + fieldnames)
- Full thermals now in CSV: added `cpu_mean`, `gpu_mean`, `gpu_ppt_peak_w` alongside existing `cpu_base/peak`, `gpu_base/peak`, `gpu_ppt_mean_w`
- All-codecs matrix: output size split into separate "CPU out" / "GPU out" columns (previously a single combined column appearing after GPU energy)

#### Results — ABR all-codecs benchmark (3 runs, all 🟢)
Meridian 120s extract (4K → 1080p), ABR targets as above, full GPU pipeline:

| Codec | CPU | GPU | GPU energy saving | GPU speed gain |
|---|---|---|---|---|
| H.264 | 37.3s / 0.83 Wh | 17.5s / 0.37 Wh | ~55% | ~53% |
| H.265 | 70.3s / 1.58 Wh | 14.5s / 0.29 Wh | ~81% | ~79% |
| AV1   | 30.8s / 0.65 Wh | 14.5s / 0.30 Wh | ~55% | ~53% |

Notable observations:
- H.265 and AV1 GPU both encode in exactly 14.5s — VAAPI hardware clock is the ceiling on the GPU path
- AV1 CPU outperforms H.265 CPU on both speed and energy (SVT-AV1 multi-core optimisation)
- Most energy-efficient preset: AV1 GPU and H.265 GPU (~0.29 Wh) — gap within noise, more runs needed
- Results reproduced across 3 runs to within 1%

#### Deferred roadmap updates
- CPU temp under GPU load: **closed** — full pipeline (session 12) resolved this
- DNS + SSL: **closed** — done this session
- Added: Benchmark 2 (representative real-world CRF/QP presets), main.py refactor, Docker containerisation

#### Cron jobs
Two cron jobs added to `/etc/cron.d/`:

**wattlab-tmp-cleanup** — daily at 03:00, removes transcode output files older than 180 minutes from `/tmp/wattlab_uploads/`. The age filter ensures no in-flight or queued job input files are touched. (4.2 GB had accumulated at time of writing.)
```
0 3 * * * gos find /tmp/wattlab_uploads -type f -mmin +180 -delete
```

**wattlab-results-backup** — daily at 03:30, rsyncs `results/` to Nextcloud (`GoS1-backup/wattlab-results/`). Results are gitignored and were previously unbacked — this is the only copy outside GoS1. Logs to `/var/log/wattlab-backup.log`.
```
30 3 * * * gos /usr/bin/rclone sync /home/gos/wattlab/results/ nextcloud:GoS1-backup/wattlab-results/ --log-file=/var/log/wattlab-backup.log 2>&1
```

#### power.py — pluggable power measurement module
`get_power_watts()` was duplicated identically in all five files: `video.py`, `llm.py`, `image_gen.py`, `rag.py`, `main.py` (the comment in llm.py even said "same as video.py"). Extracted into a new `wattlab_service/power.py` module.

All five files now import `from power import get_power_watts`. The `tapo` and `dotenv_values` imports were removed from the four measurement modules entirely; `main.py` retains `dotenv_values` for the gate password.

`power.py` includes an explicit comment marking the swap point for future PDU/IPMI/alternative sources — the only file that needs changing for a DC deployment.

Net result: −89 lines across the codebase.

### Deferred (carried forward)
- Image page elapsed time in progress bar
- GPU image generation: first clean measurement run
- phi4: `ollama pull phi4`
- Confidence multiplier grounding with Tanya (5×/2× thresholds still by judgement)
- Transcoding profile documentation: GOP structure and profile level (bitrate now standardised)
- Benchmark 2: representative real-world presets (CRF/QP, codec-natural rate control)
- main.py refactor (routes/, Jinja templates, typed models, tests)
- Docker containerisation (two-stage plan; see CLAUDE.md)

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
