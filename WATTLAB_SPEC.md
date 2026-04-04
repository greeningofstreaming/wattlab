# WattLab — Product Specification & Roadmap
# Version 0.2 — 2026-04-04

---

## Product Vision

WattLab is GoS's live energy measurement platform. It makes the energy cost of real-world content generation and manipulation visible, credible, and reproducible — using primary measurement data, not estimates.

**Not a dashboard. Not a calculator. A lab.**

Two audiences, two modes:

| | Lab Mode | Demo Mode |
|---|---|---|
| Who | Simon, Tania, Dom, internal researchers | CTOs, partners, policymakers, public |
| Access | SSH tunnel or LAN | Public URL (gos1.duckdns.org) |
| Interface | Full controls, configurable | Guided journey, curated |
| Parameters | Editable | Fixed (but re-runnable) |
| Export | Yes — CSV, JSON | No |
| Settings | Yes | No |

---

## Visual Identity

**Source:** greeningofstreaming.org
**Logo:** Round bug mark — embed on every page, top-left, links to greeningofstreaming.org
**Logo URL (for now):** `https://static.wixstatic.com/media/b1006e_f5e9aff607cf4133abf7089207dc3cab~mv2.png`
**Tone:** Engineering realism. No eco-moralizing. Professional dark theme.
**Palette (proposed, to confirm with team):**
- Background: `#0a0a0a` (current — keep)
- Accent: `#00ff99` (current — keep, it works)
- Text: `#e0e0e0` (current — keep)
- GoS green from website: `#2d6a4f` or similar — use for secondary elements
- Warning/highlight: `#ffaa00` (current — keep)

**Typography:** Monospace for data/metrics (current — keep). Add a clean sans-serif (Inter or system-ui) for explanatory text in demo mode.

---

## Architecture

### URL Structure

```
/                    → Home (power monitor + navigation)
/video               → Video transcode test (lab mode)
/llm                 → LLM inference test (lab mode)
/image               → Image generation test (lab mode) [future]
/settings            → Lab settings (NOT on public URL)
/demo                → Demo mode entry point (public URL)
/demo/video          → Guided video demo
/demo/llm            → Guided LLM demo
/demo/image          → Guided image demo [future]
/results/{job_id}    → Shareable result page
```

### Data Persistence

**Not SQLite** — use flat JSON files for agility.

```
wattlab/results/
├── video/
│   ├── 2026-04-03_a1b2c3d4.json
│   └── 2026-04-04_e5f6g7h8.json
├── llm/
│   └── 2026-04-04_i9j0k1l2.json
└── image/
    └── [future]
```

Each result file contains: timestamp, job_id, parameters, full energy report, raw poll data.
Results survive restarts. No schema migrations needed. Directly loadable by pandas/clean_measures.py.

---

## Phase 1 — Research Integrity
*Prerequisite for using data seriously. Target: next session.*

### 1.1 JSON Result Persistence
Every completed job writes a JSON file to `~/wattlab/results/{type}/`.
- Filename: `{date}_{job_id}.json`
- Content: full job result including all parameters, energy report, raw P110 polls, thermal readings
- On service restart: scan results directory and reload last N jobs into memory for the UI
- Export endpoint: `GET /results/{job_id}/download?format=json|csv`

### 1.2 Result Export
- JSON export: full raw data, machine-readable
- CSV export: flattened key metrics, importable directly into pandas / clean_measures.py
- Available from both lab UI and via direct URL
- CSV schema to match existing toolchain: `timestamp, power_w, delta_w, energy_wh, tokens (if LLM)`

### 1.3 Previous Results Browser
- On video and LLM pages: "Previous runs" collapsible section below the run form
- Shows last 10 results for that test type: date, model/preset, key metric, confidence flag
- Click any row → expand full report inline (no page reload)
- This serves both lab (to compare runs) and demo (to show previous results instantly)

---

## Phase 2 — Measurement Quality
*Make the data trustworthy. Target: session 2.*

### 2.1 LLM Measurement — Batched Mode
Current problem: TinyLlama (1-4s inference) is below P110 minimum measurable duration.

**New protocol:**
1. Load model once (keep_alive = 10 min during batch)
2. Rest for configurable time (default 30s) — let GPU settle
3. Run inference N times (default 3 for short tasks, 1 for long)
4. Measure energy across entire batch
5. Report: total energy / N = energy per run; total tokens / N = tokens per run
6. mWh/token calculated from batch averages

**Warm vs Cold toggle:**
- Cold (default): model unloaded before batch, measures first-request cost
- Warm: model pre-loaded, rest 30s, then batch — measures steady-state cost
- Both reported separately, clearly labelled

