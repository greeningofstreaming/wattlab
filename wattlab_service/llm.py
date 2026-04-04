import asyncio
import functools
import subprocess
import time
import json
import urllib.request
from pathlib import Path
from dotenv import dotenv_values
from tapo import ApiClient
import settings as cfg

config = dotenv_values("/home/gos/wattlab/.env")
LOCK_FILE = Path("/tmp/gos-measure.lock")

OLLAMA_URL = "http://localhost:11434/api/generate"

# Fixed prompt tasks — predetermined for comparability
TASKS = {
    "T1": {
        "label": "Short factual",
        "prompt": "What is adaptive bitrate streaming? Answer in 2 sentences.",
        "expected_tokens": 60,
    },
    "T2": {
        "label": "Medium reasoning",
        "prompt": "Explain the trade-offs between H.264, H.265, and AV1 for a streaming operator choosing a codec strategy.",
        "expected_tokens": 300,
    },
    "T3": {
        "label": "Long generation",
        "prompt": "Write a detailed technical briefing on network energy attribution challenges in streaming impact measurement.",
        "expected_tokens": 800,
    },
}

MODELS = {
    "tinyllama": {"label": "TinyLlama", "size": "637MB", "params": "1.1B"},
    "mistral": {"label": "Mistral 7B", "size": "4.4GB", "params": "7B"},
}

# --- P110 helpers (same as video.py) ---

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
    except:
        return {"cpu_tctl": None, "gpu_junction": None}

def confidence(delta_w: float, poll_count: int) -> dict:
    s = cfg.load()
    if delta_w > s["conf_green_delta_w"] and poll_count >= s["conf_green_polls"]:
        return {"flag": "🟢", "label": "Repeatable"}
    elif delta_w >= s["conf_yellow_delta_w"] or poll_count >= s["conf_yellow_polls"]:
        return {"flag": "🟡", "label": "Early insight"}
    else:
        return {"flag": "🔴", "label": "Need more data"}

# --- Ollama helpers ---

def unload_model(model: str):
    """Force Ollama to unload model from VRAM before baseline measurement."""
    import urllib.request, json
    payload = json.dumps({
        "model": model,
        "keep_alive": 0
    }).encode()
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except:
        pass

# --- Ollama inference ---

def run_inference_streaming(model: str, prompt: str, on_token=None,
                            num_gpu: int = -1) -> dict:
    """Stream inference token by token. num_gpu=0 forces CPU; -1 = Ollama default (GPU)."""
    payload_dict = {"model": model, "prompt": prompt, "stream": True}
    if num_gpu == 0:
        payload_dict["options"] = {"num_gpu": 0}
    payload = json.dumps(payload_dict).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    t_start = time.time()
    full_response = ""
    prompt_tokens = 0
    output_tokens = 0
    with urllib.request.urlopen(req, timeout=300) as resp:
        for line in resp:
            line = line.strip()
            if not line:
                continue
            chunk = json.loads(line)
            token = chunk.get("response", "")
            full_response += token
            if on_token and token:
                on_token(token)
            if chunk.get("done"):
                t_end = time.time()
                prompt_tokens = chunk.get("prompt_eval_count", 0)
                output_tokens = chunk.get("eval_count", 0)
                return {
                    "response": full_response,
                    "prompt_tokens": prompt_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": prompt_tokens + output_tokens,
                    "duration_s": round(t_end - t_start, 2),
                    "tokens_per_sec": round(output_tokens / max(t_end - t_start, 0.1), 1),
                }
    t_end = time.time()
    return {
        "response": full_response,
        "prompt_tokens": 0, "output_tokens": 0, "total_tokens": 0,
        "duration_s": round(t_end - t_start, 2), "tokens_per_sec": 0,
    }

# --- Main measurement ---

