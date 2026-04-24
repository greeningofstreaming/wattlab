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

IMAGE_MODEL_ID = "stabilityai/sd-turbo"  # default / backwards compat
IMAGE_STEPS_CPU = 8        # ~12s per image on Ryzen 9 7900 — reliable P110 measurement
IMAGE_STEPS_GPU = 20       # ~2s per image on RX 7800 XT — need batch for reliable measurement
IMAGE_STEPS = IMAGE_STEPS_CPU  # default / backwards compat
IMAGE_SIZE = 512           # px, square
GPU_BATCH_SIZE = 5         # GPU generates 5 images (~10s total) → report energy/image = total/5

# Model registry. CPU paths use fp32 (ROCm not available for CPU); GPU uses fp16.
# sd-turbo: ~1B params, 512px, ADD-distilled SD 2.1. Natural 1–4 steps; we use
#   higher step counts to get reliable P110 polls.
# sdxl-turbo: ~3.5B params, ADD-distilled SDXL. Native 1–4 steps, 1024×1024
#   trained but run here at 512×512 for apples-to-apples model comparison and
#   VRAM fit (the automatic SDXL fp32 VAE upcast at 1024 exceeds 12GB).
#   GPU only — CPU fp32 path is impractical (>5min/image).
IMAGE_MODELS = {
    "sd-turbo": {
        "label":        "SD-Turbo",
        "repo":         "stabilityai/sd-turbo",
        "params":       "~1B",
        "native_px":    512,
        "cpu_ok":       True,
        "cpu_steps":    IMAGE_STEPS_CPU,
        "gpu_steps":    IMAGE_STEPS_GPU,   # solo: 20 steps (historical, over-sampled)
        "gpu_batch":    GPU_BATCH_SIZE,    # solo: batch 5
        # Compare Models mode overrides: run at native 1–4 step operating point
        # so the comparison against SDXL-Turbo is at equivalent step counts,
        # not biased by our solo-mode measurement-reliability choice of 20 steps.
        "compare_steps": 4,
        "compare_batch": 30,   # ~0.4s/image × 30 ≈ 12s — reliable P110 polls
        "size_px":      512,
        "fp16_variant": False,  # repo ships single-precision safetensors
    },
    "sdxl-turbo": {
        "label":        "SDXL-Turbo",
        "repo":         "stabilityai/sdxl-turbo",
        "params":       "~3.5B",
        "native_px":    1024,    # model's training resolution (not used here)
        "cpu_ok":       False,
        "cpu_steps":    None,
        "gpu_steps":    4,       # native operating point for SDXL-Turbo
        # Run at 512×512 to match SD-Turbo for apples-to-apples compare, and
        # because SDXL's fp16 VAE produces black images on Navi31 so the
        # pipeline upcasts VAE to fp32; at 1024×1024 that fp32 decode exceeds
        # our 12GB VRAM budget. At 512px the fp32 VAE path fits comfortably.
        "size_px":      512,
        "gpu_batch":    15,      # ~0.66s/image × 15 ≈ 10s — reliable P110 polls
        # Compare Models mode uses the same config as solo (already at native).
        "compare_steps": 4,
        "compare_batch": 15,
        "fp16_variant": True,    # repo publishes dedicated fp16 weights
    },
}

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

