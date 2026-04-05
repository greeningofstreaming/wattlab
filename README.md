# WattLab

WattLab measures the real device-side energy cost of video transcoding and AI inference on physical hardware — using a calibrated smart plug, not estimates or models.

Built by [Greening of Streaming](https://greeningofstreaming.org), a French NGO working on the environmental impact of streaming infrastructure.

**Live instance:** [wattlab.greeningofstreaming.org](http://wattlab.greeningofstreaming.org) *(DNS pending — currently accessible via SSH tunnel)*

---

## What it measures

| Test | What you get |
|---|---|
| Video transcode | Energy (Wh) and time for CPU vs GPU H.264/H.265/AV1 encode |
| LLM inference | mWh per output token, tokens/sec — Mistral 7B and TinyLlama |
| Image generation | Wh per image — SD-Turbo, CPU and GPU paths |

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

**Video — H.264 1080p from 4K (4 runs) 🟢**
- CPU: 174.3s mean, 4.06 Wh mean
- GPU: 114.0s mean, 4.42 Wh mean
- GPU is 34.5% faster but uses 9.7% more energy on this workload

**LLM cold inference 🟢**
- Mistral 7B T3: 0.94 mWh/token
- TinyLlama 1.1B T3: 0.06 mWh/token (~15× more efficient per token)

**Image generation — SD-Turbo CPU 🟢**
- 0.21 Wh/image, 12s generation time, ~30W delta above idle

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
