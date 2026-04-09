import json
from pathlib import Path

SETTINGS_FILE = Path("/home/gos/wattlab/settings.json")

DEFAULTS = {
    "baseline_polls": 10,
    "video_cooldown_s": 60,
    "llm_rest_s": 10,
    "llm_unload_settle_s": 3,
    # Confidence — poll count thresholds (kept)
    "conf_green_polls": 10,
    "conf_yellow_polls": 5,
    # Confidence — variance-based ΔW thresholds (replace old conf_*_delta_w)
    "variance_pct": 2.0,        # measured system variance as % of baseline power
    "variance_green_x": 5.0,    # 🟢  ΔW must exceed this × noise_w
    "variance_yellow_x": 2.0,   # 🟡  ΔW must exceed this × noise_w
    # Variance calibration outputs (written by calibration run, not user-edited)
    "variance_idle_pct": None,   # CV of raw idle P110 readings across all baseline periods
    "variance_cpu_pct": None,    # CV of ΔW across H264-CPU runs
    "variance_gpu_pct": None,    # CV of ΔW across H265-GPU runs
    # Variance calibration run parameters
    "variance_runs": 10,         # how many H264-CPU + H265-GPU pairs to run
    "variance_cooldown_s": 60,   # seconds between each run pair
    "variance_cpu_cmd": (
        "ffmpeg -y -i {input} -c:v libx264 -crf 23"
        " -vf scale=-2:1080 -c:a aac -b:a 128k {output}"
    ),
    "variance_gpu_cmd": (
        "ffmpeg -y -hwaccel vaapi -hwaccel_output_format vaapi"
        " -extra_hw_frames 32"
        " -vaapi_device /dev/dri/renderD128 -i {input}"
        " -vf scale_vaapi=-2:1080"
        " -c:v hevc_vaapi -qp 28 -c:a aac -b:a 128k {output}"
    ),
    "rag_corpus_path": "/home/gos/wattlab/corpus/papers",
    "rag_chroma_path": "/home/gos/wattlab/.chroma",
}


def load() -> dict:
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text())
            return {**DEFAULTS, **{k: data[k] for k in DEFAULTS if k in data}}
        except Exception:
            pass
    return dict(DEFAULTS)


def save(data: dict) -> dict:
    merged = {**DEFAULTS, **{k: data[k] for k in DEFAULTS if k in data}}
    SETTINGS_FILE.write_text(json.dumps(merged, indent=2))
    return merged
