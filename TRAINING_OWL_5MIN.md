# OWL — 5-Minute Training Narrative

**Purpose:** spoken segment for GoS's 1-hour training course on five years of measurement learnings.
**Length:** ~735 words → ~5 minutes at ~140 wpm.
**Audience:** streaming-industry CTOs, operators, infrastructure people, policymakers.
**Voice:** spoken prose. Read aloud, not from slides.
**Sibling deliverable:** `TRAINING_REM_5MIN.md` (REM counterpart, same shape, written in a separate session).
**Section markers in brackets are timing cues, not for delivery.**

---

**[OPENING — anchor on a finding · ~45 seconds]**

Last month we measured something that tells you everything about why OWL exists. Same workload — transcoding a two-minute clip of *Meridian* from 4K down to 1080p, H.264, with the same bitrate target on both runs. Same hardware. We ran it twice: once on the CPU, once on the GPU with the full VAAPI pipeline.

The CPU run used 0.83 watt-hours and took 37 seconds. The GPU run used 0.37 watt-hours and took 17 seconds — fifty-five percent less energy, more than twice as fast.

That number contradicts what most engineers in this room would have predicted from intuition. And here's the thing — we wouldn't have believed it either, except we measured it on the wall, against a grounded baseline, with a confidence flag, and we'd run it three times to be sure.

That habit — *measure, don't assert* — is what OWL is.

**[WHAT IS OWL · ~60 seconds]**

OWL stands for Online WattLab. It's a public, browser-runnable measurement bench that GoS built to put concrete numbers on the energy and carbon cost of the workloads behind streaming. You pick a workload — video transcoding, LLM inference, image generation, retrieval-augmented-generation — you click Run, and you watch the watts and the grams of CO₂e land in real time, on real hardware, against the live French electricity grid.

It does three things that, together, no other tool in this space does.

It measures **wall power, not modelled power**. There's a smart plug between the server and the wall socket; what you see is what the workload actually drew, including PSU losses, RAM, drives, the lot.

It converts watt-hours to gCO₂e using **live grid carbon intensity from Eco2mix** — RTE and Etalab's official French TSO data, refreshed every fifteen minutes — with explicit fallback to country-level annual means for comparison cities. Every CO₂e number on the screen is tagged "live" or "estimated" so you always know which one you're reading.

And it puts a **traffic-light confidence flag** on every result. Green means repeatable, yellow means directional, red means below the measurement floor. We never dress up a yellow as a green.

**[KEY FINDINGS · ~80 seconds]**

Five years of GoS measurement work taught us things that don't fit on a single slide. OWL puts a few of them within reach in real time.

On video transcoding, the encoder choice and the encoder *implementation* both matter more than people think. H.265 on the GPU uses eighty percent less energy than H.265 on the CPU for the same bitrate target. AV1 on the AMD GPU we use finishes in exactly 14.5 seconds — same as H.265 — which tells you the hardware encoder clock is the bottleneck, not the codec. These are findings you can cite in vendor conversations, not back-of-envelope estimates.

On AI workloads, scale and efficiency don't move together. TinyLlama at 1.1 billion parameters is fifteen times more energy-efficient per token than Mistral 7B. But ask both the same question grounded in our own corpus — say, *"What is REM?"*, where the answer lives in a GoS whitepaper we've indexed — and TinyLlama returns a confident, wrong answer; the larger models stay faithful to the source. **Energy, quality, and faithfulness are three independent axes of any AI tradeoff**, and OWL lets you walk that triangle in your browser.

On grid carbon, the same watt-hour means very different things in different places. The headline number in France right now, with the nuclear-heavy mix, is around eleven grams of CO₂ per kilowatt-hour. In Poland it's nearly six hundred. Same workload, same code, **fifty times the carbon footprint** depending on where the server is plugged in. OWL surfaces that comparison alongside every result you run.

**[CLOSING · ~45 seconds]**

OWL is opening publicly at this conference. Anyone in the streaming industry will be able to run a transcode, an LLM inference, or an image generation, see live energy and carbon numbers, and walk away with something they can cite. The measurement is the same whether you're a casual visitor or a GoS member — the public version isn't a stripped-down demo, it's the real bench.

What you'll see if you visit is partly the technical credibility — real hardware, real grid, honest measurement — but it's also the GoS philosophy made concrete. *If it can't be measured, it shouldn't be asserted.* OWL is what that line looks like when you actually build the bench.

And if we've done our job right, OWL is also a starting point. The members tier — custom uploads, your own video files, your own corpus, your own prompts — is how this becomes useful for *your* infrastructure, not just ours. That's the conversation we want to start at GoS this year.