async def _run_single_inference(model_key: str, task_key: str,
                                baseline: float, sensors_base: dict,
                                device: str, effective_prompt: str,
                                jobs: dict, job_id: str,
                                stage_prefix: str) -> dict:
    """Run one inference pass against an already-measured baseline."""
    model = MODELS[model_key]
    task = TASKS[task_key]
    num_gpu = 0 if device == "cpu" else -1

    if jobs and job_id:
        jobs[job_id]["stage"] = f"{stage_prefix}_inference"
        jobs[job_id]["partial_response"] = ""

    def on_token(t):
        if jobs and job_id:
            jobs[job_id]["partial_response"] = \
                jobs[job_id].get("partial_response", "") + t

    stop_event = asyncio.Event()
    poll_task = asyncio.create_task(poll_during_task(stop_event))
    fn = functools.partial(run_inference_streaming, model_key,
                           effective_prompt, on_token, num_gpu)
    inference_result = await asyncio.get_event_loop().run_in_executor(None, fn)
    stop_event.set()
    readings = await poll_task

    sensors_end = read_sensors()
    delta_t = inference_result["duration_s"]
    w_task = sum(r[1] for r in readings) / len(readings) if readings else baseline
    delta_w = round(w_task - baseline, 2)
    delta_e_wh = round(delta_w * (delta_t / 3600), 4)
    output_tokens = inference_result["output_tokens"]
    mwh_per_token = round((delta_e_wh * 1000) / max(output_tokens, 1), 4) \
        if output_tokens else None
    conf = confidence(delta_w, len(readings))

    return {
        "device": device,
        "inference": inference_result,
        "energy": {
            "w_base": round(baseline, 2),
            "w_task": round(w_task, 2),
            "delta_w": delta_w,
            "delta_t_s": delta_t,
            "delta_e_wh": delta_e_wh,
            "mwh_per_token": mwh_per_token,
            "poll_count": len(readings),
            "confidence": conf,
        },
        "thermals": {
            "cpu_base": sensors_base.get("cpu_tctl"),
            "gpu_base": sensors_base.get("gpu_junction"),
            "cpu_end": sensors_end.get("cpu_tctl"),
            "gpu_end": sensors_end.get("gpu_junction"),
        },
    }


async def run_llm_measurement(model_key: str, task_key: str,
                               jobs: dict = None, job_id: str = None,
                               warm: bool = False, prompt: str = None,
                               device: str = "gpu") -> dict:
    model = MODELS[model_key]
    task = TASKS[task_key]
    effective_prompt = prompt or task["prompt"]

    s = cfg.load()
    if jobs and job_id: jobs[job_id]["stage"] = "baseline"
    if not warm:
        unload_model(model_key)
        await asyncio.sleep(s["llm_unload_settle_s"])
    w_base = await measure_baseline(polls=s["baseline_polls"])
    sensors_base = read_sensors()

    LOCK_FILE.write_text(job_id or "llm")
    result = await _run_single_inference(
        model_key, task_key, w_base, sensors_base,
        device, effective_prompt, jobs, job_id, device
    )
    LOCK_FILE.unlink(missing_ok=True)
    if jobs and job_id: jobs[job_id]["stage"] = "done"

    return {
        "mode": "single",
        "model_key": model_key,
        "model_label": model["label"],
        "model_params": model["params"],
        "task_key": task_key,
        "task_label": task["label"],
        "prompt": effective_prompt,
        "warm": warm,
        "device": device,
        "inference": result["inference"],
        "energy": result["energy"],
        "thermals": result["thermals"],
        "scope": "Device layer only (GoS1). Network and CPE excluded. No amortised training cost.",
    }


