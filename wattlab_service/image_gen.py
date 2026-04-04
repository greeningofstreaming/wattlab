import asyncio
import base64
import io
import random
import subprocess
import time
import json
from pathlib import Path
from dotenv import dotenv_values
from tapo import ApiClient
from video import focus_mode_enter, focus_mode_exit
import settings as cfg

config = dotenv_values("/home/gos/wattlab/.env")
LOCK_FILE = Path("/tmp/gos-measure.lock")

# Prompt colour/mood modifiers for variation per run (anti-slideware proof)
PROMPT_MODIFIERS = [
    "bathed in emerald light",
    "under a cobalt sky",
    "in amber afternoon sun",
    "in violet twilight",
    "with a crimson horizon",
    "drenched in golden hour light",
    "in cool silver mist",
    "with deep indigo shadows",
]

IMAGE_MODEL_ID = "stabilityai/sd-turbo"
IMAGE_STEPS = 8        # ~12s per image on Ryzen 9 7900 — reliable P110 measurement
IMAGE_SIZE = 512       # px, square

# --- P110 helpers ---

async def get_power_watts() -> float:
    client = ApiClient(config["TAPO_EMAIL"], config["TAPO_PASSWORD"])
    device = await client.p110(config["TAPO_P110_IP"])
    result = await device.get_energy_usage()
    return result.current_power / 1000

async def measure_baseline(polls: int = 10) -> float:
    readings = []
    for _ in range(polls):
        readings.append(await get_power_watts())
        await asyncio.sleep(1)
    return round(sum(readings) / len(readings), 2)

async def poll_during_task(stop_event: asyncio.Event) -> list:
    readings = []
    while not stop_event.is_set():
        readings.append((time.time(), await get_power_watts()))
        await asyncio.sleep(1)
    return readings

def read_sensors() -> dict:
    try:
        result = subprocess.run(['sensors', '-j'], capture_output=True, text=True)
        data = json.loads(result.stdout)
        return {
            "cpu_tctl": data['k10temp-pci-00c3']['Tctl']['temp1_input'],
            "gpu_junction": data['amdgpu-pci-0300']['junction']['temp2_input'],
        }
    except Exception:
        return {"cpu_tctl": None, "gpu_junction": None}

def confidence(delta_w: float, poll_count: int) -> dict:
    s = cfg.load()
    if delta_w > s["conf_green_delta_w"] and poll_count >= s["conf_green_polls"]:
        return {"flag": "🟢", "label": "Repeatable"}
    elif delta_w >= s["conf_yellow_delta_w"] or poll_count >= s["conf_yellow_polls"]:
        return {"flag": "🟡", "label": "Early insight"}
    else:
        return {"flag": "🔴", "label": "Need more data"}

# --- Generation ---

def generate_image(prompt: str, seed: int = None) -> dict:
    """Run SD-Turbo image generation on CPU. Returns base64 PNG + timing."""
    from diffusers import AutoPipelineForText2Image
    import torch

    generator = torch.Generator().manual_seed(seed) if seed is not None else None

    t_start = time.time()
    pipe = AutoPipelineForText2Image.from_pretrained(
        IMAGE_MODEL_ID,
        torch_dtype=torch.float32,
    )
    t_load = time.time()

    image = pipe(
        prompt=prompt,
        num_inference_steps=IMAGE_STEPS,
        guidance_scale=0.0,
        generator=generator,
    ).images[0]
    t_end = time.time()

    # Encode to base64 PNG
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    return {
        "b64_png": b64,
        "prompt": prompt,
        "steps": IMAGE_STEPS,
        "size": IMAGE_SIZE,
        "model": IMAGE_MODEL_ID,
        "load_s": round(t_load - t_start, 2),
        "gen_s": round(t_end - t_load, 2),
        "total_s": round(t_end - t_start, 2),
    }

# --- Main measurement entry point ---

async def run_image_measurement(prompt: str, job_id: str,
                                jobs: dict = None) -> dict:
    s = cfg.load()

    # Pick a random modifier and append it to make each run visibly distinct
    modifier = random.choice(PROMPT_MODIFIERS)
    full_prompt = f"{prompt}, {modifier}"

    if jobs is not None:
        jobs[job_id]["stage"] = "baseline"
        jobs[job_id]["full_prompt"] = full_prompt

    stopped = focus_mode_enter()
    sensors_base = read_sensors()
    w_base = await measure_baseline(polls=s["baseline_polls"])

    LOCK_FILE.write_text(job_id)

    if jobs is not None:
        jobs[job_id]["stage"] = "generating"

    stop_event = asyncio.Event()
    poll_task = asyncio.create_task(poll_during_task(stop_event))

    gen_result = await asyncio.get_event_loop().run_in_executor(
        None, generate_image, full_prompt, None
    )

    stop_event.set()
    readings = await poll_task

    LOCK_FILE.unlink(missing_ok=True)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, focus_mode_exit, stopped)

    if jobs is not None:
        jobs[job_id]["stage"] = "done"

    sensors_end = read_sensors()

    delta_t = gen_result["total_s"]
    w_task = sum(r[1] for r in readings) / len(readings) if readings else w_base
    delta_w = round(w_task - w_base, 2)
    delta_e_wh = round(delta_w * (delta_t / 3600), 4)
    conf = confidence(delta_w, len(readings))

    return {
        "mode": "single",
        "job_id": job_id,
        "prompt": prompt,
        "full_prompt": full_prompt,
        "modifier": modifier,
        "generation": gen_result,
        "energy": {
            "w_base": round(w_base, 2),
            "w_task": round(w_task, 2),
            "delta_w": delta_w,
            "delta_t_s": delta_t,
            "delta_e_wh": delta_e_wh,
            "wh_per_image": delta_e_wh,
            "poll_count": len(readings),
            "confidence": conf,
        },
        "thermals": {
            "cpu_base": sensors_base.get("cpu_tctl"),
            "cpu_end": sensors_end.get("cpu_tctl"),
            "gpu_base": sensors_base.get("gpu_junction"),
            "gpu_end": sensors_end.get("gpu_junction"),
        },
        "scope": "Device layer only (GoS1). CPU inference. GPU excluded from this run. No amortised training cost.",
    }
