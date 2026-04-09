import asyncio
import shlex
import subprocess
import time
import json
from pathlib import Path
from dotenv import dotenv_values
from tapo import ApiClient
import settings as cfg

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
        "label": "H.264 CPU",
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
        "label": "H.264 GPU",
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
    },
    "h265_cpu": {
        "label": "H.265 CPU",
        "detail": "libx265 · CRF 28 · 1080p · 24 cores",
        "cmd": lambda i, o: [
            "ffmpeg", "-y", "-i", str(i),
            "-c:v", "libx265", "-crf", "28",
            "-vf", "scale=-2:1080",
            "-c:a", "aac", "-b:a", "128k",
            str(o)
        ]
    },
    "h265_gpu": {
        "label": "H.265 GPU",
        "detail": "hevc_vaapi · QP 28 · 1080p · AMD RX 7800 XT",
        "cmd": lambda i, o: [
            "ffmpeg", "-y",
            "-vaapi_device", "/dev/dri/renderD128",
            "-i", str(i),
            "-vf", "scale=-2:1080,format=nv12,hwupload",
            "-c:v", "hevc_vaapi", "-qp", "28",
            "-c:a", "aac", "-b:a", "128k",
            str(o)
        ]
    },
    "av1_cpu": {
        "label": "AV1 CPU",
        "detail": "libsvtav1 · CRF 30 · 1080p · 24 cores",
        "cmd": lambda i, o: [
            "ffmpeg", "-y", "-i", str(i),
            "-c:v", "libsvtav1", "-crf", "30",
            "-vf", "scale=-2:1080",
            "-c:a", "aac", "-b:a", "128k",
            str(o)
        ]
    },
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
    for attempt in range(3):
        try:
            client = ApiClient(config["TAPO_EMAIL"], config["TAPO_PASSWORD"])
            device = await client.p110(config["TAPO_P110_IP"])
            result = await device.get_energy_usage()
            return result.current_power / 1000
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(1)

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

def confidence(delta_w: float, poll_count: int, w_base: float) -> dict:
    """Variance-based confidence: noise_w = variance_pct/100 * w_base.
    Green if ΔW > green_x × noise_w AND polls ≥ green_polls.
    Yellow if ΔW ≥ yellow_x × noise_w OR polls ≥ yellow_polls.
    Red otherwise.
    """
    s = cfg.load()
    noise_w = s["variance_pct"] / 100.0 * max(w_base, 1.0)
    green_thresh = s["variance_green_x"] * noise_w
    yellow_thresh = s["variance_yellow_x"] * noise_w
    if delta_w > green_thresh and poll_count >= s["conf_green_polls"]:
        return {"flag": "🟢", "label": "Repeatable"}
    elif delta_w >= yellow_thresh or poll_count >= s["conf_yellow_polls"]:
        return {"flag": "🟡", "label": "Early insight"}
    else:
        return {"flag": "🔴", "label": "Need more data"}

# --- ffmpeg ---

def build_preset_cmd(preset_key: str, input_path, output_path) -> list:
    """Return the ffmpeg command list for a preset (no nice prefix)."""
    return PRESETS[preset_key]["cmd"](Path(input_path), Path(output_path))


def apply_custom_cmd(custom_cmd: str, input_path, output_path) -> list:
    """Substitute {input}/{output} placeholders and shlex-split a custom command string."""
    cmd_str = custom_cmd.replace("{input}", str(input_path)).replace("{output}", str(output_path))
    return shlex.split(cmd_str)


def transcode(cmd: list) -> dict:
    # nice -n -5 gives ffmpeg elevated CPU scheduling priority
    cmd = ["nice", "-n", "-5"] + cmd
    t_start = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True)
    t_end = time.time()
    return {
        "success": result.returncode == 0,
        "duration_s": round(t_end - t_start, 1),
        "ffmpeg_cmd": " ".join(cmd),
        "stderr": result.stderr[-500:] if result.returncode != 0 else ""
    }

# --- Single run ---

