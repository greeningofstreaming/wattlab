# WattLab — Claude Code Context File
# Auto-loaded by Claude Code. Keep this current.
# Last updated: 2026-05-01 (Session 16)
# Public name: OWL (Online WattLab). "WattLab" is the legacy/internal/repo name.
# See also: GOS1_INFRA.md — server infrastructure, Nextcloud backup, personal stack context
# See also: TESTING.md — three-tier testing strategy
# See also: CHANGE_REQUESTS.md — open CRs (CR-001 two-tier OWL, CR-001b demo lock, CR-002…CR-009)
# See also: AUDIT_BRIEF.md + AUDIT_RESPONSE.md — pre-CR-001 architecture audit + recommendations
# See also: JOURNAL.md — session-by-session change log (full detail; not auto-loaded)
# See also: REM/CLAUDE.md — sibling project (distributed fleet via Tapo P110 + TP-Link cloud); repo at dom-robinson/stats. OWL = bench, REM = meter on the building.

## Project Identity
- **Name:** WattLab
- **Repo:** https://github.com/greeningofstreaming/wattlab
- **Host:** GoS1 — Ubuntu 24, `192.168.1.62`, externally `gos1.duckdns.org:2222`
- **Owner:** GoS (Greening of Streaming), French NGO loi 1901
- **Mission:** Measure environmental impact of streaming. Neutral, technically credible.
- **Full spec:** See WATTLAB_SPEC.md in repo root

## GoS Framing (always apply)
- "Not eco-warriors. Just people who dislike waste."
- If it can't be measured, it shouldn't be asserted.
- Separate device / network / data center / production+storage impacts explicitly.
- State scoping assumptions. Signal uncertainty. Traffic Light Confidence on all claims.
- Audience: CTOs, operators, infrastructure players, policymakers.

## GoS1 Server
- OS: Ubuntu 24, kernel 6.17
- CPU: AMD Ryzen 9 7900, 24 cores
- GPU: AMD Radeon RX 7800 XT — VAAPI (video) + ROCm (AI), 12GB VRAM
- RAM: 61GB · Disk: 457GB, 221GB free (April 2026)
- Python: 3.12.3 · Node: 20.x
- Claude Code: `~/.npm-global/bin/claude`, authenticated as nebul2
- Git: bs@ctoic.net / nebul2
- SSH users: simon, tania, dom, marisol, gos (owner)
- External: `ssh -p 2222 user@gos1.duckdns.org`
- Idle power: ~51-54W (stable), occasional drift to 58W

## Network Topology
```
BouyguesBox (192.168.1.x)
├── GoS1 (ethernet) → 192.168.1.62
└── Nighthawk RAX120 (AP mode)
    ├── MacBook (WiFi)
    └── Tapo P110 (WiFi) → 192.168.1.159
```

## Thermal Sensors
- CPU: `data['k10temp-pci-00c3']['Tctl']['temp1_input']`
- GPU junction: `data['amdgpu-pci-0300']['junction']['temp2_input']`
- GPU PPT: `data['amdgpu-pci-0300']['PPT']['power1_average']`
- Read via: `subprocess.run(['sensors', '-j'], ...)`

## Environment
- `.env` at `/home/gos/wattlab/.env` — gitignored
- Variables: `TAPO_EMAIL`, `TAPO_PASSWORD`, `TAPO_P110_IP`, `WATTLAB_GATE_PASSWORD`

## Installed Packages
- Python: tapo==0.8.12, python-dotenv, fastapi, uvicorn, python-multipart, torch 2.5.1+rocm6.2, diffusers, transformers, accelerate, pillow
- System: lm-sensors, ffmpeg 6.1.1, nmap
- AI: Ollama 0.20.2 (systemd service, port 11434)
- Models: tinyllama:latest (1.1B), mistral:latest (7B), gemma3:12b (12B), phi4:latest (14B), x/z-image-turbo (12GB, GPU blocked), x/flux2-klein (5.7GB, CUDA/MLX only)
- Image gen: stabilityai/sd-turbo + stabilityai/sdxl-turbo via diffusers (CPU for SD-Turbo only; GPU via ROCm for both; cached in ~/.cache/huggingface)

## Repo Structure
```
wattlab/
├── .env                          # gitignored
├── .gitignore                    # includes test_content/, results/
├── README.md
├── CLAUDE.md
├── JOURNAL.md
├── TESTING.md                    # three-tier testing strategy
├── WATTLAB_SPEC.md               # full product spec
├── data_analysis_nov25/          # Nov25 hackathon scripts
├── data_cleanup/
│   └── clean_measures.py         # Tania — aligns Tapo CSVs
├── test_content/
│   └── meridian_4k.mp4           # Netflix Open Content, CC BY 4.0, 812MB
├── results/                      # [to create] persistent JSON results
│   ├── video/
│   └── llm/
└── wattlab_service/
    ├── main.py                   # FastAPI routes + all HTML UI + queue worker
    ├── video.py                  # P110 + ffmpeg + thermals + focus mode
    ├── llm.py                    # Ollama inference + P110 measurement
    ├── image_gen.py              # SD-Turbo CPU diffusion + P110 measurement
    ├── persist.py                # Flat-file result storage + CSV/JSON export
    ├── settings.py               # Lab config (15 params, settings.json)
    ├── sources.py                # Pre-loaded test content registry
    └── power.py                  # Power measurement interface (Tapo P110); swap here for PDU/IPMI
```

