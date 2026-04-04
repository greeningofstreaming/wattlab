import csv
import io
import json
from datetime import datetime
from pathlib import Path

RESULTS_DIR = Path("/home/gos/wattlab/results")


def save_result(job_type: str, job_id: str, data: dict) -> Path:
    """Write a completed job result to results/{job_type}/{date}_{job_id}.json."""
    out_dir = RESULTS_DIR / job_type
    out_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = out_dir / f"{date_str}_{job_id}.json"
    payload = {"job_id": job_id, "saved_at": datetime.now().isoformat(), **data}
    path.write_text(json.dumps(payload, indent=2))
    return path


def list_results(job_type: str, limit: int = 10) -> list:
    """Return summary metadata for last N results, newest first."""
    out_dir = RESULTS_DIR / job_type
    if not out_dir.exists():
        return []
    files = sorted(out_dir.glob("*.json"), reverse=True)[:limit]
    results = []
    for f in files:
        try:
            data = json.loads(f.read_text())
            results.append(_summarise(job_type, data))
        except Exception:
            pass
    return results


def load_result(job_type: str, job_id: str) -> dict | None:
    """Load full result for a job_id."""
    out_dir = RESULTS_DIR / job_type
    if not out_dir.exists():
        return None
    matches = list(out_dir.glob(f"*_{job_id}.json"))
    if not matches:
        return None
    return json.loads(matches[0].read_text())


def to_csv(job_type: str, data: dict) -> str:
    """Flatten a result dict to CSV string."""
    output = io.StringIO()
    if job_type == "image":
        fieldnames = [
            "job_id", "saved_at", "prompt", "full_prompt", "modifier",
            "steps", "size", "model", "load_s", "gen_s", "total_s",
            "w_base", "w_task", "delta_w", "delta_e_wh",
            "poll_count", "confidence", "cpu_base", "cpu_end",
        ]
        rows = _image_rows(data)
    elif job_type == "video":
        fieldnames = [
            "job_id", "saved_at", "mode", "preset", "duration_s",
            "w_base", "w_task", "delta_w", "delta_e_wh", "poll_count", "confidence",
            "cpu_base", "cpu_peak", "gpu_base", "gpu_peak", "gpu_ppt_mean_w",
        ]
        rows = _video_rows(data)
    else:
        fieldnames = [
            "job_id", "saved_at", "model", "task", "duration_s",
            "output_tokens", "tokens_per_sec",
            "w_base", "w_task", "delta_w", "delta_e_wh", "mwh_per_token",
            "poll_count", "confidence", "cpu_base", "gpu_base",
        ]
        rows = _llm_rows(data)
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


# --- Internal helpers ---

def _summarise(job_type: str, data: dict) -> dict:
    summary = {"job_id": data.get("job_id"), "saved_at": data.get("saved_at")}
    if job_type == "image":
        e = data.get("energy", {})
        gen = data.get("generation", {})
        summary["delta_e_wh"] = e.get("delta_e_wh")
        summary["delta_t_s"] = gen.get("total_s")
        summary["confidence"] = e.get("confidence", {})
        summary["full_prompt"] = data.get("full_prompt", data.get("prompt", ""))
        summary["b64_png"] = gen.get("b64_png", "")
        return summary
    elif job_type == "video":
        mode = data.get("mode", "?")
        summary["mode"] = mode
        if mode == "both":
            cpu_e = data.get("cpu", {}).get("energy", {})
            gpu_e = data.get("gpu", {}).get("energy", {})
            summary["cpu_delta_e_wh"] = cpu_e.get("delta_e_wh")
            summary["gpu_delta_e_wh"] = gpu_e.get("delta_e_wh")
            summary["cpu_duration_s"] = cpu_e.get("delta_t_s")
            summary["gpu_duration_s"] = gpu_e.get("delta_t_s")
            summary["cpu_confidence"] = cpu_e.get("confidence", {}).get("flag")
            summary["gpu_confidence"] = gpu_e.get("confidence", {}).get("flag")
        else:
            result = data.get("result", {})
            e = result.get("energy", {})
            summary["preset"] = result.get("preset_label")
            summary["delta_e_wh"] = e.get("delta_e_wh")
            summary["duration_s"] = e.get("delta_t_s")
            summary["confidence"] = e.get("confidence", {}).get("flag")
    else:
        e = data.get("energy", {})
        i = data.get("inference", {})
        summary["model"] = data.get("model_label")
        summary["task"] = data.get("task_label")
        summary["mwh_per_token"] = e.get("mwh_per_token")
        summary["tokens_per_sec"] = i.get("tokens_per_sec")
        summary["confidence"] = e.get("confidence", {}).get("flag")
    return summary


