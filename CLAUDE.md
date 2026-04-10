# WattLab — Claude Code Context File
# Auto-loaded by Claude Code. Keep this current.
# Last updated: 2026-04-10 (Session 13)
# See also: GOS1_INFRA.md — server infrastructure, Nextcloud backup, personal stack context

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
- Models: tinyllama:latest (1.1B), mistral:latest (7B), x/z-image-turbo (12GB, GPU blocked), x/flux2-klein (5.7GB, CUDA/MLX only)
- Image gen: stabilityai/sd-turbo via diffusers (CPU, cached in ~/.cache/huggingface)

## Repo Structure
```
wattlab/
├── .env                          # gitignored
├── .gitignore                    # includes test_content/, results/
├── README.md
├── CLAUDE.md
├── JOURNAL.md
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
    └── sources.py                # Pre-loaded test content registry
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
- Home: `http://192.168.1.62:8000`
- Video: `http://192.168.1.62:8000/video`
- LLM: `http://192.168.1.62:8000/llm`
- Image: `http://192.168.1.62:8000/image`
- Guided Tour: `http://192.168.1.62:8000/demo`
- Settings: `http://192.168.1.62:8000/settings`
- Queue: `http://192.168.1.62:8000/queue-status`
- Tunnel: `ssh -p 2222 -L 8000:localhost:8000 user@gos1.duckdns.org`
- Public (nginx, HTTP only until cert): `http://176.148.88.254` / `http://wattlab.greeningofstreaming.org` (DNS pending)
- Gate password: in `.env` as `WATTLAB_GATE_PASSWORD` (ask owner)

## Prioritised Roadmap

### Phase 1 — Research Integrity ✅
- [x] JSON result persistence (flat files, not SQLite)
- [x] Result export (CSV + JSON download) — video, llm, image
- [x] Previous results browser (last 10 runs per test type)

### Phase 2 — Measurement Quality ✅
- [x] LLM batched mode (load once, rest, run N times)
- [x] LLM warm vs cold toggle
- [x] LLM editable prompts with reset-to-default
- [x] LLM streaming output display (word-by-word)
- [x] Video H.265 + AV1 presets

### Phase 3 — Settings & Lab Config ✅
- [x] /settings page (lab only, blocked on public URL)
- [x] Configurable: baseline duration, cooldown, repeats, rest time
- [x] Settings stored in settings.json

### Phase 4 — Demo Mode (session 4) ✅
- [x] /demo guided journey
- [x] GoS visual identity (logo, colours, typography)
- [x] Inline methodology explanations
- [x] "Previous run" instant result option
- [x] Anti-slideware proof points per test type

### Phase 5 — Image Generation (session 5) ✅
- [x] Install diffusion model (sd-turbo via diffusers, CPU)
- [x] Energy per image metric (0.2063 Wh first run, 🟢)
- [x] Live image display as generated
- [x] Prompt variation per run (random colour/mood modifier)
- [x] GPU image generation — SD-Turbo via PyTorch ROCm, batch of 5 images, HSA_OVERRIDE_GFX_VERSION=11.0.0

### Phase 6 — Public Access ✅ (partial — DNS/SSL pending)
- [x] Block /settings from public URL — graceful read-only view (IP-based `_is_local()`)
- [x] nginx config: `infra/wattlab.nginx.conf` — rate limiting, HTTP proxy, HTTPS block commented pending cert
- [x] Setup script: `infra/setup-nginx.sh`
- [x] nginx running on GoS1
- [x] BouyguesBox: TCP 80+443 → 192.168.1.62
- [x] Password gate: cookie-based, `WATTLAB_GATE_PASSWORD` in `.env`
- [ ] DNS: A record `wattlab.greeningofstreaming.org → 176.148.88.254` — DNS table lost during Wix ownership transfer (Dom → Ben). Needs rebuild.
- [ ] Let's Encrypt SSL: `sudo certbot --nginx -d wattlab.greeningofstreaming.org`
- [ ] Enable HTTP→HTTPS redirect in nginx config, reload nginx

