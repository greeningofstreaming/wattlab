import asyncio
import shlex
import subprocess
import time
import json
from pathlib import Path
import settings as cfg
from power import get_power_watts
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
        "bitrate_key": "h264_bitrate_kbps",
        "detail_fn": lambda bps: f"libx264 · {bps} kbps ABR · 1080p · 24 cores",
        "cmd_fn": lambda i, o, bps: [
            "ffmpeg", "-y", "-i", str(i),
            "-c:v", "libx264", "-b:v", f"{bps}k",
            "-vf", "scale=-2:1080",
            "-c:a", "aac", "-b:a", "128k",
            str(o)
        ]
    },
    "gpu": {
        "label": "H.264 GPU",
        "bitrate_key": "h264_bitrate_kbps",
        "detail_fn": lambda bps: f"h264_vaapi · {bps} kbps ABR · 1080p · full pipeline",
        "cmd_fn": lambda i, o, bps: [
            "ffmpeg", "-y",
            "-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
            "-extra_hw_frames", "32",
            "-vaapi_device", "/dev/dri/renderD128",
            "-i", str(i),
            "-vf", "scale_vaapi=w=-2:h=1080:format=nv12",
            "-c:v", "h264_vaapi", "-b:v", f"{bps}k",
            "-c:a", "aac", "-b:a", "128k",
            str(o)
        ]
    },
    "h265_cpu": {
        "label": "H.265 CPU",
        "bitrate_key": "h265_bitrate_kbps",
        "detail_fn": lambda bps: f"libx265 · {bps} kbps ABR · 1080p · 24 cores",
        "cmd_fn": lambda i, o, bps: [
            "ffmpeg", "-y", "-i", str(i),
            "-c:v", "libx265", "-b:v", f"{bps}k",
            "-vf", "scale=-2:1080",
            "-c:a", "aac", "-b:a", "128k",
            str(o)
        ]
    },
    "h265_gpu": {
        "label": "H.265 GPU",
        "bitrate_key": "h265_bitrate_kbps",
        "detail_fn": lambda bps: f"hevc_vaapi · {bps} kbps ABR · 1080p · full pipeline",
        "cmd_fn": lambda i, o, bps: [
            "ffmpeg", "-y",
            "-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
            "-extra_hw_frames", "32",
            "-vaapi_device", "/dev/dri/renderD128",
            "-i", str(i),
            "-vf", "scale_vaapi=w=-2:h=1080:format=nv12",
            "-c:v", "hevc_vaapi", "-b:v", f"{bps}k",
            "-c:a", "aac", "-b:a", "128k",
            str(o)
        ]
    },
    "av1_cpu": {
        "label": "AV1 CPU",
        "bitrate_key": "av1_bitrate_kbps",
        "detail_fn": lambda bps: f"libsvtav1 · {bps} kbps ABR · 1080p · 24 cores",
        "cmd_fn": lambda i, o, bps: [
            "ffmpeg", "-y", "-i", str(i),
            "-c:v", "libsvtav1", "-b:v", f"{bps}k",
            "-vf", "scale=-2:1080",
            "-c:a", "aac", "-b:a", "128k",
            str(o)
        ]
    },
    "av1_gpu": {
        "label": "AV1 GPU",
        "bitrate_key": "av1_bitrate_kbps",
        "detail_fn": lambda bps: f"av1_vaapi · {bps} kbps ABR · 1080p · full pipeline",
        "cmd_fn": lambda i, o, bps: [
            "ffmpeg", "-y",
            "-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
            "-extra_hw_frames", "32",
            "-vaapi_device", "/dev/dri/renderD128",
            "-i", str(i),
            "-vf", "scale_vaapi=w=-2:h=1080:format=nv12",
            "-c:v", "av1_vaapi", "-b:v", f"{bps}k",
            "-c:a", "aac", "-b:a", "128k",
            str(o)
        ]
    },
}