### 2.2 LLM — Editable Prompts
- Three prompt fields (T1/T2/T3) editable in the UI
- "Reset to default" button per prompt
- Prompt used is saved in the result JSON (so results are always reproducible)
- Character limit: 500 chars with counter

### 2.3 LLM — Live Output Display
- Streaming output from Ollama (use `stream: true`)
- Response appears word-by-word in a scrollable box during inference
- Token counter increments in real time
- This is the primary "proof it's real" mechanism for demo mode

### 2.4 Video — H.265/HEVC and AV1 Presets
Add to existing CPU/GPU comparison:

| Preset | Encoder | Notes |
|---|---|---|
| H.264 CPU | libx264 CRF 23 | Current |
| H.264 GPU | h264_vaapi QP 23 | Current |
| H.265 CPU | libx265 CRF 28 | New |
| H.265 GPU | hevc_vaapi QP 28 | New |
| AV1 CPU | libaom-av1 CRF 35 | New — will be slow |
| AV1 GPU | av1_vaapi QP 35 | New |

Multi-preset "Compare all" mode: runs all selected presets sequentially, presents full comparison table. User selects which presets to include via checkboxes.

---

## Phase 3 — Settings & Lab Configuration
*For power users. Target: session 3.*

### 3.1 Settings Page (`/settings` — lab only, not on public URL)
Sections:
- **Measurement:** baseline duration (default 10s), cooldown between runs (default 60s), poll interval (default 1s)
- **LLM:** default repeat count (default 3), rest between repeats (default 30s), keep_alive duration
- **Video:** upload size limit, temp file cleanup policy
- **Focus mode:** which timers to suppress (checklist)
- **Confidence thresholds:** editable ΔW and poll count thresholds for 🟢/🟡/🔴
- **About:** GoS1 hardware info, P110 IP, sensor paths

Settings stored in `~/wattlab/wattlab_service/settings.json`. Loaded at startup, hot-reloadable.

### 3.2 Navigation — Lab vs Demo
- Lab mode (`/`, `/video`, `/llm`): full controls, settings link visible
- Demo mode (`/demo/*`): no settings, no export, no raw data — clean guided experience
- Nginx config (when added) can block `/settings` from public URL

---

## Phase 4 — Demo Mode
*For partners and policymakers. Target: session 4.*

### 4.1 Demo Journey Structure

```
/demo
  → "Welcome to WattLab" — 2 sentences, what this is
  → [Next] → /demo/video
    → Brief explanation (collapsible "more info")
    → "See previous run" button (loads last result instantly)
    → [Run this test] button (runs live)
    → Results display
    → [Next] → /demo/llm
      → Same structure
      → [Next] → /demo/image [future]
        → [Back to start]
```

### 4.2 Demo Explanation Style
- Max 2 sentences visible by default
- "More info ↓" expands a paragraph with GoS methodology context
- No jargon without definition
- Confidence flags explained inline with plain English ("This result has been repeated enough times to be reliable")

### 4.3 Proof Points (anti-slideware)
- Video: output file size shown and downloadable, transcode duration is real elapsed time
- LLM: streaming output visible word-by-word, prompt is editable even in demo (but reset to default on next visitor)
- Image: each generated image displayed as it completes, prompt varies per run (colour, detail) so successive images are visibly different

---

## Phase 5 — Image Generation
*Deferred until diffusion model installed. Target: session 5.*

### 5.1 Runtime
- Ollama image model (if available) or ComfyUI
- Fixed resolution: 512×512 for speed, 1024×1024 for quality comparison

### 5.2 Fixed Prompts (varied per run)
```
Base: "A data center server room with cooling infrastructure, photorealistic"
Variation: add colour modifier per run ("with blue lighting", "at night", "in warm tones")
```

### 5.3 Metrics
- Energy per image (Wh)
- Time per image (s)
- Resolution comparison (512 vs 1024)
- Step count comparison (20 vs 50 steps)

---

## Phase 6 — Public Access
*Only after Phase 3 is solid. Target: session 6.*

- nginx reverse proxy on GoS1
- Let's Encrypt SSL via certbot
- Port forward 80/443 on BouyguesBox
- nginx config: block `/settings` from public, rate-limit `/video/upload`
- Domain: `wattlab.greeningofstreaming.org` (preferred) or `gos1.duckdns.org`

---

## Scope Statement (always include in all reports)
"Device layer only (GoS1 server). Network, CDN, and CPE excluded from all measurements."
For LLM: add "No amortised training cost included."

## Traffic Light Confidence (always apply)
- 🟢 Repeatable: ΔW > 5W, ≥10 polls, low variance across runs
- 🟡 Early insight: ΔW ≥ 2W or ≥5 polls
- 🔴 Need more data: ΔW < 2W or <5 polls (near P110 noise floor ~1W)