async def run_llm_both_measurement(model_key: str, task_key: str,
                                    jobs: dict = None, job_id: str = None,
                                    warm: bool = False, prompt: str = None) -> dict:
    """Run CPU then GPU sequentially, new baseline between, return comparison."""
    model = MODELS[model_key]
    task = TASKS[task_key]
    effective_prompt = prompt or task["prompt"]
    s = cfg.load()

    # --- CPU pass ---
    if jobs and job_id: jobs[job_id]["stage"] = "baseline_cpu"
    if not warm:
        unload_model(model_key)
        await asyncio.sleep(s["llm_unload_settle_s"])
    w_base_cpu = await measure_baseline(polls=s["baseline_polls"])
    sensors_base_cpu = read_sensors()

    LOCK_FILE.write_text(job_id or "llm")
    cpu_result = await _run_single_inference(
        model_key, task_key, w_base_cpu, sensors_base_cpu,
        "cpu", effective_prompt, jobs, job_id, "cpu"
    )

    # --- Cooldown + re-baseline ---
    if jobs and job_id: jobs[job_id]["stage"] = "cooldown"
    await asyncio.sleep(s["video_cooldown_s"])

    if jobs and job_id: jobs[job_id]["stage"] = "baseline_gpu"
    unload_model(model_key)
    await asyncio.sleep(s["llm_unload_settle_s"])
    w_base_gpu = await measure_baseline(polls=s["baseline_polls"])
    sensors_base_gpu = read_sensors()

    # --- GPU pass ---
    gpu_result = await _run_single_inference(
        model_key, task_key, w_base_gpu, sensors_base_gpu,
        "gpu", effective_prompt, jobs, job_id, "gpu"
    )
    LOCK_FILE.unlink(missing_ok=True)
    if jobs and job_id: jobs[job_id]["stage"] = "done"

    analysis = _analyse_llm(cpu_result, gpu_result)

    return {
        "mode": "both",
        "model_key": model_key,
        "model_label": model["label"],
        "model_params": model["params"],
        "task_key": task_key,
        "task_label": task["label"],
        "prompt": effective_prompt,
        "warm": warm,
        "cpu": cpu_result,
        "gpu": gpu_result,
        "analysis": analysis,
        "scope": "Device layer only (GoS1). Network and CPE excluded. No amortised training cost.",
    }


def _analyse_llm(cpu: dict, gpu: dict) -> dict:
    ce = cpu["energy"]
    ge = gpu["energy"]
    ci = cpu["inference"]
    gi = gpu["inference"]

    speed_winner = "GPU" if gi["tokens_per_sec"] > ci["tokens_per_sec"] else "CPU"
    energy_winner = "GPU" if ge["delta_e_wh"] < ce["delta_e_wh"] else "CPU"
    mwh_winner = "GPU" if (ge["mwh_per_token"] or 999) < (ce["mwh_per_token"] or 999) else "CPU"

    speed_diff_pct = round(abs(gi["tokens_per_sec"] - ci["tokens_per_sec"]) /
                           max(ci["tokens_per_sec"], 0.01) * 100, 1)
    energy_diff_pct = round(abs(ce["delta_e_wh"] - ge["delta_e_wh"]) /
                            max(ce["delta_e_wh"], ge["delta_e_wh"], 0.0001) * 100, 1)

    finding = (
        f"{speed_winner} was {speed_diff_pct}% faster "
        f"({gi['tokens_per_sec']} vs {ci['tokens_per_sec']} tok/s). "
        f"{energy_winner} used {energy_diff_pct}% less total energy "
        f"({ce['delta_e_wh']:.4f} vs {ge['delta_e_wh']:.4f} Wh). "
    )
    if ce["mwh_per_token"] and ge["mwh_per_token"]:
        finding += (
            f"{mwh_winner} was more efficient per token "
            f"({ce['mwh_per_token']:.4f} vs {ge['mwh_per_token']:.4f} mWh/token)."
        )

    return {
        "speed_winner": speed_winner,
        "energy_winner": energy_winner,
        "mwh_winner": mwh_winner,
        "speed_diff_pct": speed_diff_pct,
        "energy_diff_pct": energy_diff_pct,
        "finding": finding,
    }


