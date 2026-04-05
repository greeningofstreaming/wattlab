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
    files = list(out_dir.glob("*.json"))
    results = []
    for f in files:
        try:
            data = json.loads(f.read_text())
            results.append(_summarise(job_type, data))
        except Exception:
            pass
    results.sort(key=lambda r: r.get("saved_at") or "", reverse=True)
    return results[:limit]


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
        mode = data.get("mode", "cpu")
        summary["mode"] = mode
        summary["full_prompt"] = data.get("full_prompt", data.get("prompt", ""))
        if mode == "both":
            for side in ("cpu", "gpu"):
                s = data.get(side, {})
                e = s.get("energy", {})
                gen = s.get("generation", {})
                summary[side] = {
                    "delta_e_wh": e.get("delta_e_wh"),
                    "delta_t_s": gen.get("total_s"),
                    "confidence": e.get("confidence", {}),
                    "b64_png": gen.get("b64_png", ""),
                }
        else:
            e = data.get("energy", {})
            gen = data.get("generation", {})
            summary["delta_e_wh"] = e.get("delta_e_wh")
            summary["delta_t_s"] = gen.get("total_s")
            summary["confidence"] = e.get("confidence", {})
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
    else:  # llm (including rag)
        mode = data.get("mode", "single")
        summary["model"] = data.get("model_label")
        if mode == "rag":
            summary["task"] = f"RAG/{data.get('rag_mode', 'baseline')}"
            e = data.get("energy", {})
            i = data.get("inference", {})
            summary["mwh_per_token"] = e.get("mwh_per_token")
            summary["tokens_per_sec"] = i.get("tokens_per_sec")
            summary["confidence"] = e.get("confidence", {}).get("flag")
            return summary
        elif mode == "rag_compare":
            summary["task"] = "RAG compare (3 modes)"
            rl = data.get("results", {}).get("rag_large", {})
            e = rl.get("energy", {})
            i = rl.get("inference", {})
            summary["mwh_per_token"] = e.get("mwh_per_token")
            summary["tokens_per_sec"] = i.get("tokens_per_sec")
            summary["confidence"] = e.get("confidence", {}).get("flag")
            return summary
        elif mode == "all":
            summary["task"] = "T1+T2+T3"
            t3 = data.get("tasks", {}).get("T3", {})
            e = t3.get("energy", {})
            i = t3.get("inference", {})
        elif mode == "all_both":
            summary["task"] = "T1+T2+T3 · CPU vs GPU"
            t3 = data.get("gpu", {}).get("T3", {})
            e = t3.get("energy", {})
            i = t3.get("inference", {})
        elif mode == "both":
            summary["task"] = data.get("task_label")
            gpu = data.get("gpu", {})
            e = gpu.get("energy", {})
            i = gpu.get("inference", {})
        elif mode == "batch":
            summary["task"] = data.get("task_label")
            agg = data.get("aggregate", {})
            runs = data.get("runs", [])
            summary["mwh_per_token"] = agg.get("mwh_per_token_mean")
            summary["tokens_per_sec"] = agg.get("tokens_per_sec_mean")
            try:
                summary["confidence"] = runs[-1]["energy"]["confidence"]["flag"]
            except (IndexError, KeyError):
                summary["confidence"] = None
            return summary
        else:  # single
            summary["task"] = data.get("task_label")
            e = data.get("energy", {})
            i = data.get("inference", {})
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
    mode = data.get("mode", "single")
    common = {"job_id": data.get("job_id"), "saved_at": data.get("saved_at"),
              "model": data.get("model_label")}

    def _row(task_label, i, e, t):
        return {**common,
            "task": task_label,
            "duration_s": i.get("duration_s"),
            "output_tokens": i.get("output_tokens"),
            "tokens_per_sec": i.get("tokens_per_sec"),
            "w_base": e.get("w_base"), "w_task": e.get("w_task"),
            "delta_w": e.get("delta_w"), "delta_e_wh": e.get("delta_e_wh"),
            "mwh_per_token": e.get("mwh_per_token"),
            "poll_count": e.get("poll_count"),
            "confidence": e.get("confidence", {}).get("label"),
            "cpu_base": t.get("cpu_base"), "gpu_base": t.get("gpu_base"),
        }

    if mode == "all":
        rows = []
        for tk, tr in data.get("tasks", {}).items():
            rows.append(_row(f"{tk} {tr.get('task_label','')}",
                             tr.get("inference", {}), tr.get("energy", {}),
                             tr.get("thermals", {})))
        return rows
    elif mode == "all_both":
        rows = []
        for dev in ("cpu", "gpu"):
            for tk, tr in data.get(dev, {}).items():
                rows.append(_row(f"{tk} {tr.get('task_label','')} ({dev})",
                                 tr.get("inference", {}), tr.get("energy", {}),
                                 tr.get("thermals", {})))
        return rows
    elif mode == "both":
        rows = []
        for dev in ("cpu", "gpu"):
            tr = data.get(dev, {})
            rows.append(_row(f"{data.get('task_label','')} ({dev})",
                             tr.get("inference", {}), tr.get("energy", {}),
                             tr.get("thermals", {})))
        return rows
    elif mode == "rag":
        e = data.get("energy", {})
        i = data.get("inference", {})
        t = data.get("thermals", {})
        label = f"RAG/{data.get('rag_mode','')} — {data.get('question','')[:50]}"
        return [_row(label, i, e, t)]
    elif mode == "rag_compare":
        rows = []
        for m, res in data.get("results", {}).items():
            e = res.get("energy", {})
            i = res.get("inference", {})
            t = res.get("thermals", {})
            rows.append(_row(f"RAG/{m} — {data.get('question','')[:40]}", i, e, t))
        return rows
    elif mode == "batch":
        rows = []
        t = data.get("thermals", {})
        for run in data.get("runs", []):
            rows.append(_row(f"{data.get('task_label','')} run {run['run']}",
                             run.get("inference", {}), run.get("energy", {}), t))
        return rows
    else:  # single
        e = data.get("energy", {})
        i = data.get("inference", {})
        t = data.get("thermals", {})
        return [_row(data.get("task_label", ""), i, e, t)]
