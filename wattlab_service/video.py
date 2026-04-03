import asyncio
import subprocess
import time
import json
from pathlib import Path
from dotenv import dotenv_values
from tapo import ApiClient

config = dotenv_values("/home/gos/wattlab/.env")
UPLOAD_DIR = Path("/tmp/wattlab_uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
LOCK_FILE = Path("/tmp/gos-measure.lock")


# Services to pause during measurement
FOCUS_MODE_UNITS = [
    "sysstat-collect.timer",
    "anacron.timer",
    "fwupd-refresh.timer",
    "apt-daily.timer",
    "apt-daily-upgrade.timer",
    "man-db.timer",
    "motd-news.timer",
    "update-notifier-download.timer",
]

def focus_mode_enter():
    """Stop background timers before measurement."""
    stopped = []
    for unit in FOCUS_MODE_UNITS:
        result = subprocess.run(
            ["sudo", "systemctl", "stop", unit],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            stopped.append(unit)
    return stopped

def focus_mode_exit(stopped: list):
    """Restart background timers after measurement — run in parallel."""
    import concurrent.futures
    def start_unit(unit):
        subprocess.run(["sudo", "systemctl", "start", unit],
                      capture_output=True, text=True)
    with concurrent.futures.ThreadPoolExecutor() as ex:
        list(ex.map(start_unit, stopped))

PRESETS = {
    "cpu": {
        "label": "CPU encode",
        "detail": "libx264 · CRF 23 · 1080p · 24 cores",
        "cmd": lambda i, o: [
            "ffmpeg", "-y", "-i", str(i),
            "-c:v", "libx264", "-crf", "23",
            "-vf", "scale=-2:1080",
            "-c:a", "aac", "-b:a", "128k",
            str(o)
        ]
    },
    "gpu": {
        "label": "GPU encode",
        "detail": "h264_vaapi · QP 23 · 1080p · AMD RX 7800 XT",
        "cmd": lambda i, o: [
            "ffmpeg", "-y",
            "-vaapi_device", "/dev/dri/renderD128",
            "-i", str(i),
            "-vf", "scale=-2:1080,format=nv12,hwupload",
            "-c:v", "h264_vaapi", "-qp", "23",
            "-c:a", "aac", "-b:a", "128k",
            str(o)
        ]
    }
}

POLL_INTERVAL = 1.0

# --- Sensors ---

def read_sensors() -> dict:
    try:
        result = subprocess.run(['sensors', '-j'], capture_output=True, text=True)
        data = json.loads(result.stdout)
        return {
            "cpu_tctl": data['k10temp-pci-00c3']['Tctl']['temp1_input'],
            "gpu_junction": data['amdgpu-pci-0300']['junction']['temp2_input'],
            "gpu_ppt_w": data['amdgpu-pci-0300']['PPT']['power1_average'],
        }
    except Exception as e:
        return {"cpu_tctl": None, "gpu_junction": None, "gpu_ppt_w": None, "error": str(e)}

# --- P110 ---

async def get_power_watts() -> float:
    client = ApiClient(config["TAPO_EMAIL"], config["TAPO_PASSWORD"])
    device = await client.p110(config["TAPO_P110_IP"])
    result = await device.get_energy_usage()
    return result.current_power / 1000

async def measure_baseline(polls: int = 10) -> dict:
    power_readings = []
    sensor_readings = []
    for _ in range(polls):
        power_readings.append(await get_power_watts())
        sensor_readings.append(read_sensors())
        await asyncio.sleep(POLL_INTERVAL)
    return {
        "w_base": round(sum(power_readings) / len(power_readings), 2),
        "cpu_temp_base": round(sum(s["cpu_tctl"] for s in sensor_readings
                                   if s["cpu_tctl"]) / len(sensor_readings), 1),
        "gpu_temp_base": round(sum(s["gpu_junction"] for s in sensor_readings
                                   if s["gpu_junction"]) / len(sensor_readings), 1),
    }

async def poll_during_task(stop_event: asyncio.Event) -> list:
    readings = []
    while not stop_event.is_set():
        watts = await get_power_watts()
        sensors = read_sensors()
        readings.append({
            "t": time.time(),
            "watts": watts,
            **sensors
        })
        await asyncio.sleep(POLL_INTERVAL)
    return readings

# --- Confidence ---

def confidence(delta_w: float, poll_count: int) -> dict:
    if delta_w > 5 and poll_count >= 10:
        return {"flag": "🟢", "label": "Repeatable"}
    elif delta_w >= 2 or poll_count >= 5:
        return {"flag": "🟡", "label": "Early insight"}
    else:
        return {"flag": "🔴", "label": "Need more data — delta near P110 noise floor"}

# --- ffmpeg ---

def transcode(cmd: list) -> dict:
    # nice -n -5 gives ffmpeg elevated CPU scheduling priority
    cmd = ["nice", "-n", "-5"] + cmd
    t_start = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True)
    t_end = time.time()
    return {
        "success": result.returncode == 0,
        "duration_s": round(t_end - t_start, 1),
        "stderr": result.stderr[-500:] if result.returncode != 0 else ""
    }

# --- Single run ---

async def run_single(input_path: Path, job_id: str, preset_key: str,
                     baseline: dict) -> dict:
    preset = PRESETS[preset_key]
    output_path = UPLOAD_DIR / f"{job_id}_{preset_key}_out.mp4"

    stop_event = asyncio.Event()
    t_start = time.time()

    cmd = preset["cmd"](input_path, output_path)
    poll_task = asyncio.create_task(poll_during_task(stop_event))
    transcode_result = await asyncio.get_event_loop().run_in_executor(
        None, transcode, cmd
    )

    t_end = time.time()
    stop_event.set()
    readings = await poll_task

    delta_t = round(t_end - t_start, 1)
    w_base = baseline["w_base"]
    w_task = sum(r["watts"] for r in readings) / len(readings) if readings else w_base
    delta_w = round(w_task - w_base, 2)
    delta_e_wh = round(delta_w * (delta_t / 3600), 4)
    conf = confidence(delta_w, len(readings))

    cpu_temps = [r["cpu_tctl"] for r in readings if r.get("cpu_tctl")]
    gpu_temps = [r["gpu_junction"] for r in readings if r.get("gpu_junction")]
    gpu_ppts = [r["gpu_ppt_w"] for r in readings if r.get("gpu_ppt_w")]

    out_size_mb = round(output_path.stat().st_size / 1024 / 1024, 2) \
        if transcode_result["success"] and output_path.exists() else None

    return {
        "preset_key": preset_key,
        "preset_label": preset["label"],
        "preset_detail": preset["detail"],
        "transcode": transcode_result,
        "output_size_mb": out_size_mb,
        "energy": {
            "w_base": round(w_base, 2),
            "w_task": round(w_task, 2),
            "delta_w": round(delta_w, 2),
            "delta_t_s": delta_t,
            "delta_e_wh": delta_e_wh,
            "poll_count": len(readings),
            "confidence": conf,
        },
        "thermals": {
            "cpu_base": baseline["cpu_temp_base"],
            "cpu_peak": round(max(cpu_temps), 1) if cpu_temps else None,
            "cpu_mean": round(sum(cpu_temps) / len(cpu_temps), 1) if cpu_temps else None,
            "gpu_base": baseline["gpu_temp_base"],
            "gpu_peak": round(max(gpu_temps), 1) if gpu_temps else None,
            "gpu_mean": round(sum(gpu_temps) / len(gpu_temps), 1) if gpu_temps else None,
            "gpu_ppt_mean_w": round(sum(gpu_ppts) / len(gpu_ppts), 1) if gpu_ppts else None,
            "gpu_ppt_peak_w": round(max(gpu_ppts), 1) if gpu_ppts else None,
        }
    }

# --- Analysis ---

def analyse(cpu: dict, gpu: dict) -> dict:
    ce = cpu["energy"]
    ge = gpu["energy"]
    ct = cpu["thermals"]
    gt = gpu["thermals"]

    energy_winner = "CPU" if ce["delta_e_wh"] < ge["delta_e_wh"] else "GPU"
    speed_winner = "CPU" if ce["delta_t_s"] < ge["delta_t_s"] else "GPU"
    speed_winner = "CPU" if ce["delta_t_s"] < ge["delta_t_s"] else "GPU"

    energy_diff_pct = round(abs(ce["delta_e_wh"] - ge["delta_e_wh"]) /
                            max(ce["delta_e_wh"], ge["delta_e_wh"]) * 100, 1)
    speed_diff_pct = round(abs(ce["delta_t_s"] - ge["delta_t_s"]) /
                           max(ce["delta_t_s"], ge["delta_t_s"]) * 100, 1)

    finding = (
        f"{energy_winner} used {energy_diff_pct}% less energy "
        f"({'%.4f' % ce['delta_e_wh']} vs {'%.4f' % ge['delta_e_wh']} Wh). "
        f"{speed_winner} was {speed_diff_pct}% faster "
        f"({ce['delta_t_s']}s vs {ge['delta_t_s']}s). "
    )

    if energy_winner != speed_winner:
        finding += (
            f"The faster encoder used more total energy — "
            f"higher peak draw ({ge['delta_w'] if speed_winner == 'GPU' else ce['delta_w']}W) "
            f"outweighed the time saving."
        )
    else:
        finding += f"{energy_winner} was both faster and more energy-efficient on this workload."

    # thermal note
    if ct["cpu_peak"] and gt["gpu_peak"]:
        finding += (
            f" CPU peaked at {ct['cpu_peak']}°C (Tctl), "
            f"GPU junction at {gt['gpu_peak']}°C."
        )

    # PPT cross-check
    if gt["gpu_ppt_mean_w"]:
        finding += (
            f" GPU self-reported mean power (PPT): {gt['gpu_ppt_mean_w']}W "
            f"— cross-check against P110 delta ({ge['delta_w']}W total system delta)."
        )

    conf_both = ce["confidence"]["flag"] == "🟢" and ge["confidence"]["flag"] == "🟢"
    confidence_note = "Both runs 🟢 Repeatable." if conf_both else \
        "⚠ One or both runs below Repeatable threshold — treat comparison as Early insight."

    return {
        "energy_winner": energy_winner,
        "speed_winner": speed_winner,
        "energy_diff_pct": energy_diff_pct,
        "speed_diff_pct": speed_diff_pct,
        "finding": finding,
        "confidence_note": confidence_note,
    }

# --- Main entry points ---

async def run_video_measurement(input_path: Path, job_id: str,
                                preset_key: str) -> dict:
    stopped = focus_mode_enter()
    baseline = await measure_baseline(polls=10)
    LOCK_FILE.write_text(job_id)
    try:
        result = await run_single(input_path, job_id, preset_key, baseline)
    finally:
        LOCK_FILE.unlink(missing_ok=True)
        # Run focus_mode_exit in executor so it doesn't block event loop
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, focus_mode_exit, stopped)
    return {
        "mode": "single",
        "job_id": job_id,
        "baseline": baseline,
        "result": result,
        "scope": "Device layer only (GoS1 server). Network, CDN, CPE excluded.",
    }

