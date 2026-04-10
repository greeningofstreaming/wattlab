import asyncio
import base64
import io
import os
import random
import subprocess
import time
import json
from pathlib import Path
from video import focus_mode_enter, focus_mode_exit
import settings as cfg
from power import get_power_watts

# Required for gfx1101 (RX 7800 XT) with PyTorch ROCm — must be set before torch import
os.environ.setdefault("HSA_OVERRIDE_GFX_VERSION", "11.0.0")


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
IMAGE_STEPS_CPU = 8        # ~12s per image on Ryzen 9 7900 — reliable P110 measurement
IMAGE_STEPS_GPU = 20       # ~2s per image on RX 7800 XT — need batch for reliable measurement
IMAGE_STEPS = IMAGE_STEPS_CPU  # default / backwards compat
IMAGE_SIZE = 512           # px, square
GPU_BATCH_SIZE = 5         # GPU generates 5 images (~10s total) → report energy/image = total/5

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

def confidence(delta_w: float, poll_count: int, w_base: float) -> dict:
    s = cfg.load()
    noise_w = s["variance_pct"] / 100.0 * max(w_base, 1.0)
    if delta_w > s["variance_green_x"] * noise_w and poll_count >= s["conf_green_polls"]:
        return {"flag": "🟢", "label": "Repeatable"}
    elif delta_w >= s["variance_yellow_x"] * noise_w or poll_count >= s["conf_yellow_polls"]:
        return {"flag": "🟡", "label": "Early insight"}
    else:
        return {"flag": "🔴", "label": "Need more data"}

# --- Generation ---

def generate_image(prompt: str, seed: int = None, device: str = "cpu") -> dict:
    """Run SD-Turbo image generation on CPU or GPU.
    GPU mode generates GPU_BATCH_SIZE images and reports energy/image.
    Returns base64 PNG of first/last image + timing."""
    from diffusers import AutoPipelineForText2Image
    import torch

    use_gpu = (device == "gpu")
    steps = IMAGE_STEPS_GPU if use_gpu else IMAGE_STEPS_CPU
    batch = GPU_BATCH_SIZE if use_gpu else 1

    generator = torch.Generator().manual_seed(seed) if seed is not None else None

    t_start = time.time()
    if use_gpu:
        pipe = AutoPipelineForText2Image.from_pretrained(
            IMAGE_MODEL_ID,
            torch_dtype=torch.float16,
        )
        pipe = pipe.to("cuda")
    else:
        pipe = AutoPipelineForText2Image.from_pretrained(
            IMAGE_MODEL_ID,
            torch_dtype=torch.float32,
        )
    t_load = time.time()

    images = []
    for i in range(batch):
        gen = torch.Generator().manual_seed(seed + i) if seed is not None else None
        result = pipe(
            prompt=prompt,
            num_inference_steps=steps,
            guidance_scale=0.0,
            generator=gen,
        )
        images.append(result.images[0])
    t_end = time.time()

    # Encode the last generated image to base64 PNG
    buf = io.BytesIO()
    images[-1].save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    total_s = round(t_end - t_start, 2)
    gen_s = round(t_end - t_load, 2)

    return {
        "b64_png": b64,
        "prompt": prompt,
        "steps": steps,
        "size": IMAGE_SIZE,
        "model": IMAGE_MODEL_ID,
        "device": device,
        "batch_size": batch,
        "load_s": round(t_load - t_start, 2),
        "gen_s": gen_s,
        "gen_s_per_image": round(gen_s / batch, 2),
        "total_s": total_s,
    }


def _calc_energy(w_base: float, w_task: float, delta_t: float,
                 readings: list, batch: int) -> dict:
    poll_count = len(readings)
    delta_w = round(w_task - w_base, 2)
    delta_e_wh = round(delta_w * (delta_t / 3600), 4)
    wh_per_image = round(delta_e_wh / batch, 4)
    conf = confidence(delta_w, poll_count, w_base)
    return {
        "w_base": round(w_base, 2),
        "w_task": round(w_task, 2),
        "delta_w": delta_w,
        "delta_t_s": round(delta_t, 2),
        "delta_e_wh": delta_e_wh,
        "wh_per_image": wh_per_image,
        "poll_count": poll_count,
        "confidence": conf,
    }


async def _run_single_image(prompt: str, device: str, job_id: str,
                             jobs: dict, stage_prefix: str,
                             stopped_timers: list) -> dict:
    """Run one image measurement pass (CPU or GPU). Returns result dict."""
    s = cfg.load()

    if jobs is not None:
        jobs[job_id]["stage"] = f"{stage_prefix}_baseline"

    sensors_base = read_sensors()
    w_base = await measure_baseline(polls=s["baseline_polls"])

    if jobs is not None:
        jobs[job_id]["stage"] = f"{stage_prefix}_generating"

    stop_event = asyncio.Event()
    poll_task = asyncio.create_task(poll_during_task(stop_event))

    gen_result = await asyncio.get_event_loop().run_in_executor(
        None, generate_image, prompt, None, device
    )

    stop_event.set()
    readings = await poll_task

    sensors_end = read_sensors()
    batch = gen_result["batch_size"]
    delta_t = gen_result["gen_s"]   # measure only generation time, not load time
    w_task = sum(r[1] for r in readings) / len(readings) if readings else w_base
    energy = _calc_energy(w_base, w_task, delta_t, readings, batch)

    return {
        "device": device,
        "generation": gen_result,
        "energy": energy,
        "thermals": {
            "cpu_base": sensors_base.get("cpu_tctl"),
            "cpu_end": sensors_end.get("cpu_tctl"),
            "gpu_base": sensors_base.get("gpu_junction"),
            "gpu_end": sensors_end.get("gpu_junction"),
        },
    }


