# WattLab — Claude Code Context File
# Auto-loaded by Claude Code. Keep this current.
# Last updated: 2026-04-06

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
- RAM: 61GB · Disk: 457GB, 308GB free
- Python: 3.12.3 · Node: 20.x
- Claude Code: `~/.npm-global/bin/claude`, authenticated as nebul2
- Git: bs@ctoic.net / nebul2
- SSH users: simon, tania, dom, gos (owner)
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
    ├── settings.py               # Lab config (8 params, settings.json)
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
- 🟢 Repeatable: ΔW > 5W, ≥10 polls
- 🟡 Early insight: ΔW ≥ 2W or ≥5 polls
- 🔴 Need more data: ΔW < 2W

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

### Deferred
- [ ] DNS: A record `wattlab.greeningofstreaming.org → 176.148.88.254` — DNS table wiped during Wix ownership transfer (Dom → Ben). Rebuild needed.
- [ ] SSL: `sudo certbot --nginx -d wattlab.greeningofstreaming.org` — after DNS restored. Note: use `systemctl restart nginx` (not reload) after certbot edits config.
- [ ] Image page progress bar: add elapsed time (video + LLM already have it)
- [ ] GPU image generation: code complete, needs first clean measurement run
- [ ] phi4 pull: `ollama pull phi4` (9.1GB) — enables 14B model in RAG compare
- [ ] Confidence threshold refinement: working session with Tanya (current thresholds are heuristic, need statistical grounding)
- [ ] Transcoding profile documentation: confirm H.264/H.265/AV1 presets are apples-to-apples (same bitrate target, GOP, profile level) — work with Simon/Tanya
- [ ] CPU temp under GPU load: investigate why CPU heats more during GPU encode than CPU encode

## Key Findings to Date

### Video H.264 1080p from 4K (Meridian, 4 runs) 🟢
- CPU: 174.3s, 4.06 Wh mean — faster AND more energy efficient
- GPU: 114.0s, 4.42 Wh mean
- GPU 34.5% faster, 9.7% more energy on this workload

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