## Measurement Protocol
1. Focus mode: stop background timers (sudoers: `/etc/sudoers.d/wattlab-focus`)
2. LLM only: unload model (keep_alive=0), sleep 3s
3. Baseline: 10 polls × 1s → W_base
4. Lock: `/tmp/gos-measure.lock`
5. Task: ffmpeg (nice -n -5) or Ollama API
6. Poll P110 + sensors at 1s
7. Compute: ΔW, ΔE = ΔW × (ΔT/3600) Wh, mWh/token (LLM)
8. Write result JSON to results/{type}/{date}_{job_id}.json
9. Focus exit: parallel timer restart (ThreadPoolExecutor + run_in_executor)

## Focus Mode Timers
sysstat-collect, anacron, fwupd-refresh, apt-daily, apt-daily-upgrade,
man-db, motd-news, update-notifier-download
Sudoers: `/etc/sudoers.d/wattlab-focus`

## Traffic Light Confidence
Variance-relative thresholds (Session 11). `noise_w = variance_pct/100 × w_base`
- 🟢 Repeatable: ΔW > variance_green_x × noise_w AND ≥conf_green_polls polls (defaults: 5×, 10 polls)
- 🟡 Early insight: ΔW ≥ variance_yellow_x × noise_w OR ≥conf_yellow_polls polls (defaults: 2×, 5 polls)
- 🔴 Need more data: below yellow threshold
- `variance_pct` default 2.0% — auto-updated by variance calibration run
- `confidence(delta_w, poll_count, w_base)` — all four modules (video, llm, image_gen, rag)

## Scope Statements
Video: "Device layer only (GoS1 server). Network, CDN, and CPE excluded."
LLM: "Device layer only (GoS1 server). Network and CPE excluded. No amortised training cost."

## Running Services
| Service | Port | Status |
|---|---|---|
| wattlab (systemd) | 8000 | ✅ 1 worker |
| ollama (systemd) | 11434 | ✅ active |

## Current URLs
- LAN: `http://192.168.1.62:8000` (paths: `/video /llm /image /demo /settings /queue-status /methodology /rag /carbon`)
- Tunnel: `ssh -p 2222 -L 8000:localhost:8000 user@gos1.duckdns.org`
- Public (HTTPS via certbot): `https://wattlab.greeningofstreaming.org`
- Gate password: in `.env` as `WATTLAB_GATE_PASSWORD` (ask owner)

## Roadmap

**Phases 1–8 shipped:** research integrity (persistence + export), measurement quality (LLM batched/warm-cold/streaming, H.265+AV1), settings & lab config, demo mode + GoS visual identity, image generation (SD-Turbo CPU/GPU + SDXL-Turbo), public access (nginx + cert + IP-gate), guided tour + credibility (confidence popover, resume), RAG energy test (Chroma + compare-3-modes).

**Active CRs:** see `CHANGE_REQUESTS.md` (CR-001 two-tier OWL, CR-001b demo lock, CR-002…CR-009).

### Recent sessions (one-line summary; full detail in JOURNAL.md + git log)
- **S10 (2026-04-07):** centralised power cache, ffmpeg cmd in result JSON, GPU PPT note, home nav restructure.
- **S11 (2026-04-09):** /methodology page; variance-based confidence framework (replaces fixed-W thresholds); /variance/run calibration endpoint.
- **S12 (2026-04-10):** video preset overhaul (3 codec rows × CPU/GPU/Both); full VAAPI pipeline; VAAPI surface-pool fix; meridian_120s test asset.
- **S13 (2026-04-10):** ABR rate control across all 6 presets; all_codecs compare mode; HTTPS via certbot.
- **S14 (2026-04-24):** Gemma 3 12B + Phi-4 added; SDXL-Turbo image gen; Compare Models ⚡; progressive-disclosure pilot; live telemetry badge; queue pause flag (/tmp/owl-paused); owl logo across all 10 pages.
- **S15 (2026-04-29):** _BASE_STYLES palette (CSS-var contrast pass); RAG compare cooldown; corpus browser; LLM CSV gains response column.
- **S16 (2026-05-01):** **CO₂e measurement** — `carbon.py` module (Eco2mix→ElectricityMaps→Ember static fallback ladder), `walk_and_enrich()` injects co2e block on all result shapes, `_CARBON_JS` UI helpers (live/EST badges, comparison strip with collapsed details + live French production mix), fmtMass auto-switches g/mg/µg, "below measurement floor" rendering when ΔE=0. **CR-002 methodology accuracy pass** — placeholders + settings injection so `baseline_polls`/`video_cooldown_s`/confidence thresholds can never drift from settings.json. **First test suite** — `wattlab_service/tests/test_carbon.py`, 28 tests, sets the testing pattern for the upcoming access-spine modules. Strategy docs landed: `CHANGE_REQUESTS.md` (CR-001 two-tier OWL + CR-001b demo lock + CR-002…CR-009), `AUDIT_BRIEF.md` + `AUDIT_RESPONSE.md`, `TRAINING_OWL_5MIN.md`, `rem-theme.css`.