def generate_image(prompt: str, seed: int = None, device: str = "cpu",
                   model_key: str = "sd-turbo",
                   steps_override: int = None,
                   batch_override: int = None) -> dict:
    """Run text-to-image generation on CPU or GPU for the named model.
    GPU mode generates model-specific batch size and reports energy/image.
    Compare-models mode passes steps_override/batch_override to force the
    native operating point across both models.
    Returns base64 PNG of last image + timing."""
    from diffusers import AutoPipelineForText2Image
    import torch

    cfg_m = IMAGE_MODELS.get(model_key)
    if cfg_m is None:
        raise ValueError(f"Unknown image model: {model_key}")

    use_gpu = (device == "gpu")
    if not use_gpu and not cfg_m["cpu_ok"]:
        raise ValueError(f"Model {model_key} is GPU-only (CPU path disabled)")

    steps = steps_override if steps_override is not None else (
        cfg_m["gpu_steps"] if use_gpu else cfg_m["cpu_steps"])
    batch = batch_override if batch_override is not None else (
        cfg_m["gpu_batch"] if use_gpu else 1)
    size_px = cfg_m["size_px"]
    repo = cfg_m["repo"]

    t_start = time.time()
    pipe = None
    try:
        if use_gpu:
            kwargs = {"torch_dtype": torch.float16}
            if cfg_m.get("fp16_variant"):
                kwargs["variant"] = "fp16"
            pipe = AutoPipelineForText2Image.from_pretrained(repo, **kwargs)
            pipe = pipe.to("cuda")
        else:
            pipe = AutoPipelineForText2Image.from_pretrained(
                repo,
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
                height=size_px,
                width=size_px,
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
            "size": size_px,
            "model": repo,
            "model_key": model_key,
            "model_label": cfg_m["label"],
            "device": device,
            "batch_size": batch,
            "load_s": round(t_load - t_start, 2),
            "gen_s": gen_s,
            "gen_s_per_image": round(gen_s / batch, 2),
            "total_s": total_s,
        }
    finally:
        # Release pipeline weights so the uvicorn worker doesn't accumulate
        # VRAM across sequential image jobs (otherwise each run strands its
        # ~2GB of UNet + text-encoder weights until Python GC eventually runs).
        if pipe is not None:
            del pipe
        import gc
        gc.collect()
        if use_gpu and torch.cuda.is_available():
            torch.cuda.empty_cache()


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
                             stopped_timers: list,
                             model_key: str = "sd-turbo",
                             seed: int = None,
                             steps_override: int = None,
                             batch_override: int = None) -> dict:
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
        None, generate_image, prompt, seed, device, model_key,
        steps_override, batch_override
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
        "model_key": model_key,
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
                                device: str = "cpu",
                                model_key: str = "sd-turbo") -> dict:
    s = cfg.load()

    cfg_m = IMAGE_MODELS.get(model_key)
    if cfg_m is None:
        raise ValueError(f"Unknown image model: {model_key}")
    if device == "cpu" and not cfg_m["cpu_ok"]:
        raise ValueError(f"{cfg_m['label']} is GPU-only")

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
        None, generate_image, full_prompt, None, device, model_key
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
             f"Model: {cfg_m['label']} ({cfg_m['params']}) at {cfg_m['size_px']}px. "
             f"No amortised training cost.")

    return {
        "mode": device,
        "job_id": job_id,
        "prompt": prompt,
        "full_prompt": full_prompt,
        "modifier": modifier,
        "model_key": model_key,
        "model_label": cfg_m["label"],
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
                                     jobs: dict = None,
                                     model_key: str = "sd-turbo") -> dict:
    """Run CPU pass then GPU pass, with cooldown in between. Report comparison."""
    s = cfg.load()

    cfg_m = IMAGE_MODELS.get(model_key)
    if cfg_m is None:
        raise ValueError(f"Unknown image model: {model_key}")
    if not cfg_m["cpu_ok"]:
        raise ValueError(f"{cfg_m['label']} is GPU-only — cannot run CPU vs GPU comparison")

    modifier = random.choice(PROMPT_MODIFIERS)
    full_prompt = f"{prompt}, {modifier}"

    if jobs is not None:
        jobs[job_id]["stage"] = "baseline_cpu"
        jobs[job_id]["full_prompt"] = full_prompt

    stopped = focus_mode_enter()
    LOCK_FILE.write_text(job_id)

    # --- CPU pass ---
    cpu_result = await _run_single_image(full_prompt, "cpu", job_id, jobs,
                                          "cpu", stopped, model_key=model_key)

    # --- Cooldown ---
    if jobs is not None:
        jobs[job_id]["stage"] = "cooldown"
    cooldown = s.get("video_cooldown_s", 30)
    await asyncio.sleep(cooldown)

    # --- GPU pass ---
    gpu_result = await _run_single_image(full_prompt, "gpu", job_id, jobs,
                                          "gpu", stopped, model_key=model_key)

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
        "model_key": model_key,
        "model_label": cfg_m["label"],
        "cpu": cpu_result,
        "gpu": gpu_result,
        "analysis": analysis,
        "scope": (f"Device layer only (GoS1). CPU vs GPU comparison. "
                  f"Model: {cfg_m['label']} ({cfg_m['params']}). "
                  f"No amortised training cost."),
    }


