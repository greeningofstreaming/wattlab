# WattLab — Project Journal

## About
WattLab is GoS's live energy measurement platform. It makes the energy cost of real-world content generation and manipulation visible, credible, and reproducible — using primary measurement data, not estimates. Not a dashboard. Not a calculator. A lab.

Scope: device layer only (GoS1). Network, CDN, and CPE explicitly excluded.

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