def _image_rows(data: dict) -> list:
    e = data.get("energy", {})
    gen = data.get("generation", {})
    t = data.get("thermals", {})
    return [{
        "job_id": data.get("job_id"),
        "saved_at": data.get("saved_at"),
        "prompt": data.get("prompt"),
        "full_prompt": data.get("full_prompt"),
        "modifier": data.get("modifier"),
        "steps": gen.get("steps"),
        "size": gen.get("size"),
        "model": gen.get("model"),
        "load_s": gen.get("load_s"),
        "gen_s": gen.get("gen_s"),
        "total_s": gen.get("total_s"),
        "w_base": e.get("w_base"), "w_task": e.get("w_task"),
        "delta_w": e.get("delta_w"), "delta_e_wh": e.get("delta_e_wh"),
        "poll_count": e.get("poll_count"),
        "confidence": e.get("confidence", {}).get("label"),
        "cpu_base": t.get("cpu_base"), "cpu_end": t.get("cpu_end"),
    }]


def _video_rows(data: dict) -> list:
    common = {
        "job_id": data.get("job_id"),
        "saved_at": data.get("saved_at"),
        "mode": data.get("mode"),
    }
    mode = data.get("mode")
    if mode == "both":
        return [_video_result_row(common, data["cpu"]),
                _video_result_row(common, data["gpu"])]
    else:
        return [_video_result_row(common, data.get("result", {}))]


def _video_result_row(common: dict, r: dict) -> dict:
    e = r.get("energy", {})
    t = r.get("thermals", {})
    return {
        **common,
        "preset": r.get("preset_label"),
        "duration_s": e.get("delta_t_s"),
        "w_base": e.get("w_base"), "w_task": e.get("w_task"),
        "delta_w": e.get("delta_w"), "delta_e_wh": e.get("delta_e_wh"),
        "poll_count": e.get("poll_count"),
        "confidence": e.get("confidence", {}).get("label"),
        "cpu_base": t.get("cpu_base"), "cpu_peak": t.get("cpu_peak"),
        "gpu_base": t.get("gpu_base"), "gpu_peak": t.get("gpu_peak"),
        "gpu_ppt_mean_w": t.get("gpu_ppt_mean_w"),
    }


def _llm_rows(data: dict) -> list:
    e = data.get("energy", {})
    i = data.get("inference", {})
    t = data.get("thermals", {})
    return [{
        "job_id": data.get("job_id"),
        "saved_at": data.get("saved_at"),
        "model": data.get("model_label"),
        "task": data.get("task_label"),
        "duration_s": i.get("duration_s"),
        "output_tokens": i.get("output_tokens"),
        "tokens_per_sec": i.get("tokens_per_sec"),
        "w_base": e.get("w_base"), "w_task": e.get("w_task"),
        "delta_w": e.get("delta_w"), "delta_e_wh": e.get("delta_e_wh"),
        "mwh_per_token": e.get("mwh_per_token"),
        "poll_count": e.get("poll_count"),
        "confidence": e.get("confidence", {}).get("label"),
        "cpu_base": t.get("cpu_base"),
        "gpu_base": t.get("gpu_base"),
    }]