### Deferred / open
- [ ] **Confidence multiplier grounding** — `variance_green_x`/`variance_yellow_x` (5×/2×) by judgement; statistical grounding pending session with Tanya.
- [ ] **Transcoding apples-to-apples** — bitrate is ABR-controlled; GOP/profile still default-per-encoder. Working session with Simon/Tanya.
- [ ] **Benchmark 2** — codec-natural rate control (CRF/QP) alongside Benchmark 1 (ABR). Add to `WATTLAB_SPEC.md`.
- [ ] **Access spine refactor** (audit's #1 recommendation) — `audience.py` + `capabilities.py` + `queue_control.py` before CR-001 lands.
- [ ] **Dockerize OWL** — isolate from future GoS1 projects. Two-stage plan (FastAPI+VAAPI, then ROCm). Long-term.
- [ ] **Factorise `_HEADER` constant** — mirror `_FOOTER` so `/methodology` and `/queue-status` use the same shape as standard pages.
- [ ] **Guided Tour Findings step** — currently echoes session run; redesign to aggregate across all stored results to surface body-of-evidence learnings (see Key Findings).
- [ ] **RAG visitor upload + corpus PDF view** — see `CHANGE_REQUESTS.md` follow-ups.
- [ ] **Power-user/visitor UX watch** — progressive-disclosure pilot is live across test pages; revisit if a visible density toggle becomes needed.

## Key Findings to Date

### Video — ABR All-Codecs benchmark (Meridian 120s, n=3, all 🟢) — canonical
Identical-bitrate ABR (H.264 4000 kbps · H.265 2000 kbps · AV1 1500 kbps). GPU = full VAAPI pipeline.
- **H.264:** CPU 37.3s / 0.83 Wh · GPU 17.5s / 0.37 Wh → GPU **~55% less energy, ~53% faster**
- **H.265:** CPU 70.3s / 1.58 Wh · GPU 14.5s / 0.29 Wh → GPU **~81% less energy, ~79% faster**
- **AV1:** CPU 30.8s / 0.65 Wh · GPU 14.5s / 0.30 Wh → GPU **~55% less energy, ~53% faster**
- H.265 GPU and AV1 GPU both finish at **exactly 14.5s** — VAAPI hardware clock is the GPU-path ceiling.
- Most efficient: AV1 GPU and H.265 GPU (gap within noise).
- AV1 CPU beats H.265 CPU on speed AND energy — SVT-AV1 multi-core advantage.
- Results within 1% across 3 runs; supersedes all CRF/QP comparisons.

### LLM Cold Inference 🟢/🟡
- Mistral 7B T3: **0.943 mWh/token** 🟢
- TinyLlama T3: **0.061 mWh/token** 🟡 (~15× more efficient — but generic-boilerplate answers).
- TinyLlama short tasks are below the P110 floor; batched mode required for reliable readings.

### Image generation
- SD-Turbo CPU first run: **0.2063 Wh/image**, 12.15s, ~30 W delta. Backend: Ryzen 9 7900, 8 steps, 512×512.
- SD-Turbo + SDXL-Turbo on GPU (ROCm, fp16 small / fp32 VAE upcast at 512×512) shipped in S14; Compare Models ⚡ runs both at 4 native steps for apples-to-apples size comparison.
- VRAM ceiling: SDXL-Turbo at 1024×1024 busts the 12 GB Navi31 budget via the fp32 VAE upcast — 512×512 is the sweet spot.

### RAG faithfulness ⭐ (S15, "What is REM?")
All three models (TinyLlama 1.1B, Gemma 3 12B, Phi-4 14B) **retrieved identical correct chunks** — the GoS REM whitepapers. But TinyLlama hallucinated *"REM is a framework provided by the European Commission"*, blending the GoS source with an adjacent JRC chunk. Gemma and Phi-4 stayed faithful. **Headline:** RAG retrieval works at small scale; RAG *quality* depends on the consuming model's faithfulness. Hallucination is a third axis on the energy/quality tradeoff.

## Visual Identity
- Project mark: owl SVG at `wattlab_service/static/owl.svg` (2.4KB teal/green geometric).
- Org mark: GoS round bug, footer only (`_LOGO`).
- Dark theme: `#0a0a0a` bg, `#00ff99` accent — all tokens centralised in `_BASE_STYLES` (`main.py:~276`).
- For external/family theming see `rem-theme.css` (drop-in stylesheet that re-skins REM with OWL palette).