### Phase 7 — Guided Tour & Credibility ✅
- [x] Demo renamed to Guided Tour (`/demo` URL unchanged)
- [x] Three-band structure per step (What this shows / What we're doing / Result)
- [x] Confidence flag step in Guided Tour (step 4 of 5)
- [x] Confidence flag popover on all result pages (click any 🟢🟡🔴 badge)
- [x] README written
- [x] Queue resume: ↩ Resume links on queue page, ?job= param on test pages
- [x] Navigation: ← Home, Lab Mode button removed

### Phase 8 — RAG Energy Test ✅
- [x] `/rag` page: baseline vs rag vs rag_large — energy, mWh/token, confidence
- [x] ChromaDB index build/status endpoints; sentence-transformer embeddings (singletons)
- [x] `rag.py` module, `persist.py` RAG branches (summary + CSV)
- [x] **Compare 3 modes** button: sequential baseline→rag→rag_large, side-by-side result cards, answers saved
- [x] Shared `_PROGRESS_JS`: `wlRenderProgress`, `wlStageList`, `wlRenderQueued`, `wlFormatElapsed` — injected into all 4 test pages
- [x] Home nav restructured: Guided Tour prominent, primary (Video/Image/LLM), secondary (RAG/Queue/Settings)

### Session 10 — Infrastructure & Measurement Quality (2026-04-07) ✅
- [x] nginx `client_max_body_size 2g` — fixes video upload 413 for files >1MB
  - Note: `systemctl reload` insufficient (old workers keep old config); requires `systemctl restart nginx`
- [x] Centralized power cache: `_power_cache` + `power_poller()` background task (5s cadence)
  - `/power` endpoint and home page read from cache; no direct P110 call on request path
  - Fixes multi-user power display sync issue
- [x] FFmpeg command logged in result JSON (`transcode.ffmpeg_cmd`) and shown in result card as collapsible
- [x] GPU PPT explanatory note in result cards (PPT vs P110 system delta)
- [x] Home nav: Video gets own row; Image/LLM/RAG under "AI WORKLOADS" label; Queue/Settings as utility row

### Session 11 — Methodology, Variance Confidence, ffmpeg Edit (2026-04-09) ✅
- [x] `/methodology` page — full measurement methodology as standalone HTML; linked from home nav utility row
- [x] Variance-based confidence framework — replaces fixed ΔW thresholds (5W/2W) with `noise_w = variance_pct/100 × w_base`; `confidence()` updated in all 4 modules with new `w_base` param
- [x] New settings params: `variance_pct` (2.0%), `variance_green_x` (5×), `variance_yellow_x` (2×), `variance_runs`, `variance_cooldown_s`, `variance_cpu_cmd`, `variance_gpu_cmd`
- [x] Settings page: Confidence section updated; new Variance calibration section with sliders + editable cmd textareas + Run button
- [x] `/variance/run` endpoint — queues calibration job; runs N × (H264-CPU + H265-GPU) on Meridian, computes **three separate CVs**: `variance_idle_pct` (raw P110 baselines), `variance_cpu_pct` (H264 ΔW), `variance_gpu_pct` (H265 ΔW); mean → `variance_pct`
- [x] Settings page: three read-only calibration output fields shown above editable `variance_pct`; show "—" until first calibration run
- [x] `/video/preview-cmd` endpoint — returns ffmpeg command template(s) for selected preset
- [x] Video page: ffmpeg command shown before run; editable textarea on LAN, read-only on public; custom cmd passed through to run endpoints
- [x] `persist.py` CSV: `ffmpeg_cmd` added to video export
- [x] `/methodology` confidence section updated to explain variance-relative approach
- [x] CLAUDE.md/JOURNAL.md updated; SSH tunnel URL clarified (localhost:8000, not 192.168.1.62)

### Session 13 — ABR Benchmark, Compare All Codecs, HTTPS, CSV/Output fixes (2026-04-10) ✅
- [x] **ABR rate control** across all 6 presets — replaced CRF (CPU) and QP (GPU) with `-b:v Nk` shared bitrate target per codec (H.264: 4000 kbps, H.265: 2000 kbps, AV1: 1500 kbps). CPU and GPU now receive identical tasks; output file sizes match as confirmation. Settings: `h264_bitrate_kbps`, `h265_bitrate_kbps`, `av1_bitrate_kbps` (editable in Settings page).
- [x] **Compare all codecs** — new `all_codecs` preset runs all 6 presets (3 codec pairs, sequential with cooldown). Returns energy matrix with `analyse_all()` cross-codec summary. UI: matrix table (CPU time/energy/output · GPU time/energy/output · conf per codec), highlights for most efficient + fastest, collapsible per-codec detail cards.
- [x] Output size columns split in all-codecs matrix — was a single combined "CPU/GPU" column after GPU; now separate "CPU out" and "GPU out" columns adjacent to their respective energy columns
- [x] CSV export: `output_size_mb` added; full thermals now exported (`cpu_mean`, `gpu_mean`, `gpu_ppt_peak_w` added alongside existing base/peak fields)
- [x] HTTPS: DNS A record restored, certbot provisioned, nginx restarted. Service live at https://wattlab.greeningofstreaming.org
- [x] Docker containerisation added to deferred roadmap (two-stage plan; see Deferred)
- [x] CPU temp under GPU load: closed — full GPU pipeline (session 12) resolved this; frames GPU-resident throughout, CPU decode overhead eliminated

### Session 12 — Preset Overhaul, Full GPU Pipeline, VAAPI Fix (2026-04-10) ✅
- [x] Video presets restructured: 3 rows (H.264 / H.265 / AV1), each with CPU / GPU / Both cards
  - Details collapsible via `<details class="pdesc">` with `▸/▾` toggle
  - `DEFAULT` badge removed; `.pspec` class for codec spec line
- [x] All GPU presets switched to **full pipeline** (hwaccel vaapi decode + encode)
  - Was: partial pipeline (CPU software decode + GPU encode) — CPU was heating on GPU jobs
  - Now: `-hwaccel vaapi -hwaccel_output_format vaapi` decode + `scale_vaapi` + encoder
  - Represents real live-encoding workflows (Harmonic, Ateme). See Key Findings.
- [x] `av1_gpu` preset added (av1_vaapi, QP 28, full pipeline, RDNA3 AV1 engine)
- [x] `h265_both` and `av1_both` presets wired through all endpoints and STAGES/STAGE_MAP
- [x] VAAPI surface pool fix: `-extra_hw_frames 32` + `scale_vaapi=w=-2:h=1080:format=nv12`
  - Fixes "Cannot allocate memory" at frame ~7178/7193 (EOS filter flush bug in Mesa VA-API)
  - `out_size_mb` now also reports from file-on-disk (not gated on `success=True`) — muxer writes valid file even when ffmpeg exits non-zero from the EOS error
- [x] Confidence hint: `confidence()` returns `hint` field when signal strong but polls < green threshold; rendered in single and both result cards
- [x] `meridian_120s` source: 2-min extract of Meridian 4K (123MB, ~7200 frames) — fast demo mode
  - Gives 14–30 polls per GPU/CPU job, all 🟢; added to video page source picker
  - Generated with: `ffmpeg -y -ss 0 -i meridian_4k.mp4 -t 120 -c copy meridian_120s.mp4`
- [x] Queue badge: bottom-right on all pages, shows live watts + queued job count
- [x] Guided tour: 7 steps, RAG step added (step 4), page X/Y counter, Previous buttons on all steps; confidence step updated to variance-relative language
- [x] Settings: three read-only variance calibration output fields (idle/cpu/gpu pct); save-before-run fix; stage labels `run N/M — H.264 CPU encode`; `variance_runs` slider min=2
- [x] Previous runs: codec displayed (e.g. "H.264 CPU vs H.264 GPU"); `persist.py` both-mode summary adds `cpu_preset`/`gpu_preset`

### Deferred
- [x] DNS: A record `wattlab.greeningofstreaming.org → 176.148.88.254` — restored 2026-04-10
- [x] SSL: certbot provisioned 2026-04-10. Service now at https://wattlab.greeningofstreaming.org
- [x] CPU temp under GPU load: resolved by full GPU pipeline switch (session 12) — frames stay GPU-resident, CPU no longer involved in decode/DMA
- [ ] Image page progress bar: add elapsed time (video + LLM already have it)
- [ ] GPU image generation: code complete, needs first clean measurement run
- [ ] phi4 pull: `ollama pull phi4` (9.1GB) — enables 14B model in RAG compare
- [ ] Confidence multiplier grounding: working session with Tanya — `variance_green_x`/`variance_yellow_x` (5×/2×) currently by judgement; need statistical grounding from calibration run data
- [ ] Transcoding profile documentation: GOP structure and profile level not yet confirmed apples-to-apples across codecs — bitrate target is now standardised (ABR), but GOP/profile still TBD. Work with Simon/Tanya.
- [ ] Benchmark 2: representative real-world presets — CRF (CPU) and QP (GPU), codec-appropriate rate control. Benchmark 1 (ABR, current) ensures identical task; Benchmark 2 would show each codec at its natural operating point. Add to WATTLAB_SPEC.md.
- [ ] main.py refactor: split into routes/, Jinja templates, typed models, tests. Raised in session 8 external audit. Valid technical debt; deferred until post-demo.
- [ ] Dockerize WattLab service — isolate from future GoS1 projects. Stage 1: FastAPI + VAAPI (`--device /dev/dri`), `--network host`, drop or proxy focus mode via thin host helper socket service. Stage 2 (later, if portability needed): full ROCm image for GPU image gen. Ollama stays as host systemd service, accessed over host network. See conversation 2026-04-10 for full analysis.

## Key Findings to Date

### Video — Full GPU Pipeline vs CPU (Meridian 120s extract, H.264 Both) 🟢
- CPU (libx264 · CRF 23): 30.6s, 0.664 Wh, 78.2W delta
- GPU (h264_vaapi · full pipeline): 17.6s, 0.376 Wh, 76.8W delta
- GPU **42.5% faster, 43.4% less energy** — full pipeline eliminates the CPU decode overhead
- Note: old partial-pipeline result (4 runs on full 12-min file) showed GPU as 9.7% *more* energy — that was CPU-decode + GPU-encode, measuring worst of both worlds

### Video — H.264 1080p from 4K (Meridian full, 4 runs, partial pipeline — superseded) 🟢
- CPU: 174.3s, 4.06 Wh mean; GPU: 114.0s, 4.42 Wh mean
- GPU was 34.5% faster but 9.7% more energy — partial pipeline artifact, not representative of live encoding

### Video — AV1 Both (Meridian 120s, first run) 🟢
- CPU (libsvtav1 · CRF 30): 28.2s, 0.586 Wh, 74.8W delta
- GPU (av1_vaapi · full pipeline): 14.5s, 0.284 Wh, 70.5W delta
- GPU **48.6% faster, 51.5% less energy**

### Video — ABR All-Codecs benchmark (Meridian 120s, 3 runs, all 🟢) — Session 13
All presets on ABR (H.264: 4000 kbps, H.265: 2000 kbps, AV1: 1500 kbps). GPU full pipeline throughout.
- H.264: CPU 37.3s / 0.83 Wh · GPU 17.5s / 0.37 Wh → GPU **~55% less energy, ~53% faster**
- H.265: CPU 70.3s / 1.58 Wh · GPU 14.5s / 0.29 Wh → GPU **~81% less energy, ~79% faster**
- AV1:  CPU 30.8s / 0.65 Wh · GPU 14.5s / 0.30 Wh → GPU **~55% less energy, ~53% faster**
- Most energy-efficient preset: AV1 GPU (~0.29–0.31 Wh) and H.265 GPU (~0.29 Wh) — gap within noise
- H.265 and AV1 GPU both encode at exactly 14.5s — VAAPI hardware clock is the ceiling on the GPU path
- AV1 CPU outperforms H.265 CPU on both speed and energy — SVT-AV1 multi-core optimisation
- Results reproduced across 3 runs to within 1%; supersedes all CRF/QP comparisons

### LLM Cold Inference 🟢/🟡
- Mistral 7B T3: 0.943 mWh/token 🟢
- TinyLlama T3: 0.061 mWh/token 🟡 (~15x more efficient)
- TinyLlama too fast for reliable P110 measurement — batching needed

### Image Generation CPU — SD-Turbo (first run) 🟢
- 0.2063 Wh/image, 12.15s, ~30W delta above idle
- Backend: CPU (Ryzen 9 7900), stabilityai/sd-turbo, 8 steps, 512×512
- GPU path: code complete (float16, ROCm, batch of 5), needs first test run
  - SD-Turbo float16 needs ~2-3 GB VRAM — well within 11.1 GB available
  - Earlier "GPU deferred" note was about z-image-turbo (10.3B), not SD-Turbo

## Visual Identity
- Logo: round bug mark from greeningofstreaming.org
- Logo URL: https://static.wixstatic.com/media/b1006e_f5e9aff607cf4133abf7089207dc3cab~mv2.png
- Embed on every page, top-left, links to greeningofstreaming.org
- Current dark theme (#0a0a0a bg, #00ff99 accent) — keep
- Add sans-serif (Inter/system-ui) for explanatory text in demo mode
