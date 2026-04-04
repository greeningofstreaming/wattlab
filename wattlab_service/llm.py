import asyncio
import subprocess
import time
import json
import urllib.request
from pathlib import Path
from dotenv import dotenv_values
from tapo import ApiClient

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
    if delta_w > 5 and poll_count >= 10:
        return {"flag": "🟢", "label": "Repeatable"}
    elif delta_w >= 2 or poll_count >= 5:
        return {"flag": "🟡", "label": "Early insight"}
    else:
        return {"flag": "🔴", "label": "Need more data"}

# --- Ollama inference ---

def run_inference(model: str, prompt: str) -> dict:
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
    }).encode()

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    t_start = time.time()
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.loads(resp.read())
    t_end = time.time()

    return {
        "response": result.get("response", "")[:500],
        "prompt_tokens": result.get("prompt_eval_count", 0),
        "output_tokens": result.get("eval_count", 0),
        "total_tokens": result.get("prompt_eval_count", 0) + result.get("eval_count", 0),
        "duration_s": round(t_end - t_start, 2),
        "tokens_per_sec": round(result.get("eval_count", 0) / max(t_end - t_start, 0.1), 1),
    }

# --- Main measurement ---

async def run_llm_measurement(model_key: str, task_key: str,
                               jobs: dict = None, job_id: str = None) -> dict:
    model = MODELS[model_key]
    task = TASKS[task_key]

    if jobs and job_id: jobs[job_id]["stage"] = "baseline"
    w_base = await measure_baseline(polls=10)
    sensors_base = read_sensors()

    LOCK_FILE.write_text(job_id or "llm")

    stop_event = asyncio.Event()
    if jobs and job_id: jobs[job_id]["stage"] = "inference"

    poll_task = asyncio.create_task(poll_during_task(stop_event))
    inference_result = await asyncio.get_event_loop().run_in_executor(
        None, run_inference, model_key, task["prompt"]
    )
    stop_event.set()
    readings = await poll_task

    LOCK_FILE.unlink(missing_ok=True)
    if jobs and job_id: jobs[job_id]["stage"] = "done"

    sensors_end = read_sensors()

    delta_t = inference_result["duration_s"]
    w_task = sum(r[1] for r in readings) / len(readings) if readings else w_base
    delta_w = round(w_task - w_base, 2)
    delta_e_wh = round(delta_w * (delta_t / 3600), 4)
    output_tokens = inference_result["output_tokens"]
    mwh_per_token = round((delta_e_wh * 1000) / max(output_tokens, 1), 4) if output_tokens else None
    conf = confidence(delta_w, len(readings))

    return {
        "model_key": model_key,
        "model_label": model["label"],
        "model_params": model["params"],
        "task_key": task_key,
        "task_label": task["label"],
        "prompt": task["prompt"],
        "inference": inference_result,
        "energy": {
            "w_base": w_base,
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
        "scope": "Device layer only (GoS1). Network and CPE excluded. No amortised training cost.",
    }
