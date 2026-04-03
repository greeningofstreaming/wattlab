from pathlib import Path
import subprocess
import json

# Pre-loaded test content registry
PRELOADED = {
    "meridian_4k": {
        "label": "Meridian 4K",
        "description": "Netflix Open Content · 3840×2160 · 59.94fps · H.264 · 12min · CC BY 4.0",
        "path": Path("/home/gos/wattlab/test_content/meridian_4k.mp4"),
        "credit": "Netflix Open Content (opencontent.netflix.com)",
    }
}

def get_source_info(key: str) -> dict:
    """Return metadata for a pre-loaded source file."""
    source = PRELOADED.get(key)
    if not source or not source["path"].exists():
        return None

    try:
        result = subprocess.run([
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams", "-show_format",
            str(source["path"])
        ], capture_output=True, text=True)
        data = json.loads(result.stdout)

        video = next((s for s in data["streams"] if s["codec_type"] == "video"), {})
        fmt = data.get("format", {})

        return {
            "key": key,
            "label": source["label"],
            "description": source["description"],
            "credit": source["credit"],
            "path": str(source["path"]),
            "size_mb": round(source["path"].stat().st_size / 1024 / 1024, 1),
            "duration_s": round(float(fmt.get("duration", 0)), 1),
            "resolution": f"{video.get('width', '?')}x{video.get('height', '?')}",
            "codec": video.get("codec_name", "?"),
            "fps": video.get("r_frame_rate", "?"),
        }
    except Exception as e:
        return {"key": key, "label": source["label"], "error": str(e)}

def get_all_sources() -> list:
    return [get_source_info(k) for k in PRELOADED]
