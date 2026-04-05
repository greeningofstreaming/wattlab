import json
from pathlib import Path

SETTINGS_FILE = Path("/home/gos/wattlab/settings.json")

DEFAULTS = {
    "baseline_polls": 10,
    "video_cooldown_s": 60,
    "llm_rest_s": 10,
    "llm_unload_settle_s": 3,
    "conf_green_delta_w": 5.0,
    "conf_green_polls": 10,
    "conf_yellow_delta_w": 2.0,
    "conf_yellow_polls": 5,
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