async def run_single(input_path: Path, job_id: str, preset_key: str,
                     baseline: dict, custom_cmd: str = None) -> dict:
    preset = PRESETS[preset_key]
    output_path = UPLOAD_DIR / f"{job_id}_{preset_key}_out.mp4"

    stop_event = asyncio.Event()
    t_start = time.time()

    if custom_cmd and custom_cmd.strip():
        cmd = apply_custom_cmd(custom_cmd, input_path, output_path)
    else:
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
    conf = confidence(delta_w, len(readings), w_base)

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
                                preset_key: str, jobs: dict = None,
                                custom_cmd: str = None) -> dict:
    s = cfg.load()
    if jobs is not None: jobs[job_id]["stage"] = "baseline"
    stopped = focus_mode_enter()
    baseline = await measure_baseline(polls=s["baseline_polls"])
    LOCK_FILE.write_text(job_id)
    try:
        if jobs is not None: jobs[job_id]["stage"] = f"{preset_key}_encode"
        result = await run_single(input_path, job_id, preset_key, baseline, custom_cmd)
        if jobs is not None: jobs[job_id]["stage"] = "done"
    finally:
        LOCK_FILE.unlink(missing_ok=True)
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, focus_mode_exit, stopped)
    return {
        "mode": "single",
        "job_id": job_id,
        "baseline": baseline,
        "result": result,
        "scope": "Device layer only (GoS1 server). Network, CDN, CPE excluded.",
    }

async def run_both_measurement(input_path: Path, job_id: str, jobs: dict = None,
                               custom_cmd_cpu: str = None,
                               custom_cmd_gpu: str = None) -> dict:
    s = cfg.load()
    if jobs is not None: jobs[job_id]["stage"] = "baseline"
    stopped = focus_mode_enter()
    baseline = await measure_baseline(polls=s["baseline_polls"])
    LOCK_FILE.write_text(job_id)
    try:
        if jobs is not None: jobs[job_id]["stage"] = "cpu_encode"
        cpu_result = await run_single(input_path, job_id, "cpu", baseline, custom_cmd_cpu)
        if jobs is not None: jobs[job_id]["stage"] = "rest"
        await asyncio.sleep(s["video_cooldown_s"])
        if jobs is not None: jobs[job_id]["stage"] = "baseline_2"
        gpu_baseline = await measure_baseline(polls=s["baseline_polls"])
        if jobs is not None: jobs[job_id]["stage"] = "gpu_encode"
        gpu_result = await run_single(input_path, job_id, "gpu", gpu_baseline, custom_cmd_gpu)
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

async def run_video_measurement_path(path: str, job_id: str, preset_key: str,
                                      custom_cmd: str = None) -> dict:
    """Run measurement on an already-present file (pre-loaded content)."""
    return await run_video_measurement(Path(path), job_id, preset_key,
                                       custom_cmd=custom_cmd)

async def run_both_measurement_path(path: str, job_id: str,
                                    custom_cmd_cpu: str = None,
                                    custom_cmd_gpu: str = None) -> dict:
    """Run both measurements on an already-present file (pre-loaded content)."""
    return await run_both_measurement(Path(path), job_id,
                                      custom_cmd_cpu=custom_cmd_cpu,
                                      custom_cmd_gpu=custom_cmd_gpu)


# --- Variance calibration ---

