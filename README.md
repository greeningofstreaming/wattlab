# WattLab

WattLab measures the real device-side energy cost of video transcoding and AI inference on physical hardware — using a calibrated smart plug, not estimates or models.

Built by [Greening of Streaming](https://greeningofstreaming.org), a French NGO working on the environmental impact of streaming infrastructure.

**Live instance:** [wattlab.greeningofstreaming.org](https://wattlab.greeningofstreaming.org)

---

## What it measures

| Test | What you get |
|---|---|
| Video transcode | Energy (Wh) and time for CPU vs GPU — H.264, H.265, AV1 at matched ABR bitrates. "Compare all codecs" runs all six presets in one go. |
| LLM inference | mWh per output token, tokens/sec — TinyLlama 1.1B, Mistral 7B, Gemma 3 12B across three size tiers |
| Image generation | Wh per image — SD-Turbo (~1B), SDXL-Turbo (~3.5B). "Compare Models" runs both with same prompt + seed so model size is the only variable. |
| RAG energy test | Energy cost of retrieval-augmented generation vs plain LLM — baseline / rag / rag_large compared side-by-side |

All figures are delta above idle baseline, sampled at 1-second intervals via a Tapo P110 smart plug on the mains supply.

**Scope: device layer only.** Network, CDN, and CPE are explicitly excluded. No amortised training cost in LLM measurements.

---

## What it does not measure

- Network energy (transit, CDN, last-mile)
- Embodied carbon or manufacturing impact
- LLM training cost
- Cloud or multi-node workloads

These exclusions are deliberate. Scope statements appear on every result.

---

## Hardware

**GoS1** — lab server in France

| Component | Spec |
|---|---|
| CPU | AMD Ryzen 9 7900, 24 cores |
| GPU | AMD Radeon RX 7800 XT, 12 GB VRAM (ROCm) |
| RAM | 61 GB |
| OS | Ubuntu 24, kernel 6.17 |
| Power meter | Tapo P110 smart plug (mains, 1s polling) |
| Idle draw | ~51–54 W |

---

## Key findings so far

**Video — ABR all-codecs benchmark (Meridian 120s, 3 runs, all 🟢)**
- H.264 at 4000 kbps: CPU 37.3s / 0.83 Wh · GPU 17.5s / 0.37 Wh → GPU ~55% less energy
- H.265 at 2000 kbps: CPU 70.3s / 1.58 Wh · GPU 14.5s / 0.29 Wh → GPU ~81% less energy
- AV1 at 1500 kbps: CPU 30.8s / 0.65 Wh · GPU 14.5s / 0.30 Wh → GPU ~55% less energy
- All GPU presets use the full VAAPI pipeline (decode + scale + encode). Earlier "GPU uses more energy" result was from a partial pipeline (CPU decode + GPU encode) — superseded.

**LLM cold inference 🟢**
- Mistral 7B T3: 0.94 mWh/token
- TinyLlama 1.1B T3: 0.06 mWh/token (~15× more efficient per token)
- Gemma 3 12B now available for larger-model comparison

**Image generation — SD-Turbo CPU 🟢**
- 0.21 Wh/image, 12s generation time, ~30W delta above idle

**Ship-of-Theseus honesty:** when earlier methodology improvements (full GPU pipeline, ABR rate control) change what a result means, the old finding is marked superseded rather than silently overwritten.

---

## Access

**Guided Tour** (public, read-only settings):
```
http://wattlab.greeningofstreaming.org
```

**Lab mode** (full controls, SSH tunnel required):
```
ssh -p 2222 -L 8000:localhost:8000 user@gos1.duckdns.org
# then open http://localhost:8000
```

Settings are read-only from public URLs and fully editable only from the LAN or SSH tunnel.

---

## Running locally

Requires GoS1 or equivalent hardware (P110 plug, ROCm GPU optional).

```bash
cd wattlab_service
pip install -r requirements.txt   # see CLAUDE.md for full package list
cp .env.example .env              # add TAPO_EMAIL, TAPO_PASSWORD, TAPO_P110_IP
uvicorn main:app --host 0.0.0.0 --port 8000
```

The service runs as a systemd unit on GoS1 (`systemctl status wattlab`).

---

## Documentation

- [`WATTLAB_SPEC.md`](WATTLAB_SPEC.md) — full product spec, measurement protocol, roadmap
- [`JOURNAL.md`](JOURNAL.md) — session-by-session build log with findings
- [`CLAUDE.md`](CLAUDE.md) — project context for Claude Code (AI assistant config)