async def run_llm_batch_measurement(model_key: str, task_key: str, repeats: int,
                                     warm: bool = False, prompt: str = None,
                                     jobs: dict = None, job_id: str = None) -> dict:
    """Load model once, run N times, aggregate results."""
    model = MODELS[model_key]
    task = TASKS[task_key]
    effective_prompt = prompt or task["prompt"]

    s = cfg.load()
    if jobs and job_id: jobs[job_id]["stage"] = "baseline"
    if not warm:
        unload_model(model_key)
        await asyncio.sleep(s["llm_unload_settle_s"])
    w_base = await measure_baseline(polls=s["baseline_polls"])
    sensors_base = read_sensors()

    LOCK_FILE.write_text(job_id or "llm")
    run_results = []

    for i in range(repeats):
        if i > 0:
            if jobs and job_id: jobs[job_id]["stage"] = f"rest_{i}"
            await asyncio.sleep(s["llm_rest_s"])

        if jobs and job_id:
            jobs[job_id]["stage"] = f"inference_{i + 1}_of_{repeats}"
            jobs[job_id]["partial_response"] = ""

        stop_event = asyncio.Event()

        def on_token(t, _jid=job_id):
            if jobs and _jid:
                jobs[_jid]["partial_response"] = jobs[_jid].get("partial_response", "") + t

        poll_task = asyncio.create_task(poll_during_task(stop_event))
        inference_result = await asyncio.get_event_loop().run_in_executor(
            None, run_inference_streaming, model_key, effective_prompt, on_token
        )
        stop_event.set()
        readings = await poll_task

        delta_t = inference_result["duration_s"]
        w_task = sum(r[1] for r in readings) / len(readings) if readings else w_base
        delta_w = round(w_task - w_base, 2)
        delta_e_wh = round(delta_w * (delta_t / 3600), 4)
        output_tokens = inference_result["output_tokens"]
        mwh_per_token = round((delta_e_wh * 1000) / max(output_tokens, 1), 4) if output_tokens else None

        run_results.append({
            "run": i + 1,
            "inference": inference_result,
            "energy": {
                "w_base": w_base,
                "w_task": round(w_task, 2),
                "delta_w": delta_w,
                "delta_t_s": delta_t,
                "delta_e_wh": delta_e_wh,
                "mwh_per_token": mwh_per_token,
                "poll_count": len(readings),
                "confidence": confidence(delta_w, len(readings)),
            },
        })

    LOCK_FILE.unlink(missing_ok=True)
    sensors_end = read_sensors()
    if jobs and job_id: jobs[job_id]["stage"] = "done"

    def _mean(vals): return round(sum(vals) / len(vals), 4) if vals else None
    def _stddev(vals):
        if len(vals) < 2: return None
        m = sum(vals) / len(vals)
        return round((sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5, 4)

    e_vals = [r["energy"]["delta_e_wh"] for r in run_results]
    tps_vals = [r["inference"]["tokens_per_sec"] for r in run_results]
    mwh_vals = [r["energy"]["mwh_per_token"] for r in run_results if r["energy"]["mwh_per_token"]]

    return {
        "mode": "batch",
        "model_key": model_key,
        "model_label": model["label"],
        "model_params": model["params"],
        "task_key": task_key,
        "task_label": task["label"],
        "prompt": effective_prompt,
        "warm": warm,
        "repeats": repeats,
        "runs": run_results,
        "aggregate": {
            "delta_e_wh_mean": _mean(e_vals),
            "delta_e_wh_stddev": _stddev(e_vals),
            "tokens_per_sec_mean": _mean(tps_vals),
            "mwh_per_token_mean": _mean(mwh_vals),
            "mwh_per_token_stddev": _stddev(mwh_vals),
        },
        "thermals": {
            "cpu_base": sensors_base.get("cpu_tctl"),
            "gpu_base": sensors_base.get("gpu_junction"),
            "cpu_end": sensors_end.get("cpu_tctl"),
            "gpu_end": sensors_end.get("gpu_junction"),
        },
        "scope": "Device layer only (GoS1). Network and CPE excluded. No amortised training cost.",
    }