async def run_variance_calibration(job_id: str, jobs: dict) -> dict:
    """Run H264-CPU then H265-GPU on Meridian N times, compute coefficient of
    variation across all ΔW readings, and update variance_pct in settings."""
    s = cfg.load()
    meridian = Path("/home/gos/wattlab/test_content/meridian_4k.mp4")
    n_runs = int(s["variance_runs"])
    cooldown = float(s["variance_cooldown_s"])
    cpu_tpl = s["variance_cpu_cmd"]
    gpu_tpl = s["variance_gpu_cmd"]

    stopped = focus_mode_enter()
    delta_w_values: list[float] = []

    LOCK_FILE.write_text(job_id)
    try:
        for i in range(n_runs):
            run_label = f"{i + 1}/{n_runs}"

            # --- CPU pass ---
            if jobs:
                jobs[job_id]["stage"] = f"run_{run_label}_cpu_baseline"
            base_cpu = await measure_baseline(polls=s["baseline_polls"])
            w_base_cpu = base_cpu["w_base"]

            out_cpu = UPLOAD_DIR / f"{job_id}_var_cpu_{i}.mp4"
            cmd_cpu = apply_custom_cmd(cpu_tpl, meridian, out_cpu)

            if jobs:
                jobs[job_id]["stage"] = f"run_{run_label}_cpu_encode"
            stop_cpu = asyncio.Event()
            poll_cpu = asyncio.create_task(poll_during_task(stop_cpu))
            await asyncio.get_event_loop().run_in_executor(None, transcode, cmd_cpu)
            stop_cpu.set()
            readings_cpu = await poll_cpu
            out_cpu.unlink(missing_ok=True)

            if readings_cpu:
                w_task = sum(r["watts"] for r in readings_cpu) / len(readings_cpu)
                delta_w_values.append(round(w_task - w_base_cpu, 3))

            # --- Cooldown between CPU and GPU ---
            if jobs:
                jobs[job_id]["stage"] = f"run_{run_label}_cooldown"
            await asyncio.sleep(cooldown)

            # --- GPU pass ---
            if jobs:
                jobs[job_id]["stage"] = f"run_{run_label}_gpu_baseline"
            base_gpu = await measure_baseline(polls=s["baseline_polls"])
            w_base_gpu = base_gpu["w_base"]

            out_gpu = UPLOAD_DIR / f"{job_id}_var_gpu_{i}.mp4"
            cmd_gpu = apply_custom_cmd(gpu_tpl, meridian, out_gpu)

            if jobs:
                jobs[job_id]["stage"] = f"run_{run_label}_gpu_encode"
            stop_gpu = asyncio.Event()
            poll_gpu = asyncio.create_task(poll_during_task(stop_gpu))
            await asyncio.get_event_loop().run_in_executor(None, transcode, cmd_gpu)
            stop_gpu.set()
            readings_gpu = await poll_gpu
            out_gpu.unlink(missing_ok=True)

            if readings_gpu:
                w_task2 = sum(r["watts"] for r in readings_gpu) / len(readings_gpu)
                delta_w_values.append(round(w_task2 - w_base_gpu, 3))

            # --- Inter-run cooldown (skip after last run) ---
            if i < n_runs - 1:
                if jobs:
                    jobs[job_id]["stage"] = f"run_{run_label}_inter_cooldown"
                await asyncio.sleep(cooldown)

    finally:
        LOCK_FILE.unlink(missing_ok=True)
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, focus_mode_exit, stopped)

    if jobs:
        jobs[job_id]["stage"] = "computing"

    # Compute coefficient of variation across all ΔW readings
    n = len(delta_w_values)
    cv_pct = None
    std_dw = None
    mean_dw = None
    if n >= 2:
        mean_dw = sum(delta_w_values) / n
        variance = sum((v - mean_dw) ** 2 for v in delta_w_values) / (n - 1)
        std_dw = variance ** 0.5
        cv_pct = round(std_dw / max(abs(mean_dw), 0.001) * 100, 2) if mean_dw else None

    result = {
        "runs_completed": n,
        "delta_w_values": delta_w_values,
        "mean_delta_w": round(mean_dw, 3) if mean_dw is not None else None,
        "std_delta_w": round(std_dw, 3) if std_dw is not None else None,
        "cv_pct": cv_pct,
        "variance_updated": False,
    }

    if cv_pct is not None:
        current = cfg.load()
        current["variance_pct"] = cv_pct
        cfg.save(current)
        result["variance_updated"] = True

    if jobs:
        jobs[job_id]["stage"] = "done"
        jobs[job_id]["variance_result"] = result

    return result