def _preset_bps(preset_key: str, s: dict) -> int:
    return int(s.get(PRESETS[preset_key]["bitrate_key"], 4000))

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
        result = {"flag": "🟡", "label": "Early insight"}
        if delta_w > green_thresh and poll_count < s["conf_green_polls"]:
            ratio = int(round(delta_w / noise_w))
            result["hint"] = (f"Strong signal ({ratio}× noise floor) — task too short for 🟢. "
                              f"Use a longer clip or batch mode.")
        return result
    else:
        return {"flag": "🔴", "label": "Need more data"}

# --- ffmpeg ---

def build_preset_cmd(preset_key: str, input_path, output_path) -> list:
    """Return the ffmpeg command list for a preset (no nice prefix)."""
    s = cfg.load()
    bps = _preset_bps(preset_key, s)
    return PRESETS[preset_key]["cmd_fn"](Path(input_path), Path(output_path), bps)


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
    s = cfg.load()
    preset = PRESETS[preset_key]
    bps = _preset_bps(preset_key, s)
    output_path = UPLOAD_DIR / f"{job_id}_{preset_key}_out.mp4"

    stop_event = asyncio.Event()
    t_start = time.time()

    if custom_cmd and custom_cmd.strip():
        cmd = apply_custom_cmd(custom_cmd, input_path, output_path)
    else:
        cmd = preset["cmd_fn"](input_path, output_path, bps)
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
        if output_path.exists() and output_path.stat().st_size > 0 else None

    return {
        "preset_key": preset_key,
        "preset_label": preset["label"],
        "preset_detail": preset["detail_fn"](bps),
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

def analyse_all(codecs: dict) -> dict:
    """Cross-codec summary for all-6 result."""
    flat = []
    for codec_name, data in codecs.items():
        for side in ("cpu", "gpu"):
            r = data.get(side, {})
            e = r.get("energy", {})
            flat.append({
                "label": r.get("preset_label", ""),
                "codec": codec_name,
                "side": side,
                "delta_e_wh": e.get("delta_e_wh"),
                "delta_t_s": e.get("delta_t_s"),
                "output_size_mb": r.get("output_size_mb"),
                "confidence_flag": e.get("confidence", {}).get("flag"),
            })
    valid = [r for r in flat if r["delta_e_wh"] is not None]
    most_efficient = min(valid, key=lambda r: r["delta_e_wh"]) if valid else None
    fastest        = min(valid, key=lambda r: r["delta_t_s"])  if valid else None
    codec_summaries = {
        codec_name: {
            "energy_winner":  data["analysis"]["energy_winner"],
            "speed_winner":   data["analysis"]["speed_winner"],
            "energy_diff_pct": data["analysis"]["energy_diff_pct"],
            "speed_diff_pct":  data["analysis"]["speed_diff_pct"],
        }
        for codec_name, data in codecs.items() if "analysis" in data
    }
    return {
        "most_efficient": most_efficient,
        "fastest": fastest,
        "codec_summaries": codec_summaries,
    }


async def run_all_measurement(input_path: Path, job_id: str, jobs: dict = None) -> dict:
    """Run all 6 presets (H.264 / H.265 / AV1 × CPU / GPU) in codec pairs."""
    s = cfg.load()
    codec_pairs = [
        ("h264", "cpu",      "gpu"),
        ("h265", "h265_cpu", "h265_gpu"),
        ("av1",  "av1_cpu",  "av1_gpu"),
    ]
    stopped = focus_mode_enter()
    LOCK_FILE.write_text(job_id)
    results = {}
    try:
        for idx, (codec_name, cpu_key, gpu_key) in enumerate(codec_pairs):
            if jobs: jobs[job_id]["stage"] = f"{codec_name}_cpu_baseline"
            base_cpu = await measure_baseline(polls=s["baseline_polls"])
            if jobs: jobs[job_id]["stage"] = f"{codec_name}_cpu_encode"
            cpu_result = await run_single(input_path, job_id, cpu_key, base_cpu)

            if jobs: jobs[job_id]["stage"] = f"{codec_name}_rest"
            await asyncio.sleep(s["video_cooldown_s"])

            if jobs: jobs[job_id]["stage"] = f"{codec_name}_gpu_baseline"
            base_gpu = await measure_baseline(polls=s["baseline_polls"])
            if jobs: jobs[job_id]["stage"] = f"{codec_name}_gpu_encode"
            gpu_result = await run_single(input_path, job_id, gpu_key, base_gpu)

            results[codec_name] = {
                "cpu": cpu_result,
                "gpu": gpu_result,
                "analysis": analyse(cpu_result, gpu_result),
            }
            if idx < len(codec_pairs) - 1:
                if jobs: jobs[job_id]["stage"] = f"{codec_name}_inter_rest"
                await asyncio.sleep(s["video_cooldown_s"])
    finally:
        LOCK_FILE.unlink(missing_ok=True)
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, focus_mode_exit, stopped)

    if jobs: jobs[job_id]["stage"] = "done"
    return {
        "mode": "all_codecs",
        "job_id": job_id,
        "codecs": results,
        "analysis": analyse_all(results),
        "scope": "Device layer only (GoS1 server). Network, CDN, CPE excluded.",
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
                               custom_cmd_gpu: str = None,
                               preset_cpu: str = "cpu",
                               preset_gpu: str = "gpu") -> dict:
    s = cfg.load()
    if jobs is not None: jobs[job_id]["stage"] = "baseline"
    stopped = focus_mode_enter()
    baseline = await measure_baseline(polls=s["baseline_polls"])
    LOCK_FILE.write_text(job_id)
    try:
        if jobs is not None: jobs[job_id]["stage"] = "cpu_encode"
        cpu_result = await run_single(input_path, job_id, preset_cpu, baseline, custom_cmd_cpu)
        if jobs is not None: jobs[job_id]["stage"] = "rest"
        await asyncio.sleep(s["video_cooldown_s"])
        if jobs is not None: jobs[job_id]["stage"] = "baseline_2"
        gpu_baseline = await measure_baseline(polls=s["baseline_polls"])
        if jobs is not None: jobs[job_id]["stage"] = "gpu_encode"
        gpu_result = await run_single(input_path, job_id, preset_gpu, gpu_baseline, custom_cmd_gpu)
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
                                    custom_cmd_gpu: str = None,
                                    preset_cpu: str = "cpu",
                                    preset_gpu: str = "gpu") -> dict:
    """Run both measurements on an already-present file (pre-loaded content)."""
    return await run_both_measurement(Path(path), job_id,
                                      custom_cmd_cpu=custom_cmd_cpu,
                                      custom_cmd_gpu=custom_cmd_gpu,
                                      preset_cpu=preset_cpu,
                                      preset_gpu=preset_gpu)