async def run_both_measurement(input_path: Path, job_id: str, jobs: dict = None) -> dict:
    if jobs is not None: jobs[job_id]["stage"] = "baseline"
    stopped = focus_mode_enter()
    baseline = await measure_baseline(polls=10)
    LOCK_FILE.write_text(job_id)
    try:
        if jobs is not None: jobs[job_id]["stage"] = "cpu_encode"
        cpu_result = await run_single(input_path, job_id, "cpu", baseline)
        if jobs is not None: jobs[job_id]["stage"] = "rest"
        # rest between runs — 60s to allow CPU thermals to stabilise
        await asyncio.sleep(60)
        if jobs is not None: jobs[job_id]["stage"] = "baseline_2"
        # fresh baseline for GPU
        gpu_baseline = await measure_baseline(polls=10)
        if jobs is not None: jobs[job_id]["stage"] = "gpu_encode"
        gpu_result = await run_single(input_path, job_id, "gpu", gpu_baseline)
    finally:
        LOCK_FILE.unlink(missing_ok=True)
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, focus_mode_exit, stopped)

    analysis = analyse(cpu_result, gpu_result)

    return {
        "mode": "both",
        "job_id": job_id,
        "cpu": cpu_result,
        "gpu": gpu_result,
        "analysis": analysis,
        "scope": "Device layer only (GoS1 server). Network, CDN, CPE excluded.",
    }

async def run_video_measurement_path(path: str, job_id: str, preset_key: str) -> dict:
    """Run measurement on an already-present file (pre-loaded content)."""
    return await run_video_measurement(Path(path), job_id, preset_key)

async def run_both_measurement_path(path: str, job_id: str) -> dict:
    """Run both measurements on an already-present file (pre-loaded content)."""
    return await run_both_measurement(Path(path), job_id)