# --- Main measurement entry points ---

async def run_image_measurement(prompt: str, job_id: str,
                                jobs: dict = None,
                                device: str = "cpu") -> dict:
    s = cfg.load()

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
        None, generate_image, full_prompt, None, device
    )

    stop_event.set()
    readings = await poll_task

    LOCK_FILE.unlink(missing_ok=True)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, focus_mode_exit, stopped)

    if jobs is not None:
        jobs[job_id]["stage"] = "done"

    sensors_end = read_sensors()
    batch = gen_result["batch_size"]
    delta_t = gen_result["gen_s"]
    w_task = sum(r[1] for r in readings) / len(readings) if readings else w_base
    energy = _calc_energy(w_base, w_task, delta_t, readings, batch)

    device_label = "GPU (RX 7800 XT, ROCm)" if device == "gpu" else "CPU (Ryzen 9 7900)"
    scope = (f"Device layer only (GoS1). {device_label}. "
             f"No amortised training cost.")

    return {
        "mode": device,
        "job_id": job_id,
        "prompt": prompt,
        "full_prompt": full_prompt,
        "modifier": modifier,
        "generation": gen_result,
        "energy": energy,
        "thermals": {
            "cpu_base": sensors_base.get("cpu_tctl"),
            "cpu_end": sensors_end.get("cpu_tctl"),
            "gpu_base": sensors_base.get("gpu_junction"),
            "gpu_end": sensors_end.get("gpu_junction"),
        },
        "scope": scope,
    }


async def run_image_both_measurement(prompt: str, job_id: str,
                                     jobs: dict = None) -> dict:
    """Run CPU pass then GPU pass, with cooldown in between. Report comparison."""
    s = cfg.load()

    modifier = random.choice(PROMPT_MODIFIERS)
    full_prompt = f"{prompt}, {modifier}"

    if jobs is not None:
        jobs[job_id]["stage"] = "baseline_cpu"
        jobs[job_id]["full_prompt"] = full_prompt

    stopped = focus_mode_enter()
    LOCK_FILE.write_text(job_id)

    # --- CPU pass ---
    cpu_result = await _run_single_image(full_prompt, "cpu", job_id, jobs,
                                          "cpu", stopped)

    # --- Cooldown ---
    if jobs is not None:
        jobs[job_id]["stage"] = "cooldown"
    cooldown = s.get("video_cooldown_s", 30)
    await asyncio.sleep(cooldown)

    # --- GPU pass ---
    gpu_result = await _run_single_image(full_prompt, "gpu", job_id, jobs,
                                          "gpu", stopped)

    LOCK_FILE.unlink(missing_ok=True)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, focus_mode_exit, stopped)

    if jobs is not None:
        jobs[job_id]["stage"] = "done"

    analysis = _analyse_image(cpu_result, gpu_result)

    return {
        "mode": "both",
        "job_id": job_id,
        "prompt": prompt,
        "full_prompt": full_prompt,
        "modifier": modifier,
        "cpu": cpu_result,
        "gpu": gpu_result,
        "analysis": analysis,
        "scope": "Device layer only (GoS1). CPU vs GPU comparison. No amortised training cost.",
    }


def _analyse_image(cpu: dict, gpu: dict) -> dict:
    cpu_e = cpu["energy"]
    gpu_e = gpu["energy"]
    cpu_wh = cpu_e["wh_per_image"]
    gpu_wh = gpu_e["wh_per_image"]
    cpu_t = cpu["generation"]["gen_s_per_image"]
    gpu_t = gpu["generation"]["gen_s_per_image"]

    energy_winner = "cpu" if cpu_wh <= gpu_wh else "gpu"
    speed_winner = "gpu" if gpu_t < cpu_t else "cpu"

    energy_diff_pct = round(abs(cpu_wh - gpu_wh) / max(cpu_wh, gpu_wh) * 100, 1)
    speed_diff_pct = round(abs(cpu_t - gpu_t) / max(cpu_t, gpu_t) * 100, 1)

    if speed_winner == "gpu":
        finding = (f"GPU {speed_diff_pct}% faster per image. "
                   f"{'CPU' if energy_winner == 'cpu' else 'GPU'} {energy_diff_pct}% "
                   f"more energy efficient.")
    else:
        finding = (f"CPU {speed_diff_pct}% faster per image. "
                   f"{'CPU' if energy_winner == 'cpu' else 'GPU'} {energy_diff_pct}% "
                   f"more energy efficient.")

    return {
        "energy_winner": energy_winner,
        "speed_winner": speed_winner,
        "energy_diff_pct": energy_diff_pct,
        "speed_diff_pct": speed_diff_pct,
        "finding": finding,
    }