# --- Variance calibration ---

def _cv(values: list) -> float | None:
    """Coefficient of variation as %, sample std dev / mean × 100."""
    n = len(values)
    if n < 2:
        return None
    m = sum(values) / n
    if abs(m) < 0.001:
        return None
    std = (sum((v - m) ** 2 for v in values) / (n - 1)) ** 0.5
    return round(std / abs(m) * 100, 2)


async def run_variance_calibration(job_id: str, jobs: dict) -> dict:
    """Run H264-CPU then H265-GPU on Meridian N times.
    Computes three separate CVs:
      idle  — raw P110 readings during all baseline periods (instrument + background noise)
      cpu   — ΔW per H264-CPU run (run-to-run reproducibility)
      gpu   — ΔW per H265-GPU run
    Updates variance_idle_pct, variance_cpu_pct, variance_gpu_pct, and
    sets variance_pct = mean of the three in settings.json.
    """
    s = cfg.load()
    meridian = Path("/home/gos/wattlab/test_content/meridian_4k.mp4")
    n_runs = int(s["variance_runs"])
    cooldown = float(s["variance_cooldown_s"])
    n_base = int(s["baseline_polls"])
    cpu_tpl = s["variance_cpu_cmd"]
    gpu_tpl = s["variance_gpu_cmd"]

    stopped = focus_mode_enter()
    idle_readings: list[float] = []   # raw P110 watts during all baselines
    cpu_delta_w: list[float] = []     # ΔW per CPU run
    gpu_delta_w: list[float] = []     # ΔW per GPU run

    LOCK_FILE.write_text(job_id)
    try:
        for i in range(n_runs):
            run_label = f"{i + 1}/{n_runs}"

            def _stage(s):
                if jobs:
                    jobs[job_id]["stage"] = f"run {run_label} — {s}"

            # --- CPU baseline (collect raw readings for idle CV) ---
            _stage("CPU baseline")
            raw_cpu_base = []
            for _ in range(n_base):
                w = await get_power_watts()
                raw_cpu_base.append(w)
                idle_readings.append(w)
                await asyncio.sleep(POLL_INTERVAL)
            w_base_cpu = sum(raw_cpu_base) / len(raw_cpu_base)

            # --- CPU encode ---
            out_cpu = UPLOAD_DIR / f"{job_id}_var_cpu_{i}.mp4"
            cmd_cpu = apply_custom_cmd(cpu_tpl, meridian, out_cpu)
            _stage("H.264 CPU encode")
            stop_cpu = asyncio.Event()
            poll_cpu = asyncio.create_task(poll_during_task(stop_cpu))
            await asyncio.get_event_loop().run_in_executor(None, transcode, cmd_cpu)
            stop_cpu.set()
            readings_cpu = await poll_cpu
            out_cpu.unlink(missing_ok=True)
            if readings_cpu:
                w_task = sum(r["watts"] for r in readings_cpu) / len(readings_cpu)
                cpu_delta_w.append(round(w_task - w_base_cpu, 3))

            # --- Cooldown between CPU and GPU ---
            _stage("cooldown")
            await asyncio.sleep(cooldown)

            # --- GPU baseline (collect raw readings for idle CV) ---
            _stage("GPU baseline")
            raw_gpu_base = []
            for _ in range(n_base):
                w = await get_power_watts()
                raw_gpu_base.append(w)
                idle_readings.append(w)
                await asyncio.sleep(POLL_INTERVAL)
            w_base_gpu = sum(raw_gpu_base) / len(raw_gpu_base)

            # --- GPU encode ---
            out_gpu = UPLOAD_DIR / f"{job_id}_var_gpu_{i}.mp4"
            cmd_gpu = apply_custom_cmd(gpu_tpl, meridian, out_gpu)
            _stage("H.265 GPU encode")
            stop_gpu = asyncio.Event()
            poll_gpu = asyncio.create_task(poll_during_task(stop_gpu))
            await asyncio.get_event_loop().run_in_executor(None, transcode, cmd_gpu)
            stop_gpu.set()
            readings_gpu = await poll_gpu
            out_gpu.unlink(missing_ok=True)
            if readings_gpu:
                w_task2 = sum(r["watts"] for r in readings_gpu) / len(readings_gpu)
                gpu_delta_w.append(round(w_task2 - w_base_gpu, 3))

            # --- Inter-run cooldown (skip after last run) ---
            if i < n_runs - 1:
                _stage("inter-run cooldown")
                await asyncio.sleep(cooldown)

    finally:
        LOCK_FILE.unlink(missing_ok=True)
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, focus_mode_exit, stopped)

    if jobs:
        jobs[job_id]["stage"] = "computing"

    idle_cv = _cv(idle_readings)
    cpu_cv  = _cv(cpu_delta_w)
    gpu_cv  = _cv(gpu_delta_w)
    available = [v for v in [idle_cv, cpu_cv, gpu_cv] if v is not None]
    mean_cv = round(sum(available) / len(available), 2) if available else None

    result = {
        "runs_completed": len(cpu_delta_w),
        "idle_readings_n": len(idle_readings),
        "cpu_delta_w_values": cpu_delta_w,
        "gpu_delta_w_values": gpu_delta_w,
        "variance_idle_pct": idle_cv,
        "variance_cpu_pct":  cpu_cv,
        "variance_gpu_pct":  gpu_cv,
        "variance_mean_pct": mean_cv,
        "variance_updated": False,
    }

    if mean_cv is not None:
        current = cfg.load()
        current["variance_idle_pct"] = idle_cv
        current["variance_cpu_pct"]  = cpu_cv
        current["variance_gpu_pct"]  = gpu_cv
        current["variance_pct"]      = mean_cv
        cfg.save(current)
        result["variance_updated"] = True

    if jobs:
        jobs[job_id]["stage"] = "done"
        jobs[job_id]["variance_result"] = result

    return result