async def run_image_compare_models_measurement(prompt: str, job_id: str,
                                                jobs: dict = None) -> dict:
    """Run SD-Turbo then SDXL-Turbo on GPU with same prompt + seed.
    Reports per-model energy; image quality is intentionally shown side-by-side
    for subjective comparison (not reducible to a single metric).
    """
    s = cfg.load()

    modifier = random.choice(PROMPT_MODIFIERS)
    full_prompt = f"{prompt}, {modifier}"
    # Same seed for both models so any quality difference comes from the model,
    # not from latent noise. Uses current time for a fresh but reproducible run.
    seed = int(time.time()) % (2**31)

    if jobs is not None:
        jobs[job_id]["stage"] = "baseline_small"
        jobs[job_id]["full_prompt"] = full_prompt

    stopped = focus_mode_enter()
    LOCK_FILE.write_text(job_id)

    small_cfg = IMAGE_MODELS["sd-turbo"]
    large_cfg = IMAGE_MODELS["sdxl-turbo"]
    small = await _run_single_image(full_prompt, "gpu", job_id, jobs,
                                     "small", stopped,
                                     model_key="sd-turbo", seed=seed,
                                     steps_override=small_cfg["compare_steps"],
                                     batch_override=small_cfg["compare_batch"])

    if jobs is not None:
        jobs[job_id]["stage"] = "cooldown"
    cooldown = s.get("video_cooldown_s", 30)
    await asyncio.sleep(cooldown)

    large = await _run_single_image(full_prompt, "gpu", job_id, jobs,
                                     "large", stopped,
                                     model_key="sdxl-turbo", seed=seed,
                                     steps_override=large_cfg["compare_steps"],
                                     batch_override=large_cfg["compare_batch"])

    LOCK_FILE.unlink(missing_ok=True)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, focus_mode_exit, stopped)

    if jobs is not None:
        jobs[job_id]["stage"] = "done"

    analysis = _analyse_models(small, large)

    return {
        "mode": "compare_models",
        "job_id": job_id,
        "prompt": prompt,
        "full_prompt": full_prompt,
        "modifier": modifier,
        "seed": seed,
        "small": small,
        "large": large,
        "analysis": analysis,
        "scope": ("Device layer only (GoS1). GPU (RX 7800 XT, ROCm). "
                  "SD-Turbo (~1B) vs SDXL-Turbo (~3.5B), both at 512×512 and "
                  "4 inference steps (each model's native operating point) so "
                  "model size is the only variable. Same prompt + seed. "
                  "Image quality is subjective — shown side-by-side for "
                  "visual comparison. No amortised training cost."),
    }


def _analyse_models(small: dict, large: dict) -> dict:
    s_e = small["energy"]
    l_e = large["energy"]
    s_wh = s_e["wh_per_image"]
    l_wh = l_e["wh_per_image"]
    s_t  = small["generation"]["gen_s_per_image"]
    l_t  = large["generation"]["gen_s_per_image"]

    energy_ratio = round(l_wh / s_wh, 2) if s_wh else None
    time_ratio   = round(l_t / s_t, 2)   if s_t   else None

    finding = (
        f"Larger model uses {energy_ratio}× more energy per image "
        f"and takes {time_ratio}× longer. "
        f"Quality difference is visible in the side-by-side output — "
        f"whether it's worth the extra energy is a subjective call."
    )
    return {
        "energy_ratio_large_over_small": energy_ratio,
        "time_ratio_large_over_small":   time_ratio,
        "small_wh_per_image": s_wh,
        "large_wh_per_image": l_wh,
        "finding": finding,
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
