import asyncio
import io
import ipaddress
import json
import uuid
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from dotenv import dotenv_values
from tapo import ApiClient
from video import run_video_measurement, run_both_measurement, run_video_measurement_path, run_both_measurement_path, UPLOAD_DIR, LOCK_FILE
from sources import get_all_sources, PRELOADED
from llm import run_llm_measurement, run_llm_batch_measurement, run_llm_both_measurement, MODELS, TASKS
from persist import save_result, list_results, load_result, to_csv
from image_gen import run_image_measurement, run_image_both_measurement, IMAGE_STEPS_CPU, IMAGE_STEPS_GPU, GPU_BATCH_SIZE
import rag as rag_module
import settings as cfg

config = dotenv_values("/home/gos/wattlab/.env")
app = FastAPI()

GATE_PASSWORD = config.get("WATTLAB_GATE_PASSWORD", "")

@app.middleware("http")
async def gate_middleware(request: Request, call_next):
    if not GATE_PASSWORD or request.url.path.startswith("/gate"):
        return await call_next(request)
    if request.cookies.get("wl_auth") == GATE_PASSWORD:
        return await call_next(request)
    next_url = request.url.path
    return RedirectResponse(url=f"/gate?next={next_url}", status_code=302)

@app.get("/gate", response_class=HTMLResponse)
async def gate_page(next: str = "/", error: bool = False):
    err_html = ('<p style="color:#ff4400;font-family:monospace;font-size:0.85rem;'
                'margin-bottom:1rem">Incorrect password.</p>') if error else ''
    return f"""<!DOCTYPE html>
<html>
<head>
  <title>WattLab</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:monospace;background:#0a0a0a;color:#e0e0e0;
         display:flex;flex-direction:column;align-items:center;
         justify-content:center;height:100vh;gap:0}}
    h1{{color:#00ff99;font-size:1.4rem;margin-bottom:0.25rem}}
    p.sub{{color:#444;font-size:0.8rem;margin-bottom:2rem}}
    input{{background:#111;border:1px solid #333;color:#e0e0e0;
           font-family:monospace;font-size:1rem;padding:0.6rem 1rem;
           width:200px;text-align:center;letter-spacing:0.1em}}
    input:focus{{border-color:#00ff99;outline:none}}
    button{{background:#00ff99;color:#000;border:none;
            font-family:monospace;font-size:1rem;padding:0.6rem 2rem;
            cursor:pointer;margin-top:0.75rem}}
    button:hover{{background:#00dd88}}
    form{{display:flex;flex-direction:column;align-items:center;gap:0}}
  </style>
</head>
<body>
  <h1>WattLab</h1>
  <p class="sub">Greening of Streaming · Private preview</p>
  {err_html}
  <form method="post" action="/gate">
    <input type="hidden" name="next" value="{next}">
    <input type="password" name="password" placeholder="password" autofocus>
    <button type="submit">Enter</button>
  </form>
</body>
</html>"""

@app.post("/gate")
async def gate_submit(request: Request, password: str = Form(...), next: str = Form("/")):
    if password == GATE_PASSWORD:
        response = RedirectResponse(url=next, status_code=302)
        response.set_cookie("wl_auth", GATE_PASSWORD, max_age=30*24*3600, httponly=True)
        return response
    return RedirectResponse(url=f"/gate?next={next}&error=1", status_code=302)
jobs = {}

# --- Queue ---
pending_queue = []          # list of {"job_id", "type", "label", "coro_fn"}
queue_event = asyncio.Event()
current_job_id = None       # job currently executing
MAX_QUEUE_DEPTH = 8         # total queued + running; 429 beyond this


def enqueue(job_id: str, job_type: str, label: str, coro_fn):
    """Add a job to the FIFO queue. Returns 1-based position, or None if queue is full."""
    total = len(pending_queue) + (1 if current_job_id else 0)
    if total >= MAX_QUEUE_DEPTH:
        return None
    position = len(pending_queue) + 1
    jobs[job_id] = {"stage": "queued", "queue_position": position, "type": job_type, "label": label, "result": None, "error": None}
    pending_queue.append({"job_id": job_id, "type": job_type, "label": label, "coro_fn": coro_fn})
    queue_event.set()
    return position


@app.on_event("startup")
async def startup():
    asyncio.create_task(queue_worker())
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, rag_module.check_index)


async def queue_worker():
    global current_job_id
    while True:
        await queue_event.wait()
        queue_event.clear()
        while pending_queue:
            entry = pending_queue.pop(0)
            job_id = entry["job_id"]
            current_job_id = job_id
            # Update queue positions for remaining jobs
            for i, e in enumerate(pending_queue):
                if e["job_id"] in jobs:
                    jobs[e["job_id"]]["queue_position"] = i + 1
            try:
                await entry["coro_fn"]()
            except Exception as e:
                jobs[job_id] = {**jobs.get(job_id, {}),
                                "stage": "error", "error": str(e)}
                LOCK_FILE.unlink(missing_ok=True)
            finally:
                current_job_id = None

GOS_LOGO_URL = "https://static.wixstatic.com/media/b1006e_f5e9aff607cf4133abf7089207dc3cab~mv2.png"
_LOGO = (
    f'<a href="https://greeningofstreaming.org" target="_blank"'
    f' style="display:inline-flex;align-items:center;gap:0.6rem;'
    f'text-decoration:none;margin-bottom:1.5rem;opacity:0.75;'
    f'transition:opacity 0.2s" onmouseover="this.style.opacity=1"'
    f' onmouseout="this.style.opacity=0.75">'
    f'<img src="{GOS_LOGO_URL}" alt="Greening of Streaming"'
    f' height="32" style="display:block">'
    f'<span style="color:#444;font-size:0.72rem;font-family:monospace">'
    f'greeningofstreaming.org</span></a>'
)
_BACK = '<a href="/" style="color:#555;text-decoration:none;font-size:0.82rem;display:block;margin-bottom:1.5rem">← Home</a>'
_FOOTER = f'<footer style="margin-top:3rem;padding-top:1rem;border-top:1px solid #111">{_LOGO}</footer>'

# Confidence flag popover — inject into any page that shows .conf-badge elements.
# Plain string (not f-string) so JS curly braces need no escaping.
_CONF_HELP_WIDGET = (
    '<div id="conf-pop" style="display:none;position:fixed;z-index:9999;background:#111;'
    'border:1px solid #222;padding:1rem 1.25rem;max-width:300px;font-size:0.8rem;'
    'line-height:1.7;box-shadow:0 4px 24px #000a">'
    '<div style="font-family:monospace;color:#333;font-size:0.65rem;text-transform:uppercase;'
    'letter-spacing:0.06em;margin-bottom:0.75rem">Confidence flag</div>'
    '<div style="margin-bottom:0.5rem">'
    '<span style="font-family:monospace">🟢 Repeatable</span>'
    '<span style="color:#555;display:block;font-size:0.75rem;padding-left:1.4rem">'
    'ΔW &gt; 5W and ≥ 10 polls. Reliable enough to cite.</span></div>'
    '<div style="margin-bottom:0.5rem">'
    '<span style="font-family:monospace">🟡 Early insight</span>'
    '<span style="color:#555;display:block;font-size:0.75rem;padding-left:1.4rem">'
    'ΔW ≥ 2W or ≥ 5 polls. Directional, needs more runs.</span></div>'
    '<div>'
    '<span style="font-family:monospace">🔴 Need more data</span>'
    '<span style="color:#555;display:block;font-size:0.75rem;padding-left:1.4rem">'
    'ΔW &lt; 2W. Near P110 noise floor. Don\'t cite yet.</span></div>'
    '<div style="color:#2a2a2a;font-size:0.7rem;margin-top:0.75rem;font-family:monospace">'
    'ΔW = mean task power \u2212 idle baseline \u00b7 1s P110 polls</div>'
    '</div>'
    '<script>(function(){'
    'var s=document.createElement("style");'
    's.textContent=".conf-badge{cursor:pointer}";'
    'document.head.appendChild(s);'
    'var pop=document.getElementById("conf-pop");'
    'document.addEventListener("click",function(e){'
    'var b=e.target.closest(".conf-badge");'
    'if(b){e.stopPropagation();'
    'var r=b.getBoundingClientRect();'
    'pop.style.left=Math.min(r.left,window.innerWidth-320)+"px";'
    'pop.style.top=(r.bottom+6+window.scrollY)+"px";'
    'pop.style.display=pop.style.display==="none"?"block":"none";'
    '}else if(!pop.contains(e.target)){pop.style.display="none";}'
    '});'
    '})();</script>'
)

# Shared progress utilities — injected into every test page.
# Plain string (not f-string): JS braces are single, no escaping needed.
_PROGRESS_JS = """<script>
function wlFmt(v, dec) { if (v === null || v === undefined) return '\u2014'; return Number(v).toFixed(dec ?? 2); }
function wlFormatElapsed(ms) {
    const s = Math.floor(ms / 1000);
    if (s < 60) return s + 's';
    return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
}
function wlStageList(stages, cur) {
    return stages.map(function(lbl, i) {
        var s = i < cur ? 'done' : i === cur ? 'active' : 'pending';
        var ic = s === 'done' ? '✓' : s === 'active' ? '▶' : '·';
        var col = s === 'done' ? '#00ff99' : s === 'active' ? '#ffaa00' : '#333';
        return '<div style="display:flex;align-items:center;gap:0.6rem;font-size:0.82rem;margin-bottom:0.3rem">'
             + '<span style="color:' + col + ';width:1rem">' + ic + '</span>'
             + '<span style="color:' + col + '">' + lbl + '</span></div>';
    }).join('');
}
function wlRenderProgress(opts) {
    var w = opts.watts;
    var wHtml = w != null
        ? '<div style="font-size:2.5rem;color:#00ff99;font-family:monospace;font-weight:bold;margin:0.75rem 0 0">'
          + w.toFixed(1) + ' W</div>'
          + '<div style="color:#555;font-size:0.72rem;letter-spacing:0.04em;margin-bottom:0.5rem">live wall power \xb7 Tapo P110</div>'
        : '';
    var elHtml = opts.elapsed != null
        ? '<div style="color:#444;font-size:0.78rem;margin-top:0.4rem">Elapsed: ' + wlFormatElapsed(opts.elapsed) + '</div>'
        : '';
    document.getElementById('status').innerHTML =
        '<div style="border:1px solid #222;padding:1.5rem">'
        + '<div style="color:#ffaa00;font-size:0.9rem;margin-bottom:0.75rem">'
        + (opts.header || 'Measuring \u2014 do not close this tab') + '</div>'
        + (opts.stagesHtml || '')
        + wHtml + elHtml
        + (opts.extraHtml || '')
        + '</div>';
}
function wlRenderQueued(pos) {
    document.getElementById('status').innerHTML =
        '<div style="border:1px solid #333;padding:1.5rem">'
        + '<div style="color:#ffaa00;font-size:0.9rem;margin-bottom:0.75rem">\u23f1 Queued \u2014 position ' + pos + '</div>'
        + '<div style="color:#555;font-size:0.82rem">Another measurement is running. Your job will start automatically.</div>'
        + '</div>';
}
</script>"""

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

# --- Home ---

@app.get("/", response_class=HTMLResponse)
async def index():
    watts = await get_power_watts()
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>WattLab — GoS</title>
    <meta http-equiv="refresh" content="10">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: monospace; background: #0a0a0a; color: #e0e0e0;
               display: flex; flex-direction: column; align-items: center;
               justify-content: center; height: 100vh; }}
        .watts {{ font-size: 6rem; color: #00ff99; font-weight: bold; }}
        .label {{ font-size: 1.2rem; color: #888; margin-top: 1rem; }}
        .scope {{ font-size: 0.8rem; color: #444; margin-top: 0.5rem; }}
        .nav {{ margin-top: 3rem; display: flex; flex-direction: column; align-items: center; gap: 1rem; width: 100%; max-width: 600px; }}
        .nav-tour a {{ color: #0a0a0a; background: #00ff99; text-decoration: none;
                       padding: 0.6rem 2.5rem; font-size: 1rem; font-weight: bold;
                       display: inline-block; }}
        .nav-tour a:hover {{ background: #00cc77; }}
        .nav-primary {{ display: flex; gap: 0.75rem; flex-wrap: wrap; justify-content: center; }}
        .nav-primary a {{ color: #00ff99; text-decoration: none;
                          border: 1px solid #00ff99; padding: 0.5rem 1.25rem;
                          font-size: 0.95rem; }}
        .nav-primary a:hover {{ background: #00ff9922; }}
        .nav-secondary {{ display: flex; gap: 0.6rem; flex-wrap: wrap; justify-content: center; }}
        .nav-secondary a {{ color: #666; text-decoration: none;
                            border: 1px solid #333; padding: 0.35rem 0.9rem;
                            font-size: 0.8rem; }}
        .nav-secondary a:hover {{ color: #aaa; border-color: #555; }}
    </style>
</head>
<body>
    <div class="watts">{watts:.1f} W</div>
    <div class="label">GoS1 current power draw</div>
    <div class="scope">Device layer only · Tapo P110 · refreshes every 10s</div>
    <div class="nav">
        <div class="nav-tour"><a href="/demo">◆ Guided Tour</a></div>
        <div class="nav-primary">
            <a href="/video">▶ Video transcode</a>
            <a href="/image">▶ Image generation</a>
            <a href="/llm">▶ LLM inference</a>
        </div>
        <div class="nav-secondary">
            <a href="/rag">RAG energy test</a>
            <a href="/queue-status">⏱ Queue</a>
            <a href="/settings">⚙ Settings</a>
        </div>
    </div>
    {_FOOTER}
</body>
</html>"""

@app.get("/power")
async def power_json():
    watts = await get_power_watts()
    return {"watts": watts, "scope": "device_only", "source": "tapo_p110"}

# --- Video page ---

@app.get("/video", response_class=HTMLResponse)
async def video_page():
    queue_depth = len(pending_queue) + (1 if current_job_id else 0)
    busy_banner = (f'<div style="background:#333;color:#ffaa00;padding:0.75rem 1rem;'
                   f'margin-bottom:1rem;font-size:0.85rem">'
                   f'⏱ {queue_depth} job{"s" if queue_depth != 1 else ""} in queue — '
                   f'yours will be added and run automatically.</div>') \
        if queue_depth > 0 else ""

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>WattLab — Video Test</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: monospace; background: #0a0a0a; color: #e0e0e0;
               max-width: 780px; margin: 0 auto; padding: 2rem; }}
        h1 {{ color: #00ff99; margin-bottom: 0.25rem; font-size: 1.6rem; }}
        .subtitle {{ color: #555; font-size: 0.8rem; margin-bottom: 1.5rem; }}
        .info {{ color: #777; font-size: 0.82rem; margin-bottom: 1.5rem;
                 border-left: 2px solid #222; padding-left: 1rem; line-height: 1.6; }}
        .presets {{ display: flex; gap: 0.75rem; margin-bottom: 1.5rem; }}
        .preset {{ border: 1px solid #333; padding: 1rem; cursor: pointer;
                   flex: 1; transition: border-color 0.15s; }}
        .preset:hover {{ border-color: #00ff9966; }}
        .preset.selected {{ border-color: #00ff99; background: #00ff9911; }}
        .preset h3 {{ color: #00ff99; font-size: 0.9rem; margin-bottom: 0.4rem; }}
        .preset p {{ color: #666; font-size: 0.78rem; line-height: 1.5; }}
        .preset .badge {{ display: inline-block; background: #00ff9922;
                          color: #00ff99; font-size: 0.7rem;
                          padding: 0.1rem 0.4rem; margin-bottom: 0.4rem; }}
        input[type=file] {{ color: #aaa; margin-bottom: 1rem; width: 100%; }}
        button {{ background: #00ff99; color: #000; border: none;
                  padding: 0.75rem 2rem; cursor: pointer;
                  font-family: monospace; font-size: 1rem; }}
        button:disabled {{ background: #222; color: #555; cursor: not-allowed; }}
        button:hover:not(:disabled) {{ background: #00dd88; }}
        #status {{ margin-top: 1.5rem; }}

        /* Progress styles */
        .progress-box {{ border: 1px solid #222; padding: 1.5rem; }}
        .progress-header {{ color: #ffaa00; font-size: 0.9rem; margin-bottom: 1.25rem; }}
        .stages {{ display: flex; flex-direction: column; gap: 0.5rem; margin-bottom: 1.25rem; }}
        .stage {{ display: flex; align-items: center; gap: 0.75rem; font-size: 0.82rem; }}
        .stage-icon {{ width: 1.2rem; text-align: center; flex-shrink: 0; }}
        .stage-label {{ color: #666; }}
        .stage.done .stage-label {{ color: #00ff99; }}
        .stage.active .stage-label {{ color: #ffaa00; }}
        .stage.active .stage-icon {{ animation: pulse 1s infinite; }}
        @keyframes pulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:0.3; }} }}
        .progress-footer {{ display: flex; justify-content: space-between;
                            color: #444; font-size: 0.78rem; border-top: 1px solid #111;
                            padding-top: 0.75rem; }}
        .elapsed {{ color: #555; }}

        /* Report styles */
        .report h2 {{ color: #00ff99; font-size: 1.1rem; margin-bottom: 1rem;
                      padding-bottom: 0.5rem; border-bottom: 1px solid #222; }}
        .cols {{ display: flex; gap: 1rem; margin-bottom: 1rem; }}
        .col {{ flex: 1; border: 1px solid #222; padding: 1rem; }}
        .col h3 {{ color: #00ff99; font-size: 0.85rem; margin-bottom: 0.4rem; }}
        .col .sub {{ color: #555; font-size: 0.75rem; margin-bottom: 0.75rem; }}
        .metric {{ display: flex; justify-content: space-between;
                   padding: 0.3rem 0; border-bottom: 1px solid #111; font-size: 0.82rem; }}
        .metric:last-child {{ border-bottom: none; }}
        .val {{ color: #00ff99; }}
        .section-title {{ color: #444; font-size: 0.72rem; text-transform: uppercase;
                          letter-spacing: 0.05em; margin: 0.75rem 0 0.4rem; }}
        .analysis-box {{ border: 1px solid #00ff9944; padding: 1rem;
                         margin-bottom: 1rem; background: #00ff9908; }}
        .analysis-box h3 {{ color: #00ff99; font-size: 0.85rem; margin-bottom: 0.5rem; }}
        .finding {{ color: #ccc; font-size: 0.85rem; line-height: 1.7; }}
        .conf-note {{ color: #666; font-size: 0.78rem; margin-top: 0.5rem; }}
        .scope-note {{ color: #333; font-size: 0.72rem; margin-top: 1rem; }}
        .single-report {{ border: 1px solid #222; padding: 1.5rem; }}
        a.back {{ color: #555; text-decoration: none; font-size: 0.82rem;
                  display: inline-block; margin-top: 1.5rem; }}
        a.back:hover {{ color: #00ff99; }}
    </style>
</head>
<body>
    {_BACK}
    {busy_banner}
    <h1>Video Transcode Energy Test</h1>
    <div class="subtitle">Greening of Streaming · WattLab · GoS1</div>

    <div class="info">
        Accepted: MP4, MOV, MKV, AVI, WebM, TS · Max 1GB<br>
        Baseline measured 10s before each run · P110 + thermals at 1s intervals<br>
        Scope: device layer only — network, CDN, CPE excluded
    </div>

    <div style="color:#555;font-size:0.75rem;text-transform:uppercase;
                letter-spacing:0.05em;margin-bottom:0.5rem">H.264</div>
    <div class="presets" style="margin-bottom:0.75rem">
        <div class="preset" id="preset-cpu" onclick="selectPreset('cpu')">
            <h3>H.264 CPU</h3>
            <p style="color:#555;font-size:0.75rem;margin-bottom:0.4rem">libx264 · CRF 23 · 1080p</p>
            <p>Software encode across all 24 cores.</p>
        </div>
        <div class="preset" id="preset-gpu" onclick="selectPreset('gpu')">
            <h3>H.264 GPU</h3>
            <p style="color:#555;font-size:0.75rem;margin-bottom:0.4rem">h264_vaapi · QP 23 · 1080p</p>
            <p>AMD RX 7800 XT hardware acceleration.</p>
        </div>
        <div class="preset selected" id="preset-both" onclick="selectPreset('both')">
            <div class="badge">DEFAULT</div>
            <h3>H.264 Both</h3>
            <p style="color:#555;font-size:0.75rem;margin-bottom:0.4rem">CPU then GPU · same file</p>
            <p>Side-by-side energy + thermal report with analysis.</p>
        </div>
    </div>
    <div style="color:#555;font-size:0.75rem;text-transform:uppercase;
                letter-spacing:0.05em;margin-bottom:0.5rem">H.265 / AV1</div>
    <div class="presets" style="margin-bottom:1.5rem">
        <div class="preset" id="preset-h265_cpu" onclick="selectPreset('h265_cpu')">
            <h3>H.265 CPU</h3>
            <p style="color:#555;font-size:0.75rem;margin-bottom:0.4rem">libx265 · CRF 28 · 1080p</p>
            <p>Software HEVC encode.</p>
        </div>
        <div class="preset" id="preset-h265_gpu" onclick="selectPreset('h265_gpu')">
            <h3>H.265 GPU</h3>
            <p style="color:#555;font-size:0.75rem;margin-bottom:0.4rem">hevc_vaapi · QP 28 · 1080p</p>
            <p>AMD hardware HEVC.</p>
        </div>
        <div class="preset" id="preset-av1_cpu" onclick="selectPreset('av1_cpu')">
            <h3>AV1 CPU</h3>
            <p style="color:#555;font-size:0.75rem;margin-bottom:0.4rem">libsvtav1 · CRF 30 · 1080p</p>
            <p>SVT-AV1 software encode.</p>
        </div>
    </div>

    <div style="margin-bottom:1.5rem">
        <div style="color:#555;font-size:0.75rem;text-transform:uppercase;
                    letter-spacing:0.05em;margin-bottom:0.75rem">Source</div>
        <div style="display:flex;flex-direction:column;gap:0.5rem">
            <label style="display:flex;align-items:flex-start;gap:0.75rem;
                          border:1px solid #333;padding:0.75rem;cursor:pointer"
                   id="src-upload-label">
                <input type="radio" name="source" value="upload" checked
                       onchange="selectSource('upload')"
                       style="margin-top:0.2rem;accent-color:#00ff99">
                <div>
                    <div style="color:#e0e0e0;font-size:0.85rem">Upload a file</div>
                    <div style="color:#555;font-size:0.75rem">
                        MP4, MOV, MKV, AVI, WebM, TS · Max 1GB
                    </div>
                </div>
            </label>
            <label style="display:flex;align-items:flex-start;gap:0.75rem;
                          border:1px solid #333;padding:0.75rem;cursor:pointer"
                   id="src-meridian-label">
                <input type="radio" name="source" value="meridian_4k"
                       onchange="selectSource('meridian_4k')"
                       style="margin-top:0.2rem;accent-color:#00ff99">
                <div>
                    <div style="color:#e0e0e0;font-size:0.85rem">
                        Meridian 4K · Netflix Open Content
                    </div>
                    <div style="color:#555;font-size:0.75rem">
                        3840×2160 · 59.94fps · H.264 · 12min · 812MB · CC BY 4.0<br>
                        ⚠ Both mode ~6-8 min total
                    </div>
                </div>
            </label>
        </div>
    </div>

    <div id="upload-area">
        <input type="file" id="fileInput" accept=".mp4,.mov,.mkv,.avi,.webm,.ts">
    </div>
    <button id="runBtn" onclick="uploadAndRun()">Upload & Measure</button>

    <div id="status"></div>
    <div id="prev-runs" style="margin-top:2rem;border-top:1px solid #111;padding-top:1.5rem"></div>

    <script>
    let selectedPreset = 'both';
    let selectedSource = 'upload';

    function selectSource(src) {{
        selectedSource = src;
        document.getElementById('upload-area').style.display =
            src === 'upload' ? 'block' : 'none';
        document.getElementById('runBtn').textContent =
            src === 'upload' ? 'Upload & Measure' : 'Run Measurement';
    }}
    let progressTimer = null;
    let elapsedTimer = null;
    let startTime = null;

    const _SINGLE = ['Baseline', 'Encode', 'Done'];
    const _SINGLE_MAP = {{'starting':0, 'baseline':0, 'cpu_encode':1, 'gpu_encode':1,
                          'h265_cpu_encode':1, 'h265_gpu_encode':1, 'av1_cpu_encode':1, 'done':2}};
    const STAGES = {{
        cpu:      _SINGLE,
        gpu:      _SINGLE,
        h265_cpu: _SINGLE,
        h265_gpu: _SINGLE,
        av1_cpu:  _SINGLE,
        both: ['Baseline', 'CPU encode', 'Rest', 'Baseline 2', 'GPU encode', 'Done'],
    }};

    const STAGE_MAP = {{
        cpu:      _SINGLE_MAP,
        gpu:      _SINGLE_MAP,
        h265_cpu: _SINGLE_MAP,
        h265_gpu: _SINGLE_MAP,
        av1_cpu:  _SINGLE_MAP,
        both: {{'starting':0, 'baseline':0, 'cpu_encode':1, 'rest':2,
                'baseline_2':3, 'gpu_encode':4, 'done':5}},
    }};

    function selectPreset(key) {{
        selectedPreset = key;
        document.querySelectorAll('.preset').forEach(el => el.classList.remove('selected'));
        const el = document.getElementById('preset-' + key);
        if (el) el.classList.add('selected');
    }}

    function renderProgress(jobId, mode, serverStage, watts) {{
        const stages = STAGES[mode];
        const stageMap = STAGE_MAP[mode];
        const currentStage = stageMap[serverStage] !== undefined ? stageMap[serverStage] : 0;
        wlRenderProgress({{
            header: 'Running measurement \u2014 do not close this tab',
            stagesHtml: wlStageList(stages, currentStage),
            watts: watts,
            elapsed: startTime ? Date.now() - startTime : null,
            extraHtml: '<div style="color:#333;font-size:0.72rem;margin-top:0.4rem">Job: ' + jobId + ' \xb7 polling every 5s</div>',
        }});
    }}

    function stopProgress() {{
        if (progressTimer) {{ clearInterval(progressTimer); progressTimer = null; }}
    }}

    async function uploadAndRun() {{
        const btn = document.getElementById('runBtn');
        btn.disabled = true;
        const status = document.getElementById('status');

        let resp;
        try {{
            if (selectedSource === 'upload') {{
                const file = document.getElementById('fileInput').files[0];
                if (!file) {{ alert('Please select a file first'); btn.disabled = false; return; }}
                if (file.size > 1024 * 1024 * 1024) {{ alert('File too large (max 1GB)'); btn.disabled = false; return; }}
                status.innerHTML = '<div style="color:#ffaa00">Uploading ' + file.name + '...</div>';
                const form = new FormData();
                form.append('file', file);
                form.append('preset', selectedPreset);
                resp = await fetch('/video/upload', {{ method: 'POST', body: form }});
            }} else {{
                status.innerHTML = '<div style="color:#ffaa00">Starting measurement on ' + selectedSource + '...</div>';
                const form = new FormData();
                form.append('source_key', selectedSource);
                form.append('preset', selectedPreset);
                resp = await fetch('/video/use-source', {{ method: 'POST', body: form }});
            }}

            const data = await resp.json();
            if (data.job_id) {{
                startTime = Date.now();
                renderProgress(data.job_id, selectedPreset, 'starting');
                pollJob(data.job_id, selectedPreset);
            }} else {{
                status.innerHTML = '<div style="color:#ff4400">Error: ' + JSON.stringify(data) + '</div>';
                btn.disabled = false;
            }}
        }} catch(e) {{
            status.innerHTML = '<div style="color:#ff4400">Failed: ' + e + '</div>';
            btn.disabled = false;
        }}
    }}

    async function pollJob(jobId, mode) {{
        try {{
            const [resp, powerR] = await Promise.all([
                fetch('/video/job/' + jobId),
                fetch('/power').catch(() => null),
            ]);
            const data = await resp.json();
            const watts = powerR ? (await powerR.json().catch(()=>({{}}))).watts ?? null : null;
            if (data.status === 'done') {{
                stopProgress();
                renderResult(data.result, jobId);
                document.getElementById('runBtn').disabled = false;
            }} else if (data.status === 'error') {{
                stopProgress();
                document.getElementById('status').innerHTML =
                    '<div style="color:#ff4400">Error: ' + data.error + '</div>';
                document.getElementById('runBtn').disabled = false;
            }} else if (data.stage === 'queued') {{
                renderQueued(data.queue_position);
                setTimeout(() => pollJob(jobId, mode), 3000);
            }} else {{
                renderProgress(jobId, mode, data.stage || "starting", watts);
                setTimeout(() => pollJob(jobId, mode), 5000);
            }}
        }} catch(e) {{
            setTimeout(() => pollJob(jobId, mode), 5000);
        }}
    }}

    function renderQueued(position) {{ wlRenderQueued(position); }}

    function metricRow(label, val, unit='') {{
        return `<div class="metric"><span>${{label}}</span>
                <span class="val">${{val}}${{unit ? ' ' + unit : ''}}</span></div>`;
    }}

    function renderSingle(r) {{
        const e = r.energy;
        const t = r.thermals;
        return `
        <div class="single-report">
            <h2>Energy Report — ${{r.preset_label}}</h2>
            <div class="section-title">Encode</div>
            ${{metricRow('Preset', r.preset_detail)}}
            ${{metricRow('Duration', e.delta_t_s, 's')}}
            ${{metricRow('Output size', r.output_size_mb, 'MB')}}
            <div class="section-title">Power (P110)</div>
            ${{metricRow('Baseline', e.w_base, 'W')}}
            ${{metricRow('Task mean', e.w_task, 'W')}}
            ${{metricRow('Delta (ΔW)', e.delta_w, 'W')}}
            ${{metricRow('Energy (ΔE)', e.delta_e_wh, 'Wh')}}
            ${{metricRow('Polls', e.poll_count)}}
            <div class="section-title">Thermals</div>
            ${{metricRow('CPU base → peak', t.cpu_base + ' → ' + t.cpu_peak, '°C')}}
            ${{metricRow('GPU base → peak', t.gpu_base + ' → ' + t.gpu_peak, '°C')}}
            ${{t.gpu_ppt_mean_w ? metricRow('GPU PPT mean / peak', t.gpu_ppt_mean_w + ' / ' + t.gpu_ppt_peak_w, 'W') : ''}}
            <div style="margin-top:0.75rem">${{e.confidence.flag}} ${{e.confidence.label}}</div>
        </div>`;
    }}

    function renderBoth(r) {{
        const cpu = r.cpu;
        const gpu = r.gpu;
        const a = r.analysis;

        function col(res) {{
            const e = res.energy;
            const t = res.thermals;
            const isEnergyWinner = a.energy_winner === (res.preset_key === 'cpu' ? 'CPU' : 'GPU');
            const isSpeedWinner  = a.speed_winner  === (res.preset_key === 'cpu' ? 'CPU' : 'GPU');
            return `<div class="col">
                <h3>${{res.preset_label}}</h3>
                <div class="sub">${{res.preset_detail}}</div>
                <div class="section-title">Encode</div>
                ${{metricRow('Duration', e.delta_t_s + (isSpeedWinner ? ' 🏁' : ''), 's')}}
                ${{metricRow('Output size', res.output_size_mb, 'MB')}}
                <div class="section-title">Power (P110)</div>
                ${{metricRow('Baseline', e.w_base, 'W')}}
                ${{metricRow('Task mean', e.w_task, 'W')}}
                ${{metricRow('Peak delta', e.delta_w, 'W')}}
                ${{metricRow('Energy (ΔE)', e.delta_e_wh + (isEnergyWinner ? ' ✓' : ''), 'Wh')}}
                ${{metricRow('Polls', e.poll_count)}}
                <div class="section-title">Thermals</div>
                ${{metricRow('CPU base → peak', t.cpu_base + ' → ' + t.cpu_peak, '°C')}}
                ${{metricRow('GPU base → peak', t.gpu_base + ' → ' + t.gpu_peak, '°C')}}
                ${{t.gpu_ppt_mean_w ? metricRow('GPU PPT mean', t.gpu_ppt_mean_w, 'W') : ''}}
                <div style="margin-top:0.75rem;font-size:0.8rem">
                    ${{e.confidence.flag}} ${{e.confidence.label}}
                </div>
            </div>`;
        }}

        return `
        <div class="report">
            <h2>Comparison Report</h2>
            <div class="analysis-box">
                <h3>Finding</h3>
                <div class="finding">${{a.finding}}</div>
                <div class="conf-note">${{a.confidence_note}}</div>
            </div>
            <div class="cols">
                ${{col(cpu)}}
                ${{col(gpu)}}
            </div>
            <div class="scope-note">${{r.scope}}</div>
        </div>`;
    }}

    function downloadLinks(jobId) {{
        const base = '/results/video/' + jobId;
        return `<div style="margin-top:1rem;display:flex;gap:0.75rem">
            <a href="${{base}}/download.json" download
               style="color:#00ff99;font-size:0.8rem;border:1px solid #00ff9944;
                      padding:0.3rem 0.75rem;text-decoration:none">↓ JSON</a>
            <a href="${{base}}/download.csv" download
               style="color:#00ff99;font-size:0.8rem;border:1px solid #00ff9944;
                      padding:0.3rem 0.75rem;text-decoration:none">↓ CSV</a>
        </div>`;
    }}

    function renderResult(r, jobId) {{
        const el = document.getElementById('status');
        const elapsed = startTime ? wlFormatElapsed(Date.now() - startTime) : '';
        const elapsedNote = elapsed ? `<div style="color:#444;font-size:0.78rem;margin-bottom:1rem">
            Total elapsed: ${{elapsed}}</div>` : '';
        const links = jobId ? downloadLinks(jobId) : '';
        let html;
        if (r.mode === 'both') {{
            html = renderBoth(r) + links;
        }} else {{
            html = renderSingle(r.result) + links;
        }}
        el.innerHTML = elapsedNote + html;
        loadPrevRuns();
    }}

    async function loadPrevRuns() {{
        try {{
            const resp = await fetch('/results/video/list');
            const runs = await resp.json();
            renderPrevRuns(runs);
        }} catch(e) {{}}
    }}

    function renderPrevRuns(runs) {{
        const el = document.getElementById('prev-runs');
        if (!runs || runs.length === 0) {{
            el.innerHTML = '<div style="color:#333;font-size:0.8rem">No previous runs.</div>';
            return;
        }}
        const rows = runs.map(r => {{
            const date = r.saved_at ? r.saved_at.slice(0,16).replace('T',' ') : '—';
            let summary;
            if (r.mode === 'both') {{
                summary = `CPU ${{r.cpu_delta_e_wh}} Wh ${{r.cpu_confidence||''}} · GPU ${{r.gpu_delta_e_wh}} Wh ${{r.gpu_confidence||''}}`;
            }} else {{
                summary = `${{r.preset||''}} · ${{r.delta_e_wh}} Wh ${{r.confidence||''}}`;
            }}
            const base = '/results/video/' + r.job_id;
            return `<div style="border-bottom:1px solid #111;padding:0.6rem 0">
                <div style="display:flex;justify-content:space-between;align-items:baseline">
                    <span style="color:#e0e0e0;font-size:0.82rem">${{date}} · ${{r.mode}}</span>
                    <span style="color:#555;font-size:0.75rem;font-family:monospace">${{r.job_id}}</span>
                </div>
                <div style="color:#00ff99;font-size:0.8rem;margin:0.2rem 0">${{summary}}</div>
                <div style="display:flex;gap:0.5rem;margin-top:0.3rem">
                    <a href="${{base}}/download.json" download
                       style="color:#555;font-size:0.75rem;text-decoration:none">↓ JSON</a>
                    <a href="${{base}}/download.csv" download
                       style="color:#555;font-size:0.75rem;text-decoration:none">↓ CSV</a>
                </div>
            </div>`;
        }}).join('');
        el.innerHTML = `<div style="color:#444;font-size:0.72rem;text-transform:uppercase;
            letter-spacing:0.05em;margin-bottom:0.75rem">Previous runs</div>${{rows}}`;
    }}

    loadPrevRuns();
    const _resumeJob = new URLSearchParams(location.search).get('job');
    if (_resumeJob) {{ pollJob(_resumeJob, 'both'); }}
    </script>
    {_PROGRESS_JS}
    {_CONF_HELP_WIDGET}
    {_FOOTER}
</body>
</html>"""

# --- Job runner ---

async def run_job(job_id: str, input_path: Path, preset: str, delete_after: bool = True):
    try:
        jobs[job_id] = {"status": "running", "stage": "starting"}
        if preset == "both":
            result = await run_both_measurement(input_path, job_id, jobs)
        else:
            result = await run_video_measurement(input_path, job_id, preset, jobs)
        save_result("video", job_id, result)
        jobs[job_id] = {"status": "done", "stage": "done", "result": result}
    except Exception as e:
        jobs[job_id] = {"status": "error", "stage": "error", "error": str(e)}
    finally:
        if delete_after:
            input_path.unlink(missing_ok=True)


@app.post("/video/use-source")
async def use_preloaded_source(
    source_key: str = Form(...),
    preset: str = Form("both")
):
    if preset not in ("cpu", "gpu", "both", "h265_cpu", "h265_gpu", "av1_cpu"):
        return JSONResponse({"error": "Invalid preset"}, status_code=400)

    source = PRELOADED.get(source_key)
    if not source or not source["path"].exists():
        return JSONResponse({"error": f"Source '{source_key}' not found"}, status_code=404)

    job_id = str(uuid.uuid4())[:8]
    label = f"Video — {preset} · {source['label']}"

    async def coro():
        await run_job(job_id, source["path"], preset, False)

    position = enqueue(job_id, "video", label, coro)
    if position is None:
        return JSONResponse({"error": "Queue full — try again later."}, status_code=429)
    return {"job_id": job_id, "queue_position": position}

@app.post("/video/upload")
async def upload_video(
    file: UploadFile = File(...),
    preset: str = Form("both")
):
    if preset not in ("cpu", "gpu", "both", "h265_cpu", "h265_gpu", "av1_cpu"):
        return JSONResponse({"error": "Invalid preset"}, status_code=400)

    allowed = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".ts"}
    suffix = Path(file.filename).suffix.lower()
    if suffix not in allowed:
        return JSONResponse({"error": f"File type {suffix} not allowed"}, status_code=400)

    contents = await file.read()
    if len(contents) > 1024 * 1024 * 1024:
        return JSONResponse({"error": "File too large (max 1GB)"}, status_code=400)

    job_id = str(uuid.uuid4())[:8]
    input_path = UPLOAD_DIR / f"{job_id}_in{suffix}"
    input_path.write_bytes(contents)
    label = f"Video — {preset} · {file.filename}"

    async def coro():
        await run_job(job_id, input_path, preset, True)

    position = enqueue(job_id, "video", label, coro)
    if position is None:
        return JSONResponse({"error": "Queue full — try again later."}, status_code=429)
    return {"job_id": job_id, "queue_position": position}


@app.get("/video/sources")
async def video_sources():
    return get_all_sources()


# --- LLM job runner ---

async def run_llm_job(job_id: str, model_key: str, task_key: str,
                      repeats: int = 1, warm: bool = False, prompt: str = None,
                      device: str = "gpu"):
    try:
        jobs[job_id] = {"status": "running", "stage": "baseline", "partial_response": ""}
        if device == "both":
            result = await run_llm_both_measurement(
                model_key, task_key, jobs, job_id, warm, prompt)
        elif repeats > 1:
            result = await run_llm_batch_measurement(
                model_key, task_key, repeats, warm, prompt, jobs, job_id)
        else:
            result = await run_llm_measurement(
                model_key, task_key, jobs, job_id, warm, prompt, device)
        save_result("llm", job_id, result)
        jobs[job_id] = {"status": "done", "stage": "done", "result": result}
    except Exception as e:
        jobs[job_id] = {"status": "error", "stage": "error", "error": str(e)}

@app.get("/llm", response_class=HTMLResponse)
async def llm_page():
    models_html = "".join([
        f'''<div class="preset" id="model-{k}" onclick="selectModel('{k}')">
            <h3>{v["label"]}</h3>
            <p style="color:#555;font-size:0.75rem">{v["params"]} · {v["size"]}</p>
        </div>'''
        for k, v in MODELS.items()
    ])

    tasks_html = "".join([
        f'''<label style="display:flex;gap:0.75rem;border:1px solid #333;
                     padding:0.75rem;cursor:pointer;margin-bottom:0.5rem">
            <input type="radio" name="task" value="{k}"
                   {"checked" if k == "T1" else ""}
                   onchange="selectedTask='{k}'; document.getElementById('promptText').value=defaultPrompts['{k}']||''"
                   style="accent-color:#00ff99;margin-top:0.2rem">
            <div>
                <div style="color:#e0e0e0;font-size:0.85rem">{v["label"]}</div>
                <div style="color:#555;font-size:0.75rem">{v["prompt"][:80]}...</div>
            </div>
        </label>'''
        for k, v in TASKS.items()
    ])

    import json as _json
    tasks_js = _json.dumps({k: v["prompt"] for k, v in TASKS.items()})

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>WattLab — LLM Inference Test</title>
    <style>
        * {{ box-sizing:border-box; margin:0; padding:0; }}
        body {{ font-family:monospace; background:#0a0a0a; color:#e0e0e0;
               max-width:780px; margin:0 auto; padding:2rem; }}
        h1 {{ color:#00ff99; margin-bottom:0.25rem; font-size:1.6rem; }}
        .subtitle {{ color:#555; font-size:0.8rem; margin-bottom:1.5rem; }}
        .info {{ color:#777; font-size:0.82rem; margin-bottom:1.5rem;
                 border-left:2px solid #222; padding-left:1rem; line-height:1.6; }}
        .presets {{ display:flex; gap:0.75rem; margin-bottom:1.5rem; }}
        .preset {{ border:1px solid #333; padding:1rem; cursor:pointer; flex:1; }}
        .preset:hover {{ border-color:#00ff9966; }}
        .preset.selected {{ border-color:#00ff99; background:#00ff9911; }}
        .preset h3 {{ color:#00ff99; font-size:0.9rem; margin-bottom:0.4rem; }}
        .section-label {{ color:#555; font-size:0.75rem; text-transform:uppercase;
                          letter-spacing:0.05em; margin-bottom:0.75rem; }}
        button {{ background:#00ff99; color:#000; border:none; padding:0.75rem 2rem;
                  cursor:pointer; font-family:monospace; font-size:1rem; margin-top:1rem; }}
        button:disabled {{ background:#222; color:#555; cursor:not-allowed; }}
        button:hover:not(:disabled) {{ background:#00dd88; }}
        #status {{ margin-top:1.5rem; }}
        .result-box {{ border:1px solid #222; padding:1.5rem; }}
        .result-box h2 {{ color:#00ff99; font-size:1.1rem; margin-bottom:1rem;
                          padding-bottom:0.5rem; border-bottom:1px solid #222; }}
        .metric {{ display:flex; justify-content:space-between;
                   padding:0.3rem 0; border-bottom:1px solid #111; font-size:0.82rem; }}
        .val {{ color:#00ff99; }}
        .section-title {{ color:#444; font-size:0.72rem; text-transform:uppercase;
                          letter-spacing:0.05em; margin:0.75rem 0 0.4rem; }}
        .response-box {{ background:#111; padding:1rem; margin-top:0.75rem;
                         font-size:0.8rem; color:#aaa; line-height:1.6;
                         border-left:2px solid #00ff9944; max-height:500px;
                         overflow-y:auto; white-space:pre-wrap; }}
        .scope-note {{ color:#333; font-size:0.72rem; margin-top:1rem; }}
        .progress-box {{ border:1px solid #222; padding:1.5rem; }}
        .progress-header {{ color:#ffaa00; font-size:0.9rem; margin-bottom:1rem; }}
        .stage {{ display:flex; align-items:center; gap:0.75rem;
                  font-size:0.82rem; margin-bottom:0.4rem; }}
        .stage.active .stage-label {{ color:#ffaa00; }}
        .stage.done .stage-label {{ color:#00ff99; }}
        .stage.pending .stage-label {{ color:#333; }}
        a.back {{ color:#555; text-decoration:none; font-size:0.82rem;
                  display:inline-block; margin-top:1.5rem; }}
        a.back:hover {{ color:#00ff99; }}
    </style>
</head>
<body>
    {_BACK}
    <h1>LLM Inference Energy Test</h1>
    <div class="subtitle">Greening of Streaming · WattLab · GoS1</div>
    <div class="info">
        Fixed prompts for comparability · P110 at 1s intervals<br>
        Energy per token (mWh/token) is the primary metric<br>
        Scope: device layer only — no amortised training cost included
    </div>

    <div class="section-label">Model</div>
    <div class="presets">{models_html}</div>

    <div class="section-label">Task</div>
    {tasks_html}

    <div id="prompt-editor" style="margin-bottom:1.5rem">
        <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:0.4rem">
            <div style="color:#aaa;font-size:0.75rem;text-transform:uppercase;letter-spacing:0.05em">✎ Edit prompt</div>
            <button onclick="resetPrompt()" style="background:none;border:none;color:#555;
                font-size:0.75rem;cursor:pointer;padding:0;font-family:monospace">Reset to default</button>
        </div>
        <textarea id="promptText" rows="3"
            style="width:100%;background:#0f0f0f;border:1px solid #444;border-left:2px solid #00ff9966;
                   color:#ccc;font-family:monospace;font-size:0.8rem;padding:0.75rem;
                   resize:vertical;line-height:1.5"></textarea>
    </div>

    <div style="display:flex;gap:2rem;margin-bottom:1.5rem;flex-wrap:wrap">
        <div>
            <div style="color:#555;font-size:0.75rem;text-transform:uppercase;
                        letter-spacing:0.05em;margin-bottom:0.5rem">Backend</div>
            <div style="display:flex;gap:0.75rem">
                <label style="display:flex;align-items:center;gap:0.4rem;cursor:pointer;font-size:0.85rem">
                    <input type="radio" name="device" value="gpu" checked
                           onchange="selectedDevice='gpu'" style="accent-color:#00ff99"> GPU
                </label>
                <label style="display:flex;align-items:center;gap:0.4rem;cursor:pointer;font-size:0.85rem">
                    <input type="radio" name="device" value="cpu"
                           onchange="selectedDevice='cpu'" style="accent-color:#00ff99"> CPU
                </label>
                <label style="display:flex;align-items:center;gap:0.4rem;cursor:pointer;font-size:0.85rem">
                    <input type="radio" name="device" value="both"
                           onchange="selectedDevice='both'" style="accent-color:#00ff99"> Both ⚡
                </label>
            </div>
            <div style="color:#333;font-size:0.72rem;margin-top:0.3rem">
                Both: CPU then GPU with new baseline — full side-by-side comparison
            </div>
        </div>
        <div>
            <div style="color:#555;font-size:0.75rem;text-transform:uppercase;
                        letter-spacing:0.05em;margin-bottom:0.5rem">Mode</div>
            <div style="display:flex;gap:0.75rem">
                <label style="display:flex;align-items:center;gap:0.4rem;cursor:pointer;font-size:0.85rem">
                    <input type="radio" name="warmMode" value="cold" checked
                           onchange="selectedWarm=false" style="accent-color:#00ff99"> Cold
                </label>
                <label style="display:flex;align-items:center;gap:0.4rem;cursor:pointer;font-size:0.85rem">
                    <input type="radio" name="warmMode" value="warm"
                           onchange="selectedWarm=true" style="accent-color:#00ff99"> Warm
                </label>
            </div>
            <div style="color:#333;font-size:0.72rem;margin-top:0.3rem">
                Cold: unload model before baseline · Warm: model stays loaded
            </div>
        </div>
        <div>
            <div style="color:#555;font-size:0.75rem;text-transform:uppercase;
                        letter-spacing:0.05em;margin-bottom:0.5rem">Repeats</div>
            <div style="display:flex;gap:0.75rem">
                <label style="display:flex;align-items:center;gap:0.4rem;cursor:pointer;font-size:0.85rem">
                    <input type="radio" name="repeats" value="1" checked
                           onchange="selectedRepeats=1" style="accent-color:#00ff99"> 1×
                </label>
                <label style="display:flex;align-items:center;gap:0.4rem;cursor:pointer;font-size:0.85rem">
                    <input type="radio" name="repeats" value="3"
                           onchange="selectedRepeats=3" style="accent-color:#00ff99"> 3×
                </label>
                <label style="display:flex;align-items:center;gap:0.4rem;cursor:pointer;font-size:0.85rem">
                    <input type="radio" name="repeats" value="5"
                           onchange="selectedRepeats=5" style="accent-color:#00ff99"> 5×
                </label>
            </div>
            <div style="color:#333;font-size:0.72rem;margin-top:0.3rem">
                Batch: load once, 10s rest between runs
            </div>
        </div>
    </div>

    <div style="display:flex;gap:0.75rem;flex-wrap:wrap">
        <button id="runBtn" onclick="runInference()">Run Measurement</button>
        <button id="runAllBtn" onclick="runAllTasks()"
            style="background:#0a0a0a;border:1px solid #00ff9966;color:#00ff99;
                   padding:0.65rem 1.25rem;font-family:monospace;font-size:0.85rem;cursor:pointer">
            Run All Tasks (T1+T2+T3)
        </button>
    </div>
    <div id="status"></div>
    <div id="prev-runs" style="margin-top:2rem;border-top:1px solid #111;padding-top:1.5rem"></div>

    <script>
    let selectedModel = 'tinyllama';
    let selectedTask = 'T1';
    let selectedWarm = false;
    let selectedRepeats = 1;
    let selectedDevice = 'gpu';
    let startTime = null;
    let streamTimer = null;

    const defaultPrompts = {tasks_js};

    // Select first model by default and populate prompt
    document.getElementById('model-tinyllama').classList.add('selected');
    document.getElementById('promptText').value = defaultPrompts['T1'] || '';

    function selectModel(key) {{
        selectedModel = key;
        document.querySelectorAll('.preset').forEach(el => el.classList.remove('selected'));
        document.getElementById('model-' + key).classList.add('selected');
    }}

    function resetPrompt() {{
        document.getElementById('promptText').value = defaultPrompts[selectedTask] || '';
    }}

    function renderProgress(stage, watts) {{
        const isBoth = stage.startsWith('baseline_cpu') || stage.startsWith('cpu_') ||
                       stage.startsWith('baseline_gpu') || stage.startsWith('gpu_') ||
                       stage === 'cooldown';
        const displayStage = stage.startsWith('inference_') ? 'inference' :
                             stage.startsWith('rest_') ? 'rest' :
                             stage.startsWith('cpu_inference') ? 'cpu_inference' :
                             stage.startsWith('gpu_inference') ? 'gpu_inference' : stage;
        const stageLabel = stage.startsWith('inference_') ? 'Running inference (' + stage.replace('inference_','').replace('_',' ') + ')' :
                           stage.startsWith('rest_') ? 'Resting between runs\u2026' : null;
        const stages = isBoth ? [
            ['baseline_cpu', 'Measuring CPU baseline'],
            ['cpu_inference', 'CPU inference (num_gpu=0)'],
            ['cooldown', 'Cooldown between runs'],
            ['baseline_gpu', 'Measuring GPU baseline'],
            ['gpu_inference', 'GPU inference (ROCm)'],
            ['done', 'Done'],
        ] : [
            ['baseline', 'Measuring baseline'],
            ['inference', stageLabel || 'Running inference'],
            ['rest', 'Resting between runs\u2026'],
            ['done', 'Done'],
        ].filter(([k]) => k !== 'rest' || displayStage === 'rest');
        const stageIdx = stages.findIndex(([k]) => k === displayStage);
        wlRenderProgress({{
            header: 'Running \u2014 do not close this tab',
            stagesHtml: wlStageList(stages.map(([k,l]) => l), stageIdx < 0 ? 0 : stageIdx),
            watts: watts,
            elapsed: startTime ? Date.now() - startTime : null,
            extraHtml: '<div id="stream-preview" style="margin-top:0.75rem;background:#111;'
                + 'padding:0.75rem;font-size:0.78rem;color:#888;line-height:1.6;'
                + 'min-height:2rem;border-left:2px solid #00ff9933;max-height:120px;'
                + 'overflow-y:auto;white-space:pre-wrap"></div>',
        }});
    }}

    async function runInference() {{
        const btn = document.getElementById('runBtn');
        btn.disabled = true;
        startTime = Date.now();

        const form = new FormData();
        form.append('model_key', selectedModel);
        form.append('task_key', selectedTask);
        form.append('repeats', selectedRepeats);
        form.append('warm', selectedWarm ? 'true' : 'false');
        form.append('device', selectedDevice);
        const promptVal = document.getElementById('promptText').value.trim();
        if (promptVal) form.append('prompt', promptVal);

        try {{
            const resp = await fetch('/llm/run', {{method:'POST', body:form}});
            const data = await resp.json();
            if (data.job_id) {{
                renderProgress('baseline');
                pollLLM(data.job_id);
            }} else {{
                document.getElementById('status').innerHTML =
                    '<div style="color:#ff4400">Error: ' + JSON.stringify(data) + '</div>';
                btn.disabled = false;
            }}
        }} catch(e) {{
            document.getElementById('status').innerHTML =
                '<div style="color:#ff4400">Failed: ' + e + '</div>';
            btn.disabled = false;
        }}
    }}

    async function pollLLM(jobId) {{
        try {{
            const [resp, powerR] = await Promise.all([
                fetch('/llm/job/' + jobId),
                fetch('/power').catch(() => null),
            ]);
            const data = await resp.json();
            const watts = powerR ? (await powerR.json().catch(()=>({{}}))).watts ?? null : null;
            if (data.status === 'done') {{
                if (streamTimer) {{ clearTimeout(streamTimer); streamTimer = null; }}
                renderLLMResult(data.result, jobId);
                document.getElementById('runBtn').disabled = false;
            }} else if (data.status === 'error') {{
                if (streamTimer) {{ clearTimeout(streamTimer); streamTimer = null; }}
                document.getElementById('status').innerHTML =
                    '<div style="color:#ff4400">Error: ' + data.error + '</div>';
                document.getElementById('runBtn').disabled = false;
            }} else if (data.stage === 'queued') {{
                wlRenderQueued(data.queue_position);
                streamTimer = setTimeout(() => pollLLM(jobId), 3000);
            }} else {{
                const stage = data.stage || 'baseline';
                renderProgress(stage, watts);
                if (stage.startsWith('inference') && data.partial_response) {{
                    const box = document.getElementById('stream-preview');
                    if (box) box.textContent = data.partial_response;
                }}
                const delay = stage.startsWith('inference') ? 500 : 5000;
                streamTimer = setTimeout(() => pollLLM(jobId), delay);
            }}
        }} catch(e) {{
            streamTimer = setTimeout(() => pollLLM(jobId), 5000);
        }}
    }}

    function renderLLMResult(r, jobId) {{
        const elapsed = startTime ? wlFormatElapsed(Date.now() - startTime) : '';
        const base = '/results/llm/' + jobId;
        const links = jobId ? `<div style="margin-top:1rem;display:flex;gap:0.75rem">
            <a href="${{base}}/download.json" download
               style="color:#00ff99;font-size:0.8rem;border:1px solid #00ff9944;
                      padding:0.3rem 0.75rem;text-decoration:none">↓ JSON</a>
            <a href="${{base}}/download.csv" download
               style="color:#00ff99;font-size:0.8rem;border:1px solid #00ff9944;
                      padding:0.3rem 0.75rem;text-decoration:none">↓ CSV</a>
        </div>` : '';
        const elapsedNote = elapsed ? `<div style="color:#444;font-size:0.78rem;margin-bottom:1rem">
            Total elapsed: ${{elapsed}}</div>` : '';

        let body;
        if (r.mode === 'both') {{
            body = renderLLMBoth(r);
        }} else if (r.mode === 'batch') {{
            body = renderLLMBatch(r);
        }} else if (r.mode === 'all') {{
            body = renderLLMAll(r);
        }} else if (r.mode === 'all_both') {{
            body = renderLLMAllBoth(r);
        }} else {{
            body = renderLLMSingle(r);
        }}
        document.getElementById('status').innerHTML = elapsedNote + body + links;
        loadPrevRuns();
    }}

    function renderLLMSingle(r) {{
        const e = r.energy;
        const i = r.inference;
        const t = r.thermals;
        const modeNote = r.warm ? '🌡 Warm (model pre-loaded)' : '❄ Cold (model unloaded before baseline)';
        return `<div class="result-box">
                <h2>Energy Report — ${{r.model_label}} · ${{r.task_label}}</h2>
                <div class="section-title">Inference</div>
                <div class="metric"><span>Model</span><span class="val">${{r.model_label}} (${{r.model_params}})</span></div>
                <div class="metric"><span>Task</span><span class="val">${{r.task_label}}</span></div>
                <div class="metric"><span>Mode</span><span class="val">${{modeNote}}</span></div>
                <div class="metric"><span>Output tokens</span><span class="val">${{i.output_tokens}}</span></div>
                <div class="metric"><span>Tokens/sec</span><span class="val">${{i.tokens_per_sec}}</span></div>
                <div class="metric"><span>Duration</span><span class="val">${{i.duration_s}}s</span></div>
                <div class="section-title">Power (P110)</div>
                <div class="metric"><span>Baseline</span><span class="val">${{e.w_base}} W</span></div>
                <div class="metric"><span>Task mean</span><span class="val">${{e.w_task}} W</span></div>
                <div class="metric"><span>Delta (ΔW)</span><span class="val">${{e.delta_w}} W</span></div>
                <div class="metric"><span>Energy (ΔE)</span><span class="val">${{e.delta_e_wh}} Wh</span></div>
                <div class="metric"><span>Energy/token</span>
                    <span class="val">${{e.mwh_per_token}} mWh/token</span></div>
                <div class="metric"><span>Polls</span><span class="val">${{e.poll_count}}</span></div>
                <div class="section-title">Thermals</div>
                <div class="metric"><span>CPU (start→end)</span>
                    <span class="val">${{t.cpu_base}}→${{t.cpu_end}}°C</span></div>
                <div class="metric"><span>GPU (start→end)</span>
                    <span class="val">${{t.gpu_base}}→${{t.gpu_end}}°C</span></div>
                <div style="margin-top:0.75rem">${{e.confidence.flag}} ${{e.confidence.label}}</div>
                <div class="section-title">Response preview</div>
                <div class="response-box">${{i.response}}</div>
                <div class="scope-note">${{r.scope}}</div>
            </div>`;
    }}

    function renderLLMBatch(r) {{
        const agg = r.aggregate;
        const t = r.thermals;
        const modeNote = r.warm ? '🌡 Warm' : '❄ Cold';
        const runsRows = r.runs.map(run => {{
            const e = run.energy;
            const i = run.inference;
            return `<tr>
                <td style="color:#888">${{run.run}}</td>
                <td>${{i.output_tokens}}</td>
                <td>${{i.tokens_per_sec}}</td>
                <td>${{e.delta_e_wh}} Wh</td>
                <td>${{e.mwh_per_token}} mWh/tok</td>
                <td>${{e.confidence.flag}}</td>
            </tr>`;
        }}).join('');
        return `<div class="result-box">
                <h2>Batch Report — ${{r.model_label}} · ${{r.task_label}}</h2>
                <div class="section-title">Run parameters</div>
                <div class="metric"><span>Model</span><span class="val">${{r.model_label}} (${{r.model_params}})</span></div>
                <div class="metric"><span>Task</span><span class="val">${{r.task_label}}</span></div>
                <div class="metric"><span>Mode</span><span class="val">${{modeNote}} · ${{r.repeats}}× runs · 10s rest</span></div>
                <div class="section-title">Aggregate</div>
                <div class="metric"><span>Energy/token (mean)</span>
                    <span class="val">${{agg.mwh_per_token_mean}} mWh/token</span></div>
                <div class="metric"><span>Energy/token (σ)</span>
                    <span class="val">${{agg.mwh_per_token_stddev ?? '—'}}</span></div>
                <div class="metric"><span>Energy per run (mean)</span>
                    <span class="val">${{agg.delta_e_wh_mean}} Wh</span></div>
                <div class="metric"><span>Energy per run (σ)</span>
                    <span class="val">${{agg.delta_e_wh_stddev ?? '—'}}</span></div>
                <div class="metric"><span>Tokens/sec (mean)</span>
                    <span class="val">${{agg.tokens_per_sec_mean}}</span></div>
                <div class="section-title">Per-run breakdown</div>
                <table style="width:100%;border-collapse:collapse;font-size:0.78rem">
                    <thead><tr style="color:#444;text-align:left">
                        <th style="padding:0.3rem 0.5rem 0.3rem 0">#</th>
                        <th style="padding:0.3rem 0.5rem">Tokens</th>
                        <th style="padding:0.3rem 0.5rem">Tok/s</th>
                        <th style="padding:0.3rem 0.5rem">ΔE</th>
                        <th style="padding:0.3rem 0.5rem">mWh/tok</th>
                        <th style="padding:0.3rem 0.5rem">Conf</th>
                    </tr></thead>
                    <tbody style="color:#ccc">${{runsRows}}</tbody>
                </table>
                <div class="section-title">Thermals</div>
                <div class="metric"><span>CPU (start→end)</span>
                    <span class="val">${{t.cpu_base}}→${{t.cpu_end}}°C</span></div>
                <div class="metric"><span>GPU (start→end)</span>
                    <span class="val">${{t.gpu_base}}→${{t.gpu_end}}°C</span></div>
                <div class="section-title">Response preview (last run)</div>
                <div class="response-box">${{r.runs[r.runs.length-1].inference.response}}</div>
                <div class="scope-note">${{r.scope}}</div>
            </div>`;
    }}

    function renderLLMBoth(r) {{
        const a = r.analysis;
        const cpu = r.cpu, gpu = r.gpu;
        const ce = cpu.energy, ge = gpu.energy;
        const ci = cpu.inference, gi = gpu.inference;
        const winnerColor = (winner, side) => winner === side ? '#00ff99' : '#888';
        return `<div class="result-box">
            <h2>CPU vs GPU — ${{r.model_label}} · ${{r.task_label}}</h2>
            <div style="background:#0d1a0d;border:1px solid #00ff9933;
                        padding:1rem;margin-bottom:1.25rem;font-size:0.82rem;line-height:1.7">
              ${{a.finding}}
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-bottom:1.25rem">
              <div style="border:1px solid #222;padding:1rem">
                <div style="color:#888;font-size:0.72rem;margin-bottom:0.75rem">CPU (num_gpu=0 · Ryzen 9 7900)</div>
                <div class="metric"><span>Tokens/sec</span>
                  <span class="val" style="color:${{winnerColor(a.speed_winner,'CPU')}}">${{ci.tokens_per_sec}}</span></div>
                <div class="metric"><span>Duration</span><span class="val">${{ci.duration_s}}s</span></div>
                <div class="metric"><span>ΔE total</span>
                  <span class="val" style="color:${{winnerColor(a.energy_winner,'CPU')}}">${{ce.delta_e_wh}} Wh</span></div>
                <div class="metric"><span>mWh/token</span>
                  <span class="val" style="color:${{winnerColor(a.mwh_winner,'CPU')}}">${{ce.mwh_per_token}}</span></div>
                <div class="metric"><span>ΔW</span><span class="val">${{ce.delta_w}} W</span></div>
                <div style="margin-top:0.5rem">${{ce.confidence.flag}} ${{ce.confidence.label}}</div>
              </div>
              <div style="border:1px solid #222;padding:1rem">
                <div style="color:#888;font-size:0.72rem;margin-bottom:0.75rem">GPU (ROCm · RX 7800 XT)</div>
                <div class="metric"><span>Tokens/sec</span>
                  <span class="val" style="color:${{winnerColor(a.speed_winner,'GPU')}}">${{gi.tokens_per_sec}}</span></div>
                <div class="metric"><span>Duration</span><span class="val">${{gi.duration_s}}s</span></div>
                <div class="metric"><span>ΔE total</span>
                  <span class="val" style="color:${{winnerColor(a.energy_winner,'GPU')}}">${{ge.delta_e_wh}} Wh</span></div>
                <div class="metric"><span>mWh/token</span>
                  <span class="val" style="color:${{winnerColor(a.mwh_winner,'GPU')}}">${{ge.mwh_per_token}}</span></div>
                <div class="metric"><span>ΔW</span><span class="val">${{ge.delta_w}} W</span></div>
                <div style="margin-top:0.5rem">${{ge.confidence.flag}} ${{ge.confidence.label}}</div>
              </div>
            </div>
            <div class="section-title">GPU response preview</div>
            <div class="response-box">${{gi.response}}</div>
            <div class="scope-note">${{r.scope}}</div>
        </div>`;
    }}

    function renderLLMAll(r) {{
        const taskLabels = {{'T1': 'Short factual', 'T2': 'Medium reasoning', 'T3': 'Long generation'}};
        const cards = Object.entries(r.tasks).map(([key, t]) => {{
            const e = t.energy;
            const i = t.inference;
            return `<div style="border:1px solid #222;padding:1rem;margin-bottom:0.75rem">
                <div style="color:#00ff99;font-size:0.78rem;margin-bottom:0.75rem">${{key}} — ${{taskLabels[key] || key}}</div>
                <div class="metric"><span>Output tokens</span><span class="val">${{i.output_tokens}}</span></div>
                <div class="metric"><span>Tokens/sec</span><span class="val">${{i.tokens_per_sec}}</span></div>
                <div class="metric"><span>Duration</span><span class="val">${{i.duration_s}}s</span></div>
                <div class="metric"><span>ΔE</span><span class="val">${{e.delta_e_wh}} Wh</span></div>
                <div class="metric"><span>mWh/token</span><span class="val">${{e.mwh_per_token}}</span></div>
                <div class="metric"><span>ΔW</span><span class="val">${{e.delta_w}} W</span></div>
                <div style="margin-top:0.5rem;font-size:0.82rem">${{e.confidence.flag}} ${{e.confidence.label}}</div>
                <div class="section-title" style="margin-top:0.75rem">Response preview</div>
                <div class="response-box">${{i.response}}</div>
            </div>`;
        }}).join('');
        return `<div class="result-box">
            <h2>All Tasks — ${{r.model_label}} (${{r.model_params}})</h2>
            <div style="color:#555;font-size:0.78rem;margin-bottom:1rem">
                ${{r.warm ? '🌡 Warm' : '❄ Cold'}} · ${{r.device.toUpperCase()}} · 3 tasks
            </div>
            ${{cards}}
            <div class="scope-note">${{r.scope}}</div>
        </div>`;
    }}

    function renderLLMAllBoth(r) {{
        const taskLabels = {{'T1':'Short factual','T2':'Medium reasoning','T3':'Long generation'}};
        const winCol = (cpu_val, gpu_val, lower_is_better) => {{
            if (cpu_val == null || gpu_val == null) return ['#ccc','#ccc'];
            const cpuWins = lower_is_better ? cpu_val <= gpu_val : cpu_val >= gpu_val;
            return cpuWins ? ['#00ff99','#888'] : ['#888','#00ff99'];
        }};
        const rows = Object.keys(taskLabels).map(tk => {{
            const cpu = r.cpu[tk] || {{}};
            const gpu = r.gpu[tk] || {{}};
            const ce = cpu.energy || {{}};
            const ge = gpu.energy || {{}};
            const ci = cpu.inference || {{}};
            const gi = gpu.inference || {{}};
            const [cSpeedCol, gSpeedCol] = winCol(ci.tokens_per_sec, gi.tokens_per_sec, false);
            const [cECol, gECol] = winCol(ce.mwh_per_token, ge.mwh_per_token, true);
            return `<tr style="border-bottom:1px solid #111">
                <td style="padding:0.5rem 0.75rem 0.5rem 0;color:#888;font-size:0.78rem">${{tk}}<br><span style="font-size:0.7rem;color:#444">${{taskLabels[tk]}}</span></td>
                <td style="padding:0.5rem 0.75rem;font-size:0.8rem;color:${{cSpeedCol}}">${{ci.tokens_per_sec ?? '—'}}</td>
                <td style="padding:0.5rem 0.75rem;font-size:0.8rem;color:${{gSpeedCol}}">${{gi.tokens_per_sec ?? '—'}}</td>
                <td style="padding:0.5rem 0.75rem;font-size:0.8rem;color:${{cECol}}">${{ce.mwh_per_token ?? '—'}}</td>
                <td style="padding:0.5rem 0.75rem;font-size:0.8rem;color:${{gECol}}">${{ge.mwh_per_token ?? '—'}}</td>
                <td style="padding:0.5rem 0;font-size:0.78rem">${{ce.confidence ? ce.confidence.flag : ''}} ${{ge.confidence ? ge.confidence.flag : ''}}</td>
            </tr>`;
        }}).join('');
        return `<div class="result-box">
            <h2>All Tasks CPU vs GPU — ${{r.model_label}} (${{r.model_params}})</h2>
            <div style="color:#555;font-size:0.78rem;margin-bottom:1rem">${{r.warm ? '🌡 Warm' : '❄ Cold'}} · 3 tasks × 2 backends</div>
            <table style="width:100%;border-collapse:collapse">
                <thead><tr style="color:#444;font-size:0.72rem;text-align:left;border-bottom:1px solid #222">
                    <th style="padding:0.4rem 0.75rem 0.4rem 0">Task</th>
                    <th style="padding:0.4rem 0.75rem">CPU tok/s</th>
                    <th style="padding:0.4rem 0.75rem">GPU tok/s</th>
                    <th style="padding:0.4rem 0.75rem">CPU mWh/tok</th>
                    <th style="padding:0.4rem 0.75rem">GPU mWh/tok</th>
                    <th style="padding:0.4rem 0">Conf</th>
                </tr></thead>
                <tbody>${{rows}}</tbody>
            </table>
            <div class="scope-note" style="margin-top:1rem">${{r.scope}}</div>
        </div>`;
    }}

    async function runAllTasks() {{
        const btn = document.getElementById('runAllBtn');
        const runBtn = document.getElementById('runBtn');
        btn.disabled = true;
        runBtn.disabled = true;
        startTime = Date.now();

        const form = new FormData();
        form.append('model_key', selectedModel);
        form.append('warm', selectedWarm ? 'true' : 'false');
        form.append('device', selectedDevice);  // cpu / gpu / both all supported

        try {{
            const resp = await fetch('/llm/run-all', {{method:'POST', body:form}});
            const data = await resp.json();
            if (data.job_id) {{
                renderProgress('T1_baseline');
                pollLLMAll(data.job_id);
            }} else {{
                document.getElementById('status').innerHTML =
                    '<div style="color:#ff4400">Error: ' + JSON.stringify(data) + '</div>';
                btn.disabled = false; runBtn.disabled = false;
            }}
        }} catch(e) {{
            document.getElementById('status').innerHTML =
                '<div style="color:#ff4400">Failed: ' + e + '</div>';
            btn.disabled = false; runBtn.disabled = false;
        }}
    }}

    async function pollLLMAll(jobId) {{
        try {{
            const [resp, powerR] = await Promise.all([
                fetch('/llm/job/' + jobId),
                fetch('/power').catch(() => null),
            ]);
            const data = await resp.json();
            const watts = powerR ? (await powerR.json().catch(()=>({{}}))).watts ?? null : null;
            if (data.status === 'done') {{
                if (streamTimer) {{ clearTimeout(streamTimer); streamTimer = null; }}
                renderLLMResult(data.result, jobId);
                document.getElementById('runBtn').disabled = false;
                document.getElementById('runAllBtn').disabled = false;
            }} else if (data.status === 'error') {{
                if (streamTimer) {{ clearTimeout(streamTimer); streamTimer = null; }}
                document.getElementById('status').innerHTML =
                    '<div style="color:#ff4400">Error: ' + data.error + '</div>';
                document.getElementById('runBtn').disabled = false;
                document.getElementById('runAllBtn').disabled = false;
            }} else if (data.stage === 'queued') {{
                wlRenderQueued(data.queue_position);
                streamTimer = setTimeout(() => pollLLMAll(jobId), 3000);
            }} else {{
                const task = data.current_task || 'T1';
                const dev = data.current_device || '';
                const taskNums = {{'T1':1,'T2':2,'T3':3}};
                const taskNum = taskNums[task] || 1;
                const devBadge = dev ? ' (' + dev.toUpperCase() + ')' : '';
                const taskPips = ['T1','T2','T3'].map(k => {{
                    const s = k === task ? 'active' : taskNums[k] < taskNum ? 'done' : 'pending';
                    const color = s === 'done' ? '#00ff99' : s === 'active' ? '#ffaa00' : '#333';
                    return '<span style="border:1px solid ' + color + ';padding:0.2rem 0.5rem;font-size:0.78rem;color:' + color + '">' + k + '</span>';
                }}).join('');
                wlRenderProgress({{
                    header: 'Running All Tasks \u2014 do not close this tab',
                    stagesHtml: '<div style="display:flex;gap:0.5rem;margin-bottom:0.5rem">' + taskPips + '</div>'
                        + '<div style="color:#888;font-size:0.8rem;margin-bottom:0.25rem">' + task + devBadge + '</div>',
                    watts: watts,
                    elapsed: startTime ? Date.now() - startTime : null,
                }});
                streamTimer = setTimeout(() => pollLLMAll(jobId), 3000);
            }}
        }} catch(e) {{
            streamTimer = setTimeout(() => pollLLMAll(jobId), 5000);
        }}
    }}

    async function loadPrevRuns() {{
        try {{
            const resp = await fetch('/results/llm/list');
            const runs = await resp.json();
            renderPrevRuns(runs);
        }} catch(e) {{}}
    }}

    function renderPrevRuns(runs) {{
        const el = document.getElementById('prev-runs');
        if (!runs || runs.length === 0) {{
            el.innerHTML = '<div style="color:#333;font-size:0.8rem">No previous runs.</div>';
            return;
        }}
        const rows = runs.map(r => {{
            const date = r.saved_at ? r.saved_at.slice(0,16).replace('T',' ') : '—';
            const summary = `${{r.model||''}} · ${{r.task||''}} · ${{r.mwh_per_token}} mWh/tok · ${{r.tokens_per_sec}} tok/s ${{r.confidence||''}}`;
            const base = '/results/llm/' + r.job_id;
            return `<div style="border-bottom:1px solid #111;padding:0.6rem 0">
                <div style="display:flex;justify-content:space-between;align-items:baseline">
                    <span style="color:#e0e0e0;font-size:0.82rem">${{date}}</span>
                    <span style="color:#555;font-size:0.75rem;font-family:monospace">${{r.job_id}}</span>
                </div>
                <div style="color:#00ff99;font-size:0.8rem;margin:0.2rem 0">${{summary}}</div>
                <div style="display:flex;gap:0.5rem;margin-top:0.3rem">
                    <a href="${{base}}/download.json" download
                       style="color:#555;font-size:0.75rem;text-decoration:none">↓ JSON</a>
                    <a href="${{base}}/download.csv" download
                       style="color:#555;font-size:0.75rem;text-decoration:none">↓ CSV</a>
                </div>
            </div>`;
        }}).join('');
        el.innerHTML = `<div style="color:#444;font-size:0.72rem;text-transform:uppercase;
            letter-spacing:0.05em;margin-bottom:0.75rem">Previous runs</div>${{rows}}`;
    }}

    loadPrevRuns();
    const _resumeJob = new URLSearchParams(location.search).get('job');
    if (_resumeJob) {{ pollLLM(_resumeJob); }}
    </script>
    {_PROGRESS_JS}
    {_CONF_HELP_WIDGET}
    {_FOOTER}
</body>
</html>"""

@app.post("/llm/run")
async def llm_run(
    model_key: str = Form(...),
    task_key: str = Form(...),
    repeats: int = Form(1),
    warm: bool = Form(False),
    prompt: str = Form(None),
    device: str = Form("gpu"),
):
    if model_key not in MODELS:
        return JSONResponse({"error": "Invalid model"}, status_code=400)
    if task_key not in TASKS:
        return JSONResponse({"error": "Invalid task"}, status_code=400)
    if device not in ("cpu", "gpu", "both"):
        return JSONResponse({"error": "device must be cpu, gpu, or both"}, status_code=400)
    if repeats not in (1, 3, 5):
        return JSONResponse({"error": "repeats must be 1, 3, or 5"}, status_code=400)

    effective_prompt = prompt.strip() if prompt and prompt.strip() else None
    job_id = str(uuid.uuid4())[:8]
    device_label = "CPU vs GPU" if device == "both" else device.upper()
    label = f"LLM — {MODELS[model_key]['label']} · {TASKS[task_key]['label']} · {device_label}"

    async def coro():
        await run_llm_job(job_id, model_key, task_key, repeats, warm, effective_prompt, device)

    position = enqueue(job_id, "llm", label, coro)
    if position is None:
        return JSONResponse({"error": "Queue full — try again later."}, status_code=429)
    return {"job_id": job_id, "queue_position": position}


async def run_llm_all_job(job_id: str, model_key: str, warm: bool, device: str):
    try:
        devices = ["cpu", "gpu"] if device == "both" else [device]
        jobs[job_id] = {"status": "running", "stage": "baseline",
                        "current_task": "T1", "current_device": devices[0], "partial_response": ""}
        dev_results = {}
        for dev in devices:
            task_results = {}
            for task_key in ["T1", "T2", "T3"]:
                jobs[job_id]["current_task"] = task_key
                jobs[job_id]["current_device"] = dev
                result = await run_llm_measurement(
                    model_key, task_key, jobs, job_id, warm, None, dev)
                task_results[task_key] = result
            dev_results[dev] = task_results

        if device == "both":
            final = {
                "mode": "all_both",
                "model_key": model_key,
                "model_label": MODELS[model_key]["label"],
                "model_params": MODELS[model_key]["params"],
                "warm": warm,
                "device": device,
                "cpu": dev_results["cpu"],
                "gpu": dev_results["gpu"],
                "scope": "Device layer only (GoS1). Network and CPE excluded. No amortised training cost.",
            }
        else:
            final = {
                "mode": "all",
                "model_key": model_key,
                "model_label": MODELS[model_key]["label"],
                "model_params": MODELS[model_key]["params"],
                "warm": warm,
                "device": device,
                "tasks": dev_results[device],
                "scope": "Device layer only (GoS1). Network and CPE excluded. No amortised training cost.",
            }
        save_result("llm", job_id, final)
        jobs[job_id] = {"status": "done", "stage": "done", "result": final}
    except Exception as e:
        jobs[job_id] = {"status": "error", "stage": "error", "error": str(e)}


@app.post("/llm/run-all")
async def llm_run_all(
    model_key: str = Form(...),
    warm: bool = Form(False),
    device: str = Form("gpu"),
):
    if model_key not in MODELS:
        return JSONResponse({"error": "Invalid model"}, status_code=400)
    if device not in ("cpu", "gpu", "both"):
        return JSONResponse({"error": "device must be cpu, gpu, or both"}, status_code=400)

    job_id = str(uuid.uuid4())[:8]
    label = f"LLM All Tasks — {MODELS[model_key]['label']} · {device.upper()}"

    async def coro():
        await run_llm_all_job(job_id, model_key, warm, device)

    position = enqueue(job_id, "llm", label, coro)
    if position is None:
        return JSONResponse({"error": "Queue full — try again later."}, status_code=429)
    return {"job_id": job_id, "queue_position": position}


@app.get("/llm/job/{job_id}")
async def llm_job_status(job_id: str):
    return jobs.get(job_id, {"status": "not_found"})

@app.get("/video/job/{job_id}")
async def job_status(job_id: str):
    return jobs.get(job_id, {"status": "not_found"})

@app.get("/image/job/{job_id}")
async def image_job_status(job_id: str):
    return jobs.get(job_id, {"status": "not_found"})

@app.get("/queue")
async def queue_status_endpoint():
    running = None
    if current_job_id and current_job_id in jobs:
        j = jobs[current_job_id]
        running = {"job_id": current_job_id, "stage": j.get("stage"), "type": j.get("type"), "label": j.get("label")}
    pending_info = [
        {"job_id": e["job_id"], "type": e["type"], "label": e["label"], "position": i + 1}
        for i, e in enumerate(pending_queue)
    ]
    return {
        "depth": len(pending_queue) + (1 if current_job_id else 0),
        "running": running,
        "pending": pending_info,
    }


# --- RAG page and endpoints ---

@app.get("/rag", response_class=HTMLResponse)
async def rag_page():
    models_html = "".join([
        f'''<div class="preset" id="rmodel-{k}" onclick="selectRModel('{k}')">
            <h3>{v["label"]}</h3>
            <p style="color:#555;font-size:0.75rem">{v["params"]} · {v["size"]}</p>
        </div>'''
        for k, v in rag_module.MODELS.items()
    ])

    queue_depth = len(pending_queue) + (1 if current_job_id else 0)
    busy_banner = (f'<div style="background:#333;color:#ffaa00;padding:0.75rem 1rem;'
                   f'margin-bottom:1rem;font-size:0.85rem">'
                   f'⏱ {queue_depth} job{"s" if queue_depth != 1 else ""} in queue — '
                   f'yours will be added and run automatically.</div>') \
        if queue_depth > 0 else ""

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>WattLab — RAG Energy Test</title>
    <style>
        * {{ box-sizing:border-box; margin:0; padding:0; }}
        body {{ font-family:monospace; background:#0a0a0a; color:#e0e0e0;
               max-width:780px; margin:0 auto; padding:2rem; }}
        h1 {{ color:#00ff99; margin-bottom:0.25rem; font-size:1.6rem; }}
        .subtitle {{ color:#555; font-size:0.8rem; margin-bottom:1.5rem; }}
        .info {{ color:#777; font-size:0.82rem; margin-bottom:1.5rem;
                 border-left:2px solid #222; padding-left:1rem; line-height:1.6; }}
        .presets {{ display:flex; gap:0.75rem; margin-bottom:1.5rem; }}
        .preset {{ border:1px solid #333; padding:1rem; cursor:pointer; flex:1; }}
        .preset:hover {{ border-color:#00ff9966; }}
        .preset.selected {{ border-color:#00ff99; background:#00ff9911; }}
        .preset h3 {{ color:#00ff99; font-size:0.9rem; margin-bottom:0.4rem; }}
        .section-label {{ color:#555; font-size:0.75rem; text-transform:uppercase;
                          letter-spacing:0.05em; margin-bottom:0.75rem; }}
        button {{ background:#00ff99; color:#000; border:none; padding:0.75rem 2rem;
                  cursor:pointer; font-family:monospace; font-size:1rem; margin-top:1rem; }}
        button:disabled {{ background:#222; color:#555; cursor:not-allowed; }}
        button:hover:not(:disabled) {{ background:#00dd88; }}
        #status {{ margin-top:1.5rem; }}
        .result-box {{ border:1px solid #222; padding:1.5rem; }}
        .result-box h2 {{ color:#00ff99; font-size:1.1rem; margin-bottom:1rem;
                          padding-bottom:0.5rem; border-bottom:1px solid #222; }}
        .metric {{ display:flex; justify-content:space-between;
                   padding:0.3rem 0; border-bottom:1px solid #111; font-size:0.82rem; }}
        .val {{ color:#00ff99; }}
        .section-title {{ color:#444; font-size:0.72rem; text-transform:uppercase;
                          letter-spacing:0.05em; margin:0.75rem 0 0.4rem; }}
        .response-box {{ background:#111; padding:1rem; margin-top:0.75rem;
                         font-size:0.8rem; color:#aaa; line-height:1.6;
                         border-left:2px solid #00ff9944; max-height:500px;
                         overflow-y:auto; white-space:pre-wrap; }}
        .scope-note {{ color:#333; font-size:0.72rem; margin-top:1rem; }}
        .progress-box {{ border:1px solid #222; padding:1.5rem; }}
        .progress-header {{ color:#ffaa00; font-size:0.9rem; margin-bottom:1rem; }}
        textarea {{ background:#111; border:1px solid #333; color:#e0e0e0;
                    font-family:monospace; font-size:0.88rem; padding:0.75rem;
                    width:100%; resize:vertical; line-height:1.5; }}
        textarea:focus {{ border-color:#00ff9966; outline:none; }}
        .mode-card {{ border:1px solid #333; padding:0.75rem 1rem; cursor:pointer;
                      flex:1; transition:border-color 0.15s; }}
        .mode-card:hover {{ border-color:#00ff9966; }}
        .mode-card.selected {{ border-color:#00ff99; background:#00ff9911; }}
        .mode-card h4 {{ color:#00ff99; font-size:0.85rem; margin-bottom:0.2rem; }}
        .mode-card p {{ color:#555; font-size:0.75rem; }}
        .index-bar {{ border:1px solid #1a1a1a; padding:0.75rem 1rem;
                      font-size:0.8rem; color:#555; margin-bottom:1.5rem;
                      display:flex; align-items:center; justify-content:space-between; gap:1rem; }}
        .index-dot {{ width:8px; height:8px; border-radius:50%; flex-shrink:0; }}
    </style>
</head>
<body>
    {_BACK}
    {busy_banner}
    <h1>RAG Energy Test</h1>
    <div class="subtitle">Greening of Streaming · WattLab · GoS1</div>
    <div class="info">
        Retrieval-Augmented Generation (RAG) augments an LLM with chunks from a PDF corpus.<br>
        Compare baseline (no retrieval), RAG (top 3 chunks), and RAG-large (top 8 chunks).<br>
        Scope: device layer only — no network, no amortised training cost.
    </div>

    <div class="index-bar">
        <div style="display:flex;align-items:center;gap:0.6rem">
            <div class="index-dot" id="index-dot" style="background:#333"></div>
            <span id="index-status-text">Checking index…</span>
        </div>
        <div style="display:flex;gap:0.5rem">
            <button id="buildBtn" onclick="buildIndex(false)"
                    style="background:none;border:1px solid #333;color:#555;
                           font-size:0.75rem;padding:0.3rem 0.75rem;cursor:pointer;
                           font-family:monospace;margin-top:0">Build index</button>
            <button id="rebuildBtn" onclick="buildIndex(true)"
                    style="background:none;border:1px solid #333;color:#555;
                           font-size:0.75rem;padding:0.3rem 0.75rem;cursor:pointer;
                           font-family:monospace;margin-top:0">Rebuild</button>
        </div>
    </div>

    <div class="section-label">Model</div>
    <div class="presets">{models_html}</div>

    <div class="section-label">Retrieval mode</div>
    <div class="presets" style="margin-bottom:1.5rem">
        <div class="mode-card selected" id="rmode-baseline" onclick="selectRMode('baseline')">
            <h4>Baseline</h4>
            <p>No retrieval. Cold LLM inference only.</p>
        </div>
        <div class="mode-card" id="rmode-rag" onclick="selectRMode('rag')">
            <h4>RAG</h4>
            <p>Top 3 chunks · 4096 ctx</p>
        </div>
        <div class="mode-card" id="rmode-rag_large" onclick="selectRMode('rag_large')">
            <h4>RAG Large</h4>
            <p>Top 8 chunks · 8192 ctx</p>
        </div>
    </div>

    <div class="section-label">Question</div>
    <textarea id="questionText" rows="3"
              placeholder="e.g. What is the energy cost of video streaming per GB transferred?"
              style="margin-bottom:1.5rem"></textarea>

    <div style="display:flex;gap:0.75rem;flex-wrap:wrap">
        <button id="runBtn" onclick="startRag()">▶ Run single</button>
        <button id="compareBtn" onclick="startCompare()"
                style="background:#111;border:1px solid #00ff99;color:#00ff99">
            ▶▶ Compare 3 modes
        </button>
    </div>

    <div id="status"></div>

    <div id="prev-runs" style="margin-top:2.5rem"></div>

    <script>
    let selectedRModel = 'tinyllama';
    let selectedRMode = 'baseline';
    let ragTimer = null;
    let ragStartTime = null;
    let compareTimer = null;

    function selectRModel(k) {{
        document.querySelectorAll('.presets .preset').forEach(el => el.classList.remove('selected'));
        const el = document.getElementById('rmodel-' + k);
        if (el) el.classList.add('selected');
        selectedRModel = k;
    }}
    function selectRMode(m) {{
        document.querySelectorAll('.mode-card').forEach(el => el.classList.remove('selected'));
        const el = document.getElementById('rmode-' + m);
        if (el) el.classList.add('selected');
        selectedRMode = m;
    }}
    selectRModel('tinyllama');

    function toggleAns(id) {{
        var el = document.getElementById(id);
        if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
    }}

    // Index status
    async function loadIndexStatus() {{
        try {{
            const r = await fetch('/rag/index-status');
            const d = await r.json();
            const dot = document.getElementById('index-dot');
            const txt = document.getElementById('index-status-text');
            if (d.status === 'ready') {{
                dot.style.background = '#00ff99';
                txt.textContent = 'Index ready · ' + d.doc_count + ' chunks';
            }} else if (d.status === 'building') {{
                dot.style.background = '#ffaa00';
                txt.textContent = 'Building index…';
                setTimeout(loadIndexStatus, 3000);
            }} else if (d.status === 'error') {{
                dot.style.background = '#ff4400';
                txt.textContent = 'Index error: ' + (d.error || 'unknown');
            }} else {{
                dot.style.background = '#555';
                txt.textContent = 'Index not built — click "Build index" to start';
            }}
        }} catch(e) {{
            document.getElementById('index-status-text').textContent = 'Could not check index';
        }}
    }}

    async function buildIndex(rebuild) {{
        const btn = rebuild ? document.getElementById('rebuildBtn') : document.getElementById('buildBtn');
        btn.disabled = true;
        btn.textContent = 'Working…';
        document.getElementById('index-dot').style.background = '#ffaa00';
        document.getElementById('index-status-text').textContent = 'Building index…';
        try {{
            await fetch('/rag/build-index', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{rebuild: rebuild}})
            }});
        }} catch(e) {{}}
        btn.disabled = false;
        btn.textContent = rebuild ? 'Rebuild' : 'Build index';
        setTimeout(loadIndexStatus, 2000);
    }}

    async function startRag() {{
        const question = document.getElementById('questionText').value.trim();
        if (!question) {{
            document.getElementById('status').innerHTML =
                '<div style="color:#ff4400;font-size:0.85rem;margin-top:1rem">Please enter a question.</div>';
            return;
        }}
        document.getElementById('runBtn').disabled = true;
        ragStartTime = Date.now();
        const form = new FormData();
        form.append('model_key', selectedRModel);
        form.append('rag_mode', selectedRMode);
        form.append('question', question);
        try {{
            const resp = await fetch('/rag/run', {{method:'POST', body:form}});
            const data = await resp.json();
            if (data.job_id) {{
                renderRagProgress('baseline');
                pollRag(data.job_id);
            }} else {{
                document.getElementById('status').innerHTML =
                    '<div style="color:#ff4400">Error: ' + JSON.stringify(data) + '</div>';
                document.getElementById('runBtn').disabled = false;
            }}
        }} catch(e) {{
            document.getElementById('status').innerHTML =
                '<div style="color:#ff4400">Failed: ' + e + '</div>';
            document.getElementById('runBtn').disabled = false;
        }}
    }}

    const RAG_STAGES = ['Baseline poll (10s)', 'Inference running', 'Complete'];
    const RAG_STAGE_IDX = {{baseline:0, inference:1, done:2}};

    function renderRagProgress(stage, watts) {{
        wlRenderProgress({{
            header: 'Measuring RAG energy \u2014 do not close this tab',
            stagesHtml: wlStageList(RAG_STAGES, RAG_STAGE_IDX[stage] ?? 0),
            watts: watts,
            elapsed: ragStartTime ? Date.now() - ragStartTime : null,
        }});
    }}

    async function pollRag(jobId) {{
        try {{
            const [resp, powerR] = await Promise.all([
                fetch('/rag/job/' + jobId),
                fetch('/power').catch(() => null),
            ]);
            const data = await resp.json();
            const watts = powerR ? (await powerR.json().catch(()=>({{}}))).watts ?? null : null;
            if (data.stage === 'done' && data.result) {{
                if (ragTimer) {{ clearTimeout(ragTimer); ragTimer = null; }}
                renderRagResult(data.result, jobId);
                document.getElementById('runBtn').disabled = false;
                loadPrevRuns();
            }} else if (data.stage === 'error' || data.error) {{
                if (ragTimer) {{ clearTimeout(ragTimer); ragTimer = null; }}
                document.getElementById('status').innerHTML =
                    '<div style="color:#ff4400">Error: ' + (data.error||'unknown') + '</div>';
                document.getElementById('runBtn').disabled = false;
            }} else if (data.stage === 'queued') {{
                wlRenderQueued(data.queue_position);
                ragTimer = setTimeout(() => pollRag(jobId), 3000);
            }} else {{
                renderRagProgress(data.stage || 'baseline', watts);
                ragTimer = setTimeout(() => pollRag(jobId), 2000);
            }}
        }} catch(e) {{
            ragTimer = setTimeout(() => pollRag(jobId), 5000);
        }}
    }}

    function renderRagResult(r, jobId) {{
        const e = r.energy || {{}};
        const inf = r.inference || {{}};
        const conf = e.confidence || {{}};
        const ragModeLabels = {{baseline:'Baseline (no retrieval)', rag:'RAG (top 3)', rag_large:'RAG Large (top 8)'}};
        const sourcesHtml = r.chunk_sources && r.chunk_sources.length
            ? r.chunk_sources.map(s => `<span style="font-size:0.72rem;color:#555;
                background:#111;padding:0.2rem 0.4rem;margin-right:0.3rem">${{s}}</span>`).join('')
            : '<span style="color:#333;font-size:0.75rem">none</span>';
        const retrievalHtml = r.rag_mode !== 'baseline' ? `
            <div class="section-title">Retrieval</div>
            <div class="metric"><span>Chunks retrieved</span><span class="val">${{r.chunks_retrieved}} / ${{r.top_k}}</span></div>
            <div class="metric"><span>Embedding</span><span class="val">${{r.embedding_ms}} ms</span></div>
            <div class="metric"><span>Vector search</span><span class="val">${{r.retrieval_ms}} ms</span></div>
            <div class="metric"><span>Context window</span><span class="val">${{r.num_ctx}} tokens</span></div>
            <div class="section-title" style="margin-top:0.75rem">Sources</div>
            <div style="margin-bottom:0.5rem">${{sourcesHtml}}</div>
        ` : '';
        document.getElementById('status').innerHTML = `
            <div class="result-box">
                <h2>Result — ${{r.model_label}} · ${{ragModeLabels[r.rag_mode] || r.rag_mode}}</h2>
                <div class="section-title">Question</div>
                <div style="color:#aaa;font-size:0.82rem;margin-bottom:0.75rem">${{r.question}}</div>
                ${{retrievalHtml}}
                <div class="section-title">Inference</div>
                <div class="metric"><span>Output tokens</span><span class="val">${{inf.output_tokens}}</span></div>
                <div class="metric"><span>Tokens/sec</span><span class="val">${{inf.tokens_per_sec}}</span></div>
                <div class="metric"><span>Duration</span><span class="val">${{inf.duration_s}} s</span></div>
                <div class="section-title">Energy</div>
                <div class="metric"><span>Baseline</span><span class="val">${{e.w_base}} W</span></div>
                <div class="metric"><span>Task mean</span><span class="val">${{e.w_task}} W</span></div>
                <div class="metric"><span>ΔW</span><span class="val">${{e.delta_w}} W</span></div>
                <div class="metric"><span>ΔE</span><span class="val">${{e.delta_e_wh}} Wh</span></div>
                <div class="metric"><span>mWh/token</span><span class="val">${{e.mwh_per_token ?? '—'}}</span></div>
                <div class="metric"><span>Confidence</span>
                    <span class="val conf-badge">${{conf.flag||'—'}} ${{conf.label||''}}</span></div>
                <div class="section-title">Answer</div>
                <div class="response-box">${{inf.response}}</div>
                <div class="scope-note">${{r.scope}}</div>
                <div style="display:flex;gap:0.5rem;margin-top:0.75rem">
                    <a href="/results/llm/${{jobId}}/download.json" download
                       style="color:#555;font-size:0.75rem;text-decoration:none">↓ JSON</a>
                    <a href="/results/llm/${{jobId}}/download.csv" download
                       style="color:#555;font-size:0.75rem;text-decoration:none">↓ CSV</a>
                </div>
            </div>`;
    }}

    // --- Compare 3 modes ---

    async function startCompare() {{
        const question = document.getElementById('questionText').value.trim();
        if (!question) {{
            document.getElementById('status').innerHTML =
                '<div style="color:#ff4400;font-size:0.85rem;margin-top:1rem">Please enter a question.</div>';
            return;
        }}
        document.getElementById('runBtn').disabled = true;
        document.getElementById('compareBtn').disabled = true;
        ragStartTime = Date.now();
        const form = new FormData();
        form.append('model_key', selectedRModel);
        form.append('question', question);
        try {{
            const resp = await fetch('/rag/run-compare', {{method:'POST', body:form}});
            const data = await resp.json();
            if (data.job_id) {{
                renderCompareProgress({{}}, null, null);
                pollCompare(data.job_id);
            }} else {{
                document.getElementById('status').innerHTML =
                    '<div style="color:#ff4400">Error: ' + JSON.stringify(data) + '</div>';
                document.getElementById('runBtn').disabled = false;
                document.getElementById('compareBtn').disabled = false;
            }}
        }} catch(e) {{
            document.getElementById('status').innerHTML =
                '<div style="color:#ff4400">Failed: ' + e + '</div>';
            document.getElementById('runBtn').disabled = false;
            document.getElementById('compareBtn').disabled = false;
        }}
    }}

    function renderCompareProgress(partial, currentMode, watts) {{
        const MODES = ['baseline','rag','rag_large'];
        const MODE_LABELS = {{baseline:'Baseline', rag:'RAG', rag_large:'RAG Large'}};
        const stagesHtml = MODES.map(m => {{
            const done = partial && partial[m];
            const active = m === currentMode && !done;
            const col = done ? '#00ff99' : active ? '#ffaa00' : '#333';
            const icon = done ? '✓' : active ? '▶' : '·';
            let extra = '';
            if (done) {{
                const e = partial[m].energy || {{}};
                extra = ' <span style="color:#555;font-size:0.75rem">\u2014 '
                    + (e.delta_w != null ? e.delta_w + ' W \xb7 ' : '')
                    + (e.mwh_per_token != null ? e.mwh_per_token + ' mWh/tok' : '')
                    + (e.confidence ? ' ' + e.confidence.flag : '')
                    + '</span>';
            }}
            return '<div style="display:flex;align-items:center;gap:0.6rem;font-size:0.82rem;margin-bottom:0.3rem">'
                + '<span style="color:' + col + ';width:1rem">' + icon + '</span>'
                + '<span style="color:' + col + '">' + MODE_LABELS[m] + extra + '</span></div>';
        }}).join('');
        wlRenderProgress({{
            header: 'Comparing 3 modes \u2014 do not close this tab',
            stagesHtml: stagesHtml,
            watts: watts,
            elapsed: ragStartTime ? Date.now() - ragStartTime : null,
        }});
    }}

    async function pollCompare(jobId) {{
        try {{
            const [resp, powerR] = await Promise.all([
                fetch('/rag/job/' + jobId),
                fetch('/power').catch(() => null),
            ]);
            const data = await resp.json();
            const watts = powerR ? (await powerR.json().catch(()=>({{}}))).watts ?? null : null;
            if (data.stage === 'done' && data.result) {{
                if (compareTimer) {{ clearTimeout(compareTimer); compareTimer = null; }}
                renderCompareResult(data.result, jobId);
                document.getElementById('runBtn').disabled = false;
                document.getElementById('compareBtn').disabled = false;
                loadPrevRuns();
            }} else if (data.stage === 'error' || data.error) {{
                if (compareTimer) {{ clearTimeout(compareTimer); compareTimer = null; }}
                document.getElementById('status').innerHTML =
                    '<div style="color:#ff4400">Error: ' + (data.error||'unknown') + '</div>';
                document.getElementById('runBtn').disabled = false;
                document.getElementById('compareBtn').disabled = false;
            }} else if (data.stage === 'queued') {{
                wlRenderQueued(data.queue_position);
                compareTimer = setTimeout(() => pollCompare(jobId), 3000);
            }} else {{
                renderCompareProgress(data.partial_results || {{}}, data.current_mode || data.stage, watts);
                compareTimer = setTimeout(() => pollCompare(jobId), 2000);
            }}
        }} catch(e) {{
            compareTimer = setTimeout(() => pollCompare(jobId), 5000);
        }}
    }}

    function renderCompareResult(r, jobId) {{
        const MODES = ['baseline','rag','rag_large'];
        const MODE_LABELS = {{baseline:'Baseline (no retrieval)', rag:'RAG \u2014 top 3 chunks', rag_large:'RAG Large \u2014 top 8 chunks'}};
        const STRIPE = {{baseline:'#444', rag:'#0088cc', rag_large:'#00ff99'}};
        const cards = MODES.map(m => {{
            const res = (r.results || {{}})[m];
            if (!res) return '';
            const e = res.energy || {{}};
            const inf = res.inference || {{}};
            const conf = e.confidence || {{}};
            const retrievalRow = m !== 'baseline'
                ? '<div style="color:#555;font-size:0.78rem;margin:0.4rem 0">'
                  + 'embed ' + res.embedding_ms + 'ms \xb7 search ' + res.retrieval_ms + 'ms \xb7 '
                  + res.chunks_retrieved + ' chunks</div>'
                : '<div style="color:#333;font-size:0.78rem;margin:0.4rem 0">No retrieval</div>';
            const answerId = 'ans-' + m + '-' + jobId;
            return '<div style="border:1px solid #222;border-left:3px solid ' + STRIPE[m] + ';padding:1.25rem;margin-bottom:0.75rem">'
                + '<div style="font-size:0.9rem;color:#e0e0e0;margin-bottom:0.5rem">' + MODE_LABELS[m] + '</div>'
                + retrievalRow
                + '<div style="display:flex;gap:1.5rem;font-size:0.82rem;flex-wrap:wrap;margin-bottom:0.5rem">'
                + '<span>\u0394W <span style="color:#00ff99">' + e.delta_w + ' W</span></span>'
                + '<span>\u0394E <span style="color:#00ff99">' + e.delta_e_wh + ' Wh</span></span>'
                + '<span>mWh/tok <span style="color:#00ff99">' + (e.mwh_per_token ?? '\u2014') + '</span></span>'
                + '<span>' + inf.tokens_per_sec + ' tok/s</span>'
                + '<span class="conf-badge">' + (conf.flag||'') + ' ' + (conf.label||'') + '</span>'
                + '</div>'
                + '<div style="font-size:0.75rem;color:#555;margin-bottom:0.4rem;cursor:pointer" '
                + 'data-id="' + answerId + '" onclick="toggleAns(this.dataset.id)">'
                + '\u25b6 Show / hide answer</div>'
                + '<div id="' + answerId + '" style="display:none;background:#111;padding:0.75rem;'
                + 'font-size:0.78rem;color:#aaa;line-height:1.6;white-space:pre-wrap;max-height:300px;overflow-y:auto;'
                + 'border-left:2px solid ' + STRIPE[m] + '44">' + (inf.response || '') + '</div>'
                + '</div>';
        }}).join('');
        document.getElementById('status').innerHTML =
            '<div style="border:1px solid #222;padding:1.5rem">'
            + '<div style="color:#00ff99;font-size:1.1rem;margin-bottom:0.25rem">Comparison \u2014 ' + r.model_label + '</div>'
            + '<div style="color:#555;font-size:0.82rem;margin-bottom:1rem">' + r.question + '</div>'
            + cards
            + '<div style="color:#333;font-size:0.72rem;margin-top:0.75rem">' + (r.scope||'') + '</div>'
            + '<div style="display:flex;gap:0.5rem;margin-top:0.75rem">'
            + '<a href="/results/llm/' + jobId + '/download.json" download style="color:#555;font-size:0.75rem;text-decoration:none">\u2193 JSON</a>'
            + '</div></div>';
    }}

    // --- Previous runs ---

    async function loadPrevRuns() {{
        try {{
            const resp = await fetch('/results/llm/list');
            const runs = await resp.json();
            const ragRuns = runs.filter(r => r.task && (r.task.startsWith('RAG/') || r.task === 'RAG compare (3 modes)'));
            renderPrevRuns(ragRuns);
        }} catch(e) {{}}
    }}

    function renderPrevRuns(runs) {{
        const el = document.getElementById('prev-runs');
        if (!runs || runs.length === 0) {{
            el.innerHTML = '<div style="color:#333;font-size:0.8rem">No previous RAG runs.</div>';
            return;
        }}
        const rows = runs.map(r => {{
            const date = r.saved_at ? r.saved_at.slice(0,16).replace('T',' ') : '\u2014';
            const summary = (r.model||'') + ' \xb7 ' + (r.task||'') + ' \xb7 ' + r.mwh_per_token + ' mWh/tok ' + (r.confidence||'');
            const base = '/results/llm/' + r.job_id;
            return '<div style="border-bottom:1px solid #111;padding:0.6rem 0">'
                + '<div style="display:flex;justify-content:space-between;align-items:baseline">'
                + '<span style="color:#e0e0e0;font-size:0.82rem">' + date + '</span>'
                + '<span style="color:#555;font-size:0.75rem;font-family:monospace">' + r.job_id + '</span></div>'
                + '<div style="color:#00ff99;font-size:0.8rem;margin:0.2rem 0">' + summary + '</div>'
                + '<div style="display:flex;gap:0.5rem;margin-top:0.3rem">'
                + '<a href="' + base + '/download.json" download style="color:#555;font-size:0.75rem;text-decoration:none">\u2193 JSON</a>'
                + '<a href="' + base + '/download.csv" download style="color:#555;font-size:0.75rem;text-decoration:none">\u2193 CSV</a>'
                + '</div></div>';
        }}).join('');
        el.innerHTML = '<div style="color:#444;font-size:0.72rem;text-transform:uppercase;'
            + 'letter-spacing:0.05em;margin-bottom:0.75rem">Previous RAG runs</div>' + rows;
    }}

    loadIndexStatus();
    loadPrevRuns();
    const _resumeJob = new URLSearchParams(location.search).get('job');
    if (_resumeJob) {{ pollRag(_resumeJob); }}
    </script>
    {_PROGRESS_JS}
    {_CONF_HELP_WIDGET}
    {_FOOTER}
</body>
</html>"""


@app.get("/rag/index-status")
async def rag_index_status():
    return {
        "status": rag_module.index_status,
        "doc_count": rag_module.index_doc_count,
        "error": rag_module.index_error,
    }


@app.post("/rag/build-index")
async def rag_build_index(request: Request):
    body = await request.json()
    rebuild = bool(body.get("rebuild", False))
    if rag_module.index_status == "building":
        return {"status": "already_building"}
    loop = asyncio.get_event_loop()
    asyncio.create_task(loop.run_in_executor(None, lambda: rag_module.build_index(rebuild)))
    return {"status": "started"}


@app.post("/rag/run")
async def rag_run(
    model_key: str = Form(...),
    rag_mode: str = Form(...),
    question: str = Form(...),
):
    if model_key not in rag_module.MODELS:
        return JSONResponse({"error": "Invalid model"}, status_code=400)
    if rag_mode not in ("baseline", "rag", "rag_large"):
        return JSONResponse({"error": "Invalid rag_mode"}, status_code=400)
    if not question.strip():
        return JSONResponse({"error": "Question required"}, status_code=400)
    if rag_mode != "baseline" and rag_module.index_status != "ready":
        return JSONResponse({"error": "Index not ready — build it first"}, status_code=400)

    job_id = str(uuid.uuid4())[:8]
    mode_labels = {"baseline": "Baseline", "rag": "RAG", "rag_large": "RAG Large"}
    label = f"RAG — {rag_module.MODELS[model_key]['label']} · {mode_labels[rag_mode]}"

    async def coro():
        jobs[job_id]["stage"] = "baseline"
        result = await rag_module.run_rag_measurement(model_key, rag_mode, question.strip(), jobs, job_id)
        save_result("llm", job_id, result)
        jobs[job_id] = {"stage": "done", "result": result}

    position = enqueue(job_id, "rag", label, coro)
    if position is None:
        return JSONResponse({"error": "Queue full — try again later."}, status_code=429)
    return {"job_id": job_id, "queue_position": position}


@app.get("/rag/job/{job_id}")
async def rag_job_status(job_id: str):
    return jobs.get(job_id, {"status": "not_found"})


async def run_rag_compare_job(job_id: str, model_key: str, question: str):
    partial_results = {}
    try:
        for rag_mode in ("baseline", "rag", "rag_large"):
            jobs[job_id]["current_mode"] = rag_mode
            jobs[job_id]["stage"] = rag_mode
            jobs[job_id]["partial_results"] = dict(partial_results)
            result = await rag_module.run_rag_measurement(
                model_key, rag_mode, question, jobs, job_id)
            partial_results[rag_mode] = result
            jobs[job_id]["partial_results"] = dict(partial_results)

        final = {
            "mode": "rag_compare",
            "model_key": model_key,
            "model_label": rag_module.MODELS[model_key]["label"],
            "model_params": rag_module.MODELS[model_key]["params"],
            "question": question,
            "results": partial_results,
            "scope": "Device layer only (GoS1). Network and CPE excluded. No amortised training cost.",
        }
        save_result("llm", job_id, final)
        jobs[job_id] = {"stage": "done", "result": final}
    except Exception as e:
        jobs[job_id] = {"stage": "error", "error": str(e)}


@app.post("/rag/run-compare")
async def rag_run_compare(
    model_key: str = Form(...),
    question: str = Form(...),
):
    if model_key not in rag_module.MODELS:
        return JSONResponse({"error": "Invalid model"}, status_code=400)
    if not question.strip():
        return JSONResponse({"error": "Question required"}, status_code=400)
    if rag_module.index_status != "ready":
        return JSONResponse({"error": "Index not ready — build it first"}, status_code=400)

    job_id = str(uuid.uuid4())[:8]
    label = f"RAG Compare — {rag_module.MODELS[model_key]['label']} · 3 modes"

    async def coro():
        await run_rag_compare_job(job_id, model_key, question.strip())

    position = enqueue(job_id, "rag", label, coro)
    if position is None:
        return JSONResponse({"error": "Queue full — try again later."}, status_code=429)
    return {"job_id": job_id, "queue_position": position}


# --- Results: list, JSON download, CSV download ---

@app.get("/results/{job_type}/list")
async def results_list(job_type: str):
    if job_type not in ("video", "llm", "image"):
        return JSONResponse({"error": "Invalid type"}, status_code=400)
    return list_results(job_type)

@app.get("/results/{job_type}/{job_id}/download.json")
async def results_download_json(job_type: str, job_id: str):
    if job_type not in ("video", "llm", "image"):
        return JSONResponse({"error": "Invalid type"}, status_code=400)
    data = load_result(job_type, job_id)
    if not data:
        return JSONResponse({"error": "Not found"}, status_code=404)
    content = json.dumps(data, indent=2)
    return StreamingResponse(
        io.BytesIO(content.encode()),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=wattlab_{job_type}_{job_id}.json"},
    )

@app.get("/results/{job_type}/{job_id}/download.csv")
async def results_download_csv(job_type: str, job_id: str):
    if job_type not in ("video", "llm", "image"):
        return JSONResponse({"error": "Invalid type"}, status_code=400)
    data = load_result(job_type, job_id)
    if not data:
        return JSONResponse({"error": "Not found"}, status_code=404)
    content = to_csv(job_type, data)
    return StreamingResponse(
        io.BytesIO(content.encode()),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=wattlab_{job_type}_{job_id}.csv"},
    )


# --- Settings ---

def _is_local(request: Request) -> bool:
    """True if the request originates from a loopback or private IP.
    Uses X-Real-IP (set by nginx) when present, otherwise the direct client IP.
    This blocks both domain-based and raw IP-based public access."""
    ip_str = request.headers.get("x-real-ip") or (request.client.host if request.client else "")
    try:
        addr = ipaddress.ip_address(ip_str)
        return addr.is_loopback or addr.is_private
    except ValueError:
        return False


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    s = cfg.load()
    local = _is_local(request)

    def field(fid, val, min_, max_, unit, hint="", step=None):
        step_attr = f' step="{step}"' if step else ""
        if local:
            ctrl = (f'<input type="number" id="{fid}" min="{min_}" max="{max_}"{step_attr}'
                    f' value="{val}" style="background:#111;border:1px solid #333;color:#e0e0e0;'
                    f'font-family:monospace;font-size:0.9rem;padding:0.3rem 0.5rem;'
                    f'width:80px;text-align:right">')
        else:
            ctrl = f'<span style="font-family:monospace;color:#00ff99;font-size:0.95rem">{val}</span>'
        hint_html = f'<div style="color:#333;font-size:0.72rem;margin-top:0.2rem">{hint}</div>' if hint else ""
        return (f'<div style="display:flex;justify-content:space-between;align-items:baseline;'
                f'padding:0.5rem 0;border-bottom:1px solid #0d0d0d;gap:1rem">'
                f'<div><label style="color:#aaa;font-size:0.85rem">{fid.replace("_"," ").title()}</label>'
                f'{hint_html}</div>'
                f'<div style="display:flex;align-items:baseline;gap:0.5rem">'
                f'{ctrl}<span style="color:#555;font-size:0.8rem">{unit}</span>'
                f'</div></div>')

    notice = ('' if local else
              '<div style="background:#111;border-left:3px solid #555;padding:0.75rem 1rem;'
              'margin-bottom:1.5rem;font-size:0.82rem;color:#555">'
              '🔒 Read-only — settings can only be modified from the lab network or SSH tunnel.'
              '</div>')
    save_block = ('<button onclick="saveSettings()" style="background:#00ff99;color:#000;border:none;'
                  'padding:0.75rem 2rem;cursor:pointer;font-family:monospace;font-size:1rem;margin-top:2rem">'
                  'Save Settings</button><div id="msg" style="margin-top:1rem;font-size:0.85rem"></div>'
                  if local else '')
    subtitle = 'WattLab · GoS1 · Lab mode' if local else 'WattLab · GoS1 · Read-only'

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>WattLab — Settings</title>
    <style>
        * {{ box-sizing:border-box; margin:0; padding:0; }}
        body {{ font-family:monospace; background:#0a0a0a; color:#e0e0e0;
               max-width:600px; margin:0 auto; padding:2rem; }}
        h1 {{ color:#00ff99; margin-bottom:0.25rem; font-size:1.6rem; }}
        .subtitle {{ color:#555; font-size:0.8rem; margin-bottom:2rem; }}
        .section {{ color:#444; font-size:0.72rem; text-transform:uppercase;
                    letter-spacing:0.05em; margin:1.5rem 0 0.75rem;
                    padding-bottom:0.4rem; border-bottom:1px solid #111; }}
        input[type=number]:focus {{ border-color:#00ff99; outline:none; }}
    </style>
</head>
<body>
    {_BACK}
    <h1>Settings</h1>
    <div class="subtitle">{subtitle}</div>
    {notice}

    <div class="section">Measurement</div>
    {field("baseline_polls",    s['baseline_polls'],    5,  60,  "× 1s",   "baseline window duration")}
    {field("video_cooldown_s",  s['video_cooldown_s'],  10, 300, "s",      "rest between CPU and GPU runs")}
    {field("llm_rest_s",        s['llm_rest_s'],        5,  120, "s",      "pause between runs in batch mode")}
    {field("llm_unload_settle_s", s['llm_unload_settle_s'], 1, 30, "s",   "wait after model unload before baseline")}

    <div class="section">Confidence thresholds</div>
    {field("conf_green_delta_w",  s['conf_green_delta_w'],  0, 50,  "W",     "🟢 green ΔW threshold", step=0.5)}
    {field("conf_green_polls",    s['conf_green_polls'],    1, 100, "polls", "🟢 green minimum polls")}
    {field("conf_yellow_delta_w", s['conf_yellow_delta_w'], 0, 20,  "W",     "🟡 yellow ΔW threshold", step=0.5)}
    {field("conf_yellow_polls",   s['conf_yellow_polls'],   1, 50,  "polls", "🟡 yellow minimum polls")}

    {save_block}
    <script>
    async function saveSettings() {{
        const fields = ['baseline_polls','video_cooldown_s','llm_rest_s','llm_unload_settle_s',
                        'conf_green_delta_w','conf_green_polls','conf_yellow_delta_w','conf_yellow_polls'];
        const body = {{}};
        for (const f of fields) body[f] = parseFloat(document.getElementById(f).value);
        try {{
            const resp = await fetch('/settings', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify(body),
            }});
            const data = await resp.json();
            if (data.ok) {{
                document.getElementById('msg').innerHTML =
                    '<span style="color:#00ff99">✓ Saved.</span>';
            }} else {{
                document.getElementById('msg').innerHTML =
                    '<span style="color:#ff4400">Error: ' + JSON.stringify(data) + '</span>';
            }}
        }} catch(e) {{
            document.getElementById('msg').innerHTML =
                '<span style="color:#ff4400">Failed: ' + e + '</span>';
        }}
    }}
    </script>
    {_FOOTER}
</body>
</html>"""


@app.post("/settings")
async def settings_save(request: Request, data: dict):
    if not _is_local(request):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    saved = cfg.save(data)
    return {"ok": True, "settings": saved}


# --- Demo mode ---

_DEMO_HTML = f"""<!DOCTYPE html>
<html>
<head>
<title>WattLab — Guided Tour · Greening of Streaming</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:system-ui,-apple-system,sans-serif;background:#0a0a0a;
       color:#e0e0e0;max-width:840px;margin:0 auto;padding:2rem}}
  h1{{font-family:monospace;color:#00ff99;font-size:1.5rem;margin-bottom:0.25rem}}
  h2{{font-family:monospace;color:#00ff99;font-size:1.1rem;margin-bottom:0.75rem}}
  .mono{{font-family:monospace}}
  .dim{{color:#555}}
  .accent{{color:#00ff99}}

  /* Step nav */
  .step-nav{{display:flex;align-items:center;gap:0.5rem;margin-bottom:2.5rem;
             font-family:monospace;font-size:0.78rem;color:#333}}
  .step-nav .dot{{width:8px;height:8px;border-radius:50%;background:#222;
                  transition:background 0.3s}}
  .step-nav .dot.done{{background:#00ff9966}}
  .step-nav .dot.active{{background:#00ff99}}
  .step-nav .label{{color:#555;font-size:0.72rem}}
  .step-nav .label.active{{color:#00ff99}}

  /* Steps */
  .step{{display:none}}
  .step.active{{display:block}}

  /* Logo header */
  .page-header{{display:flex;justify-content:space-between;align-items:flex-start;
                margin-bottom:2rem}}

  /* Big metric */
  .big-metric{{font-family:monospace;font-size:3.5rem;color:#00ff99;
               font-weight:bold;line-height:1;margin:1rem 0}}
  .big-label{{color:#555;font-size:0.85rem;margin-bottom:2rem}}

  /* Methodology expander */
  details{{margin:1rem 0;border-left:2px solid #222;padding-left:1rem}}
  summary{{color:#444;font-size:0.8rem;cursor:pointer;list-style:none;
           padding:0.4rem 0;user-select:none}}
  summary::-webkit-details-marker{{display:none}}
  summary::before{{content:"▶  ";font-size:0.65rem}}
  details[open] summary::before{{content:"▼  "}}
  details p{{color:#555;font-size:0.82rem;line-height:1.7;margin-top:0.5rem}}
  details p+p{{margin-top:0.5rem}}

  /* Action buttons */
  .btn-row{{display:flex;gap:0.75rem;flex-wrap:wrap;margin-top:1.5rem}}
  .btn{{font-family:monospace;font-size:0.9rem;padding:0.65rem 1.5rem;
        cursor:pointer;border:none;transition:background 0.15s}}
  .btn-primary{{background:#00ff99;color:#000}}
  .btn-primary:hover{{background:#00dd88}}
  .btn-secondary{{background:transparent;color:#00ff99;
                  border:1px solid #00ff9944}}
  .btn-secondary:hover{{background:#00ff9911}}
  .btn:disabled{{background:#1a1a1a;color:#333;cursor:not-allowed;border:none}}

  /* Result card */
  .result-card{{border:1px solid #1a1a1a;padding:1.5rem;margin-top:1.5rem}}
  .result-card .headline{{font-size:1rem;color:#e0e0e0;line-height:1.6;
                           margin-bottom:1rem}}
  .kpi-row{{display:flex;gap:1.5rem;flex-wrap:wrap;margin-bottom:1rem}}
  .kpi{{flex:1;min-width:120px}}
  .kpi .val{{font-family:monospace;font-size:1.4rem;color:#00ff99}}
  .kpi .lbl{{font-size:0.72rem;color:#444;margin-top:0.2rem}}
  .conf-badge{{display:inline-block;font-size:0.75rem;color:#555;
               margin-top:0.5rem}}
  .response-preview{{background:#0d0d0d;border-left:2px solid #00ff9933;
                     padding:0.75rem 1rem;margin-top:1rem;font-size:0.8rem;
                     color:#888;line-height:1.7;max-height:300px;
                     overflow-y:auto;white-space:pre-wrap;font-family:monospace}}
  .scope-note{{color:#2a2a2a;font-size:0.72rem;margin-top:1rem;font-family:monospace}}
  .prev-note{{color:#333;font-size:0.75rem;font-family:monospace;
              margin-top:0.5rem}}
  .divider{{border:none;border-top:1px solid #111;margin:1.5rem 0}}

  /* Three-band layout */
  .band{{margin-bottom:1.75rem;padding-bottom:1.75rem;border-bottom:1px solid #0d0d0d}}
  .band-label{{color:#333;font-size:0.68rem;text-transform:uppercase;letter-spacing:0.08em;
               font-family:monospace;margin-bottom:0.6rem}}
  .limitation{{color:#2a2a2a;font-size:0.75rem;margin-top:1rem;line-height:1.6;
               font-family:monospace;border-left:1px solid #1a1a1a;padding-left:0.75rem}}

  /* Progress */
  .progress-note{{color:#ffaa00;font-family:monospace;font-size:0.85rem;
                  margin-top:1rem}}
  .stream-box{{background:#0d0d0d;border-left:2px solid #00ff9922;
               padding:0.75rem 1rem;margin-top:0.75rem;font-size:0.78rem;
               color:#666;line-height:1.7;max-height:160px;overflow-y:auto;
               white-space:pre-wrap;font-family:monospace;min-height:2.5rem}}

  /* Summary table */
  .summary-table{{width:100%;border-collapse:collapse;font-family:monospace;
                  font-size:0.82rem;margin-top:1rem}}
  .summary-table td{{padding:0.5rem 0.75rem;border-bottom:1px solid #111}}
  .summary-table td:first-child{{color:#555;width:40%}}
  .summary-table td:last-child{{color:#00ff99}}
</style>
</head>
<body>
    {_BACK}
<div class="page-header">
  <div id="step-nav" class="step-nav">
    <span class="dot active" id="dot-0"></span>
    <span class="dot" id="dot-1"></span>
    <span class="dot" id="dot-2"></span>
    <span class="dot" id="dot-3"></span>
    <span class="dot" id="dot-4"></span>
    <span class="dot" id="dot-5"></span>
    <span class="label active" id="nav-label">Welcome</span>
  </div>
</div>

<!-- Step 0: Welcome -->
<div class="step active" id="step-0">
  <h1>WattLab</h1>
  <p style="color:#555;font-size:0.85rem;margin-bottom:1.5rem">
    Greening of Streaming · Live energy measurement · GoS1</p>

  <p style="color:#aaa;line-height:1.8;max-width:560px">
    WattLab measures the real energy cost of video transcoding and AI inference —
    using a calibrated smart plug, not estimates. Every number on this page
    comes from a live measurement on GoS1, a server in our lab in France.
  </p>

  <div class="big-metric" id="live-watts">— W</div>
  <div class="big-label">GoS1 current power draw · Tapo P110 · device layer only</div>

  <details>
    <summary>What's being measured?</summary>
    <p>GoS1 is an AMD Ryzen 9 workstation with an RX 7800 XT GPU.
    Power is sampled at 1-second intervals via a Tapo P110 smart plug
    connected to the mains supply. We measure the delta between idle
    baseline and task power — not estimated TDP or nameplate figures.</p>
    <p>Scope: device layer only. Network, CDN, and CPE are explicitly excluded.
    Amortised embodied carbon and training cost are not included in LLM measurements.</p>
  </details>

  <details>
    <summary>Why does this matter?</summary>
    <p>Streaming accounts for a significant and growing share of global internet
    traffic. Codec choice, inference model size, and hardware path all affect
    real energy use — but most published figures are estimates or averages.
    WattLab produces primary measurement data that operators and researchers
    can reproduce and cite.</p>
  </details>

  <div class="btn-row">
    <button class="btn btn-primary" onclick="goStep(1)">Start Tour →</button>
  </div>
</div>

<!-- Step 1: Video -->
<div class="step" id="step-1">
  <h1>Video Transcode</h1>

  <div class="band">
    <div class="band-label">What this shows</div>
    <p style="color:#aaa;line-height:1.8;max-width:560px">
      Whether transcoding to the same quality target uses more energy on CPU or GPU —
      and whether the faster path is also the more efficient one.
    </p>
  </div>

  <div class="band">
    <div class="band-label">What we're doing</div>
    <p style="color:#555;line-height:1.7;max-width:560px;margin-bottom:0.75rem">
      Encoding a 4K clip (Meridian, Netflix Open Content, CC BY 4.0) to 1080p H.264 —
      once in software (libx264) and once with hardware acceleration (h264_vaapi).
      Same source. Same quality target. P110 sampled every second throughout.
    </p>
    <details>
      <summary>How this is measured</summary>
      <p>10s idle baseline before each run. 60s thermal cooldown between CPU and GPU.
      Energy = ΔW × duration / 3600. Confidence 🟢 = ΔW &gt; 5W and ≥ 10 polls.</p>
      <p>Source: 812 MB, 4K. Encode time ~2–3 min CPU, ~90s GPU.
      Previous runs: CPU 174s / 4.06 Wh mean · GPU 114s / 4.42 Wh mean (4 runs).</p>
    </details>
  </div>

  <div>
    <div class="band-label">Result</div>
    <div id="video-action">
      <div class="btn-row" id="video-btns" style="display:none">
        <button class="btn btn-primary" id="btn-run-video" onclick="runDemoVideo()">
          Run new measurement (~5 min)</button>
      </div>
      <div id="video-status"></div>
    </div>
    <p class="limitation">Scope: device layer only (GoS1). Network, CDN, and CPE not included.
    A faster encode does not automatically mean less energy — this measures total Wh, not rate.</p>
  </div>

  <div id="next-1" style="display:none;margin-top:2rem;padding-top:1.5rem;border-top:1px solid #111">
    <div class="btn-row">
      <button class="btn btn-primary" onclick="goStep(2)">Next: LLM inference →</button>
      <button class="btn btn-secondary" onclick="resetVideoStep()">Run again</button>
    </div>
  </div>
</div>

<!-- Step 2: LLM -->
<div class="step" id="step-2">
  <h1>LLM Inference</h1>

  <div class="band">
    <div class="band-label">What this shows</div>
    <p style="color:#aaa;line-height:1.8;max-width:560px">
      How much energy each generated token costs — and how model size
      translates into energy use per unit of output.
    </p>
  </div>

  <div class="band">
    <div class="band-label">What we're doing</div>
    <p style="color:#555;line-height:1.7;max-width:560px;margin-bottom:0.75rem">
      Running a fixed prompt (T3 Long — network energy attribution briefing)
      through Mistral 7B cold: model unloaded before baseline so we capture
      the true first-request cost. GPU inference via Ollama ROCm.
    </p>
    <details>
      <summary>How this is measured</summary>
      <p>Model unloaded from VRAM. 3s settle. 10s idle baseline. Single inference run.
      P110 at 1s intervals. Primary metric: mWh per output token.</p>
      <p>Model: Mistral 7B (4.4 GB). Previous result: 0.94 mWh/tok, ~47 tok/s.</p>
    </details>
    <details>
      <summary>Why mWh per token?</summary>
      <p>Token count varies between models and prompts, so raw Wh figures aren't
      comparable. Energy per token lets us place TinyLlama (0.06 mWh/tok) and
      Mistral 7B (0.94 mWh/tok) on the same axis — a ~15× difference.</p>
    </details>
  </div>

  <div>
    <div class="band-label">Result</div>
    <div id="llm-action">
      <div class="btn-row" id="llm-btns" style="display:none">
        <button class="btn btn-primary" id="btn-run-llm" onclick="runDemoLLM()">
          Run new measurement (~3 min)</button>
      </div>
      <div id="llm-status"></div>
    </div>
    <p class="limitation">Scope: device layer only (GoS1). No amortised training cost included.
    mWh/token measures inference energy only — not the energy cost of training the model.</p>
  </div>

  <div id="next-2" style="display:none;margin-top:2rem;padding-top:1.5rem;border-top:1px solid #111">
    <div class="btn-row">
      <button class="btn btn-primary" onclick="goStep(3)">Next: Image generation →</button>
      <button class="btn btn-secondary" onclick="resetLLMStep()">Run again</button>
    </div>
  </div>
</div>

<!-- Step 3: Image generation -->
<div class="step" id="step-3">
  <h1>Image Generation</h1>

  <div class="band">
    <div class="band-label">What this shows</div>
    <p style="color:#aaa;line-height:1.8;max-width:560px">
      How much energy one AI-generated image costs — measured end to end on
      real hardware, not estimated from TDP or cloud benchmarks.
    </p>
  </div>

  <div class="band">
    <div class="band-label">What we're doing</div>
    <p style="color:#555;line-height:1.7;max-width:560px;margin-bottom:0.75rem">
      Running SD-Turbo (stabilityai/sd-turbo, CPU, 8 steps, 512×512) with a
      randomly modified prompt — the colour modifier changes each run to prove
      the image is generated live, not replayed from cache.
    </p>
    <details>
      <summary>How this is measured</summary>
      <p>10s idle baseline. CPU diffusion run. P110 at 1s intervals.
      Metric: Wh per image = ΔW × generation_time / 3600.</p>
      <p>Previous result: 0.21 Wh/image, 12s, ~30W delta above idle.</p>
    </details>
  </div>

  <div>
    <div class="band-label">Result</div>
    <div id="image-btns" class="btn-row" style="display:none">
      <button class="btn btn-primary" onclick="runDemoImage()">Generate &amp; measure</button>
    </div>
    <div id="image-status"></div>
    <p class="limitation">Scope: device layer only (GoS1). Network and storage excluded.
    This measures one image on one machine — not the energy cost of a hosted API call.</p>
  </div>

  <div id="next-3" style="display:none;margin-top:2rem;padding-top:1.5rem;border-top:1px solid #111">
    <div class="btn-row">
      <button class="btn btn-primary" onclick="goStep(4)">Next: How we flag confidence →</button>
      <button class="btn btn-secondary" onclick="resetImageStep()">Run again</button>
    </div>
  </div>
</div>

<!-- Step 4: Confidence -->
<div class="step" id="step-4">
  <h1>How We Flag Confidence</h1>

  <div class="band">
    <div class="band-label">The problem</div>
    <p style="color:#aaa;line-height:1.8;max-width:560px">
      Not every measurement we take is equally trustworthy.
      The Tapo P110 has a practical noise floor of around 1W at steady state.
      A task that adds 1.5W above baseline might be real signal — or it might
      be measurement noise from the plug itself.
    </p>
  </div>

  <div class="band">
    <div class="band-label">The system</div>
    <p style="color:#555;line-height:1.7;max-width:560px;margin-bottom:1rem">
      Every result carries a traffic light based on two measurements:
      the power delta above idle (ΔW) and the number of 1-second polls
      taken during the task.
    </p>
    <div style="display:flex;flex-direction:column;gap:0.75rem;max-width:480px">
      <div style="border-left:2px solid #1a3a1a;padding:0.6rem 1rem">
        <div style="font-family:monospace;font-size:0.9rem">🟢 Repeatable</div>
        <div style="color:#555;font-size:0.82rem;margin-top:0.25rem">
          ΔW > 5W and ≥ 10 polls. Well above noise floor. Reliable enough to cite.</div>
      </div>
      <div style="border-left:2px solid #3a3a00;padding:0.6rem 1rem">
        <div style="font-family:monospace;font-size:0.9rem">🟡 Early insight</div>
        <div style="color:#555;font-size:0.82rem;margin-top:0.25rem">
          ΔW ≥ 2W or ≥ 5 polls. Directional and consistent with expectation,
          but needs more runs before we'd stake a public claim on it.</div>
      </div>
      <div style="border-left:2px solid #2a0000;padding:0.6rem 1rem">
        <div style="font-family:monospace;font-size:0.9rem">🔴 Need more data</div>
        <div style="color:#555;font-size:0.82rem;margin-top:0.25rem">
          ΔW &lt; 2W. Near the noise floor. Could be measurement artefact.
          We publish it anyway — but we won't cite it yet.</div>
      </div>
    </div>
  </div>

  <div class="band">
    <div class="band-label">Why these thresholds?</div>
    <p style="color:#555;line-height:1.7;max-width:560px;margin-bottom:0.75rem">
      Five watts of delta gives a ~5:1 signal-to-noise ratio against the P110
      noise floor — enough to be confident the task is the cause, not variance.
      Ten polls means ten seconds of measurement. Short tasks like TinyLlama
      inference (1–4s total) often land in yellow or red. That's not a failure
      — it tells us to run the task in batch mode to accumulate measurement time.
    </p>
    <p style="color:#555;line-height:1.7;max-width:560px">
      The thresholds are configurable in lab Settings and applied consistently
      across video, LLM, and image generation results.
      On any result page, click a 🟢 🟡 🔴 badge for a quick reminder.
    </p>
  </div>

  <div class="btn-row" style="margin-top:0.5rem">
    <button class="btn btn-primary" onclick="goStep(5)">See findings →</button>
    <button class="btn btn-secondary" onclick="goStep(1)">← Start over</button>
  </div>
</div>

<!-- Step 5: Findings -->
<div class="step" id="step-5">
  <h1>Findings</h1>
  <p style="color:#555;font-size:0.85rem;margin-bottom:1.5rem">
    Greening of Streaming · WattLab · GoS1</p>

  <div id="summary-content">
    <p style="color:#555;font-size:0.85rem">Loading results…</p>
  </div>

  <hr class="divider">
  <div class="btn-row">
    <button class="btn btn-secondary" onclick="goStep(1)">← Start over</button>
    <a href="https://greeningofstreaming.org" target="_blank"
       class="btn btn-secondary" style="text-decoration:none;display:inline-block;line-height:1">
      greeningofstreaming.org ↗</a>
  </div>
  <p class="scope-note" style="margin-top:1.5rem">
    Scope: device layer only (GoS1). Network, CDN, CPE excluded.<br>
    LLM: no amortised training cost included.</p>
</div>

<script>
// ─── State ──────────────────────────────────────────────────────────────────
let currentStep = 0;
let videoResult = null;
let llmResult = null;
let imageResult = null;
const stepLabels = ['Welcome', 'Video Transcode', 'LLM Inference', 'Image Generation', 'Confidence', 'Findings'];
let streamTimer = null;
let imageTimer = null;

// ─── Step navigation ─────────────────────────────────────────────────────────
function goStep(n) {{
  document.querySelectorAll('.step').forEach(el => el.classList.remove('active'));
  document.getElementById('step-' + n).classList.add('active');
  for (let i = 0; i < 6; i++) {{
    const dot = document.getElementById('dot-' + i);
    dot.className = 'dot' + (i < n ? ' done' : i === n ? ' active' : '');
  }}
  const lbl = document.getElementById('nav-label');
  lbl.textContent = stepLabels[n];
  lbl.className = 'label active';
  currentStep = n;
  window.scrollTo(0, 0);
  if (n === 1 && !videoResult) loadVideoStep();
  if (n === 2 && !llmResult) loadLLMStep();
  if (n === 3 && !imageResult) loadImageStep();
  if (n === 1 && videoResult) revealNext(1);
  if (n === 2 && llmResult) revealNext(2);
  if (n === 3 && imageResult) revealNext(3);
  if (n === 5) buildSummary();
}}

function revealNext(n) {{
  const el = document.getElementById('next-' + n);
  if (el) el.style.display = 'block';
}}

function loadVideoStep() {{
  document.getElementById('video-status').innerHTML = '<p class="progress-note" style="color:#555">Loading last result…</p>';
  showPrevVideo();
}}
function loadLLMStep() {{
  document.getElementById('llm-status').innerHTML = '<p class="progress-note" style="color:#555">Loading last result…</p>';
  showPrevLLM();
}}
function loadImageStep() {{
  document.getElementById('image-status').innerHTML = '<p class="progress-note" style="color:#555">Loading last result…</p>';
  showPrevImage();
}}

// ─── Live power ───────────────────────────────────────────────────────────────
async function refreshPower() {{
  try {{
    const resp = await fetch('/power');
    const data = await resp.json();
    document.getElementById('live-watts').textContent = data.watts.toFixed(1) + ' W';
  }} catch(e) {{}}
}}
refreshPower();
setInterval(refreshPower, 10000);

// ─── Helpers ─────────────────────────────────────────────────────────────────
function timeAgo(isoStr) {{
  if (!isoStr) return '';
  const diff = (Date.now() - new Date(isoStr)) / 1000;
  if (diff < 120) return 'just now';
  if (diff < 3600) return Math.floor(diff/60) + ' min ago';
  if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
  return Math.floor(diff/86400) + 'd ago';
}}

function fmt(v, dec=2) {{ return v != null ? Number(v).toFixed(dec) : '—'; }}

// ─── Previous run ─────────────────────────────────────────────────────────────
async function showPrevVideo() {{
  document.getElementById('video-btns').style.display = 'none';
  try {{
    const resp = await fetch('/results/video/list');
    const list = await resp.json();
    if (!list || list.length === 0) {{
      document.getElementById('video-status').innerHTML = '';
      document.getElementById('video-btns').style.display = 'flex';
      return;
    }}
    // Load full result for the most recent job
    const meta = list[0];
    const r2 = await fetch('/results/video/' + meta.job_id + '/download.json');
    const full = await r2.json();
    videoResult = full;
    renderVideoResult(full, meta.saved_at, true);
  }} catch(e) {{
    document.getElementById('video-btns').style.display = 'flex';
    document.getElementById('video-status').innerHTML =
      '<p class="progress-note" style="color:#ff4400">Error: ' + e + '</p>';
  }}
}}

async function showPrevLLM() {{
  document.getElementById('llm-btns').style.display = 'none';
  try {{
    const resp = await fetch('/results/llm/list');
    const list = await resp.json();
    if (!list || list.length === 0) {{
      document.getElementById('llm-status').innerHTML = '';
      document.getElementById('llm-btns').style.display = 'flex';
      return;
    }}
    const meta = list[0];
    const r2 = await fetch('/results/llm/' + meta.job_id + '/download.json');
    const full = await r2.json();
    llmResult = full;
    renderLLMResult(full, meta.saved_at, true);
  }} catch(e) {{
    document.getElementById('llm-btns').style.display = 'flex';
    document.getElementById('llm-status').innerHTML =
      '<p class="progress-note" style="color:#ff4400">Error: ' + e + '</p>';
  }}
}}


// ─── Run new video measurement ────────────────────────────────────────────────
async function runDemoVideo() {{
  document.getElementById('video-btns').style.display = 'none';
  document.getElementById('video-status').innerHTML =
    '<p class="progress-note">▶ Starting measurement…</p>';
  try {{
    const form = new FormData();
    form.append('source_key', 'meridian_4k');
    form.append('preset', 'both');
    const resp = await fetch('/video/use-source', {{method:'POST', body:form}});
    const data = await resp.json();
    if (data.job_id) {{
      pollVideo(data.job_id, Date.now());
    }} else {{
      showVideoError(JSON.stringify(data));
    }}
  }} catch(e) {{ showVideoError(e); }}
}}

function showVideoError(msg) {{
  document.getElementById('video-btns').style.display = 'flex';
  document.getElementById('video-status').innerHTML =
    '<p class="progress-note" style="color:#ff4400">Error: ' + msg + '</p>';
}}

const VIDEO_STAGE_LABELS = {{
  'starting': 'Initialising…',
  'baseline': 'Measuring baseline (10s)…',
  'cpu_encode': 'CPU encoding — measuring power…',
  'rest': 'Thermal cooldown (60s)…',
  'baseline_2': 'Measuring GPU baseline…',
  'gpu_encode': 'GPU encoding — measuring power…',
  'done': 'Complete',
}};

function pollVideo(jobId, t0) {{
  const elapsed = Math.floor((Date.now()-t0)/1000);
  const m = Math.floor(elapsed/60), s = elapsed%60;
  const eStr = m > 0 ? m+'m '+s+'s' : s+'s';
  fetch('/video/job/' + jobId).then(r=>r.json()).then(data => {{
    if (data.status === 'done') {{
      videoResult = data.result;
      renderVideoResult(data.result, new Date().toISOString(), false);
    }} else if (data.status === 'error') {{
      showVideoError(data.error);
    }} else {{
      const label = VIDEO_STAGE_LABELS[data.stage] || data.stage || '…';
      document.getElementById('video-status').innerHTML =
        '<p class="progress-note">▶ ' + label + '</p>' +
        '<p class="dim mono" style="font-size:0.78rem;margin-top:0.4rem">Elapsed: ' + eStr + '</p>';
      setTimeout(() => pollVideo(jobId, t0), 5000);
    }}
  }}).catch(() => setTimeout(() => pollVideo(jobId, t0), 5000));
}}

// ─── Run new LLM measurement ──────────────────────────────────────────────────
async function runDemoLLM() {{
  document.getElementById('llm-btns').style.display = 'none';
  document.getElementById('llm-status').innerHTML =
    '<p class="progress-note">▶ Starting measurement…</p>' +
    '<div class="stream-box" id="stream-box"></div>';
  try {{
    const form = new FormData();
    form.append('model_key', 'mistral');
    form.append('task_key', 'T3');
    form.append('repeats', '1');
    form.append('warm', 'false');
    const resp = await fetch('/llm/run', {{method:'POST', body:form}});
    const data = await resp.json();
    if (data.job_id) {{
      pollLLM(data.job_id, Date.now());
    }} else {{
      showLLMError(JSON.stringify(data));
    }}
  }} catch(e) {{ showLLMError(e); }}
}}

function showLLMError(msg) {{
  document.getElementById('llm-btns').style.display = 'flex';
  document.getElementById('llm-status').innerHTML =
    '<p class="progress-note" style="color:#ff4400">Error: ' + msg + '</p>';
}}

function pollLLM(jobId, t0) {{
  const elapsed = Math.floor((Date.now()-t0)/1000);
  const m = Math.floor(elapsed/60), s = elapsed%60;
  const eStr = m > 0 ? m+'m '+s+'s' : s+'s';
  fetch('/llm/job/' + jobId).then(r=>r.json()).then(data => {{
    if (data.status === 'done') {{
      if (streamTimer) {{ clearTimeout(streamTimer); streamTimer = null; }}
      llmResult = data.result;
      renderLLMResult(data.result, new Date().toISOString(), false);
    }} else if (data.status === 'error') {{
      if (streamTimer) {{ clearTimeout(streamTimer); streamTimer = null; }}
      showLLMError(data.error);
    }} else {{
      const stage = data.stage || '';
      const stageLabel = stage === 'baseline' ? 'Measuring baseline…' :
                         stage.startsWith('inference') ? 'Running inference…' : stage + '…';
      document.getElementById('llm-status').innerHTML =
        '<p class="progress-note">▶ ' + stageLabel + '</p>' +
        '<p class="dim mono" style="font-size:0.78rem;margin-top:0.4rem">Elapsed: ' + eStr + '</p>' +
        '<div class="stream-box" id="stream-box">' +
          (data.partial_response || '') + '</div>';
      const delay = stage.startsWith('inference') ? 500 : 3000;
      streamTimer = setTimeout(() => pollLLM(jobId, t0), delay);
    }}
  }}).catch(() => {{ streamTimer = setTimeout(() => pollLLM(jobId, t0), 5000); }});
}}

// ─── Result renderers ─────────────────────────────────────────────────────────
function renderVideoResult(r, savedAt, isPrev) {{
  const prevNote = isPrev ? '<p class="prev-note">↩ Previous run · ' + timeAgo(savedAt) + '</p>' : '';
  let html = prevNote;
  if (r.mode === 'both') {{
    const cpu = r.cpu, gpu = r.gpu, a = r.analysis;
    const ce = cpu.energy, ge = gpu.energy;
    const winner = a.energy_winner;
    html += `<div class="result-card">
      <p class="headline">${{a.finding}}</p>
      <div class="kpi-row">
        <div class="kpi">
          <div class="val">${{fmt(ce.delta_e_wh,4)}} Wh</div>
          <div class="lbl">CPU energy ${{winner==='CPU'?'✓ winner':''}}</div>
        </div>
        <div class="kpi">
          <div class="val">${{fmt(ge.delta_e_wh,4)}} Wh</div>
          <div class="lbl">GPU energy ${{winner==='GPU'?'✓ winner':''}}</div>
        </div>
        <div class="kpi">
          <div class="val">${{ce.delta_t_s}}s / ${{ge.delta_t_s}}s</div>
          <div class="lbl">Encode time CPU / GPU</div>
        </div>
      </div>
      <div class="conf-badge">${{ce.confidence.flag}} CPU · ${{ge.confidence.flag}} GPU · ${{a.confidence_note}}</div>
      <p class="scope-note">Device layer only (GoS1). Network, CDN, CPE excluded.</p>
    </div>`;
  }} else {{
    const res = r.result || r;
    const e = res.energy;
    html += `<div class="result-card">
      <p class="headline">${{res.preset_label}}: ${{e.delta_e_wh}} Wh · ${{e.delta_t_s}}s</p>
      <div class="kpi-row">
        <div class="kpi"><div class="val">${{e.delta_e_wh}} Wh</div><div class="lbl">Energy delta</div></div>
        <div class="kpi"><div class="val">${{e.delta_w}} W</div><div class="lbl">Power delta</div></div>
        <div class="kpi"><div class="val">${{e.delta_t_s}}s</div><div class="lbl">Duration</div></div>
      </div>
      <div class="conf-badge">${{e.confidence.flag}} ${{e.confidence.label}}</div>
    </div>`;
  }}
  document.getElementById('video-status').innerHTML = html;
  document.getElementById('video-btns').style.display = 'none';
  revealNext(1);
}}

function renderLLMResult(r, savedAt, isPrev) {{
  const prevNote = isPrev ? '<p class="prev-note">↩ Previous run · ' + timeAgo(savedAt) + '</p>' : '';
  let html = prevNote;

  if (r.mode === 'both') {{
    // CPU vs GPU comparison result
    const ce = r.cpu && r.cpu.energy, ge = r.gpu && r.gpu.energy;
    const ci = r.cpu && r.cpu.inference, gi = r.gpu && r.gpu.inference;
    const a = r.analysis || {{}};
    html += `<div class="result-card">
      <p class="headline">${{a.finding || ''}}</p>
      <div style="display:flex;gap:1.5rem;flex-wrap:wrap;margin-bottom:1rem">
        <div style="flex:1;min-width:180px">
          <div style="color:#444;font-size:0.72rem;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:0.5rem">CPU</div>
          <div class="kpi-row">
            <div class="kpi"><div class="val">${{fmt(ce && ce.mwh_per_token,4)}}</div><div class="lbl">mWh/token</div></div>
            <div class="kpi"><div class="val">${{fmt(ci && ci.tokens_per_sec,1)}}</div><div class="lbl">tok/s</div></div>
          </div>
        </div>
        <div style="flex:1;min-width:180px">
          <div style="color:#444;font-size:0.72rem;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:0.5rem">GPU</div>
          <div class="kpi-row">
            <div class="kpi"><div class="val">${{fmt(ge && ge.mwh_per_token,4)}}</div><div class="lbl">mWh/token</div></div>
            <div class="kpi"><div class="val">${{fmt(gi && gi.tokens_per_sec,1)}}</div><div class="lbl">tok/s</div></div>
          </div>
        </div>
      </div>
      <div class="conf-badge">${{ce ? ce.confidence.flag : ''}} CPU · ${{ge ? ge.confidence.flag : ''}} GPU · ${{r.model_label}}</div>
      <p class="scope-note">Device layer only (GoS1). No amortised training cost.</p>
    </div>`;
  }} else {{
    // single or batch
    let e = r.energy;
    let inf = r.inference;
    if (!e && r.runs && r.runs.length) {{
      e = r.runs[r.runs.length-1].energy;
      inf = r.runs[r.runs.length-1].inference;
    }}
    if (!e && r.summary) {{
      // batch summary only — reconstruct a minimal e object
      e = {{ mwh_per_token: r.summary.mwh_per_token_mean, delta_e_wh: r.summary.delta_e_wh_mean,
             confidence: {{flag:'—',label:'see runs'}}, delta_w: null }};
      inf = {{ tokens_per_sec: r.summary.tokens_per_sec_mean, output_tokens: '—', response: '' }};
    }}
    if (!e) {{
      document.getElementById('llm-btns').style.display = 'flex';
      document.getElementById('llm-status').innerHTML = '<p class="progress-note" style="color:#555">Result format not recognised — run a new measurement.</p>';
      return;
    }}
    const modeNote = r.warm ? '🌡 Warm' : '❄ Cold';
    html += `<div class="result-card">
      <div class="kpi-row">
        <div class="kpi"><div class="val">${{fmt(e.mwh_per_token,4)}}</div><div class="lbl">mWh / token</div></div>
        <div class="kpi"><div class="val">${{fmt(inf && inf.tokens_per_sec,1)}}</div><div class="lbl">tokens / sec</div></div>
        <div class="kpi"><div class="val">${{fmt(e.delta_e_wh,4)}} Wh</div><div class="lbl">total energy</div></div>
        <div class="kpi"><div class="val">${{inf ? inf.output_tokens : '—'}}</div><div class="lbl">output tokens</div></div>
      </div>
      <div class="conf-badge">${{e.confidence.flag}} ${{e.confidence.label}} · ${{r.model_label}} · ${{modeNote}}</div>
      ${{inf && inf.response ? '<div class="response-preview">' + inf.response + '</div>' : ''}}
      <p class="scope-note">Device layer only (GoS1). No amortised training cost.</p>
    </div>`;
  }}

  document.getElementById('llm-status').innerHTML = html;
  document.getElementById('llm-btns').style.display = 'none';
  revealNext(2);
}}

function resetVideoStep() {{
  videoResult = null;
  document.getElementById('video-btns').style.display = 'flex';
  document.getElementById('video-status').innerHTML = '';
  document.getElementById('next-1').style.display = 'none';
}}
function resetLLMStep() {{
  llmResult = null;
  document.getElementById('llm-btns').style.display = 'flex';
  document.getElementById('llm-status').innerHTML = '';
  document.getElementById('next-2').style.display = 'none';
}}
function resetImageStep() {{
  imageResult = null;
  document.getElementById('image-btns').style.display = 'flex';
  document.getElementById('image-status').innerHTML = '';
  document.getElementById('next-3').style.display = 'none';
}}

// ─── Image ────────────────────────────────────────────────────────────────────
async function runDemoImage() {{
  document.getElementById('image-btns').style.display = 'none';
  document.getElementById('image-status').innerHTML =
    '<p class="progress-note">Submitting…</p>';
  const resp = await fetch('/image/start', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
    body: 'prompt=' + encodeURIComponent('a lone wind turbine in an open landscape'),
  }});
  const data = await resp.json();
  if (data.error) {{
    document.getElementById('image-btns').style.display = 'flex';
    document.getElementById('image-status').innerHTML =
      '<p class="progress-note" style="color:#ff4400">' + data.error + '</p>';
    return;
  }}
  pollDemoImage(data.job_id);
}}

async function pollDemoImage(jobId) {{
  try {{
    const r = await fetch('/image/job/' + jobId);
    const j = await r.json();
    if (j.stage === 'queued') {{
      document.getElementById('image-status').innerHTML =
        '<p class="progress-note">⏱ Queued — position ' + j.queue_position +
        '. Will start automatically.</p>';
      imageTimer = setTimeout(() => pollDemoImage(jobId), 3000);
      return;
    }}
    if (j.stage === 'done' && j.result) {{
      imageResult = j.result;
      renderDemoImageResult(j.result);
      return;
    }}
    if (j.error) {{
      document.getElementById('image-status').innerHTML =
        '<p class="progress-note" style="color:#ff4400">Error: ' + j.error + '</p>';
      document.getElementById('image-btns').style.display = 'flex';
      return;
    }}
    const stageLabel = j.stage === 'generating' ? 'Generating image…' :
                       j.stage === 'baseline' ? 'Measuring baseline…' : j.stage;
    document.getElementById('image-status').innerHTML =
      '<p class="progress-note">⚡ ' + stageLabel + '</p>';
    imageTimer = setTimeout(() => pollDemoImage(jobId), 2000);
  }} catch(e) {{
    imageTimer = setTimeout(() => pollDemoImage(jobId), 3000);
  }}
}}

async function showPrevImage() {{
  document.getElementById('image-btns').style.display = 'none';
  try {{
    const resp = await fetch('/results/image/list');
    const list = await resp.json();
    if (!list || list.length === 0) {{
      document.getElementById('image-status').innerHTML = '';
      document.getElementById('image-btns').style.display = 'flex';
      return;
    }}
    const meta = list[0];
    const r2 = await fetch('/results/image/' + meta.job_id + '/download.json');
    const full = await r2.json();
    imageResult = full;
    renderDemoImageResult(full);
  }} catch(e) {{
    document.getElementById('image-btns').style.display = 'flex';
    document.getElementById('image-status').innerHTML =
      '<p class="progress-note" style="color:#ff4400">Error: ' + e + '</p>';
  }}
}}

function renderDemoImageResult(r) {{
  let html = '';
  if (r.mode === 'both') {{
    const ce = r.cpu && r.cpu.energy, ge = r.gpu && r.gpu.energy;
    const cg = r.cpu && r.cpu.generation, gg = r.gpu && r.gpu.generation;
    const a = r.analysis || {{}};
    const cpuWh = ce && (ce.wh_per_image || ce.delta_e_wh);
    const gpuWh = ge && (ge.wh_per_image || ge.delta_e_wh);
    let imgs = '';
    if (cg && cg.b64_png) imgs += '<div style="flex:1;min-width:150px"><div style="color:#444;font-size:0.7rem;margin-bottom:0.4rem">CPU</div>' +
      '<img src="data:image/png;base64,' + cg.b64_png + '" style="max-width:100%;border:1px solid #222;display:block"></div>';
    if (gg && gg.b64_png) imgs += '<div style="flex:1;min-width:150px"><div style="color:#444;font-size:0.7rem;margin-bottom:0.4rem">GPU</div>' +
      '<img src="data:image/png;base64,' + gg.b64_png + '" style="max-width:100%;border:1px solid #222;display:block"></div>';
    html = '<div class="result-card">' +
      '<p class="headline">' + (a.finding || '') + '</p>' +
      '<div class="kpi-row">' +
      '<div class="kpi"><div class="val">' + fmt(cpuWh,4) + ' Wh</div><div class="lbl">CPU / image</div></div>' +
      '<div class="kpi"><div class="val">' + fmt(gpuWh,4) + ' Wh</div><div class="lbl">GPU / image</div></div>' +
      '<div class="kpi"><div class="val">' + fmt(cg && cg.gen_s,1) + 's / ' + fmt(gg && (gg.gen_s_per_image || gg.gen_s),1) + 's</div><div class="lbl">time CPU / GPU</div></div>' +
      '</div>' +
      (ce ? '<div class="conf-badge">' + ce.confidence.flag + ' CPU · ' + (ge ? ge.confidence.flag + ' GPU' : '') + '</div>' : '') +
      '<div style="display:flex;gap:0.75rem;flex-wrap:wrap;margin-top:1rem">' + imgs + '</div>' +
      '</div>';
  }} else {{
    const e = r.energy;
    const gen = r.generation;
    if (!e) {{
      document.getElementById('image-btns').style.display = 'flex';
      document.getElementById('image-status').innerHTML = '<p class="progress-note" style="color:#555">Result format not recognised — run a new measurement.</p>';
      return;
    }}
    const wh = e.wh_per_image || e.delta_e_wh;
    const imgHtml = gen && gen.b64_png
      ? '<img src="data:image/png;base64,' + gen.b64_png +
        '" style="max-width:100%;border:1px solid #222;display:block;margin-top:1rem">' +
        '<div style="color:#444;font-size:0.75rem;margin-top:0.5rem;font-style:italic">"' +
        (r.full_prompt || '') + '"</div>'
      : '';
    html = '<div class="result-card">' +
      '<div class="kpi-row">' +
      '<div class="kpi"><div class="val">' + fmt(wh,4) + ' Wh</div><div class="lbl">energy / image</div></div>' +
      '<div class="kpi"><div class="val">' + fmt(gen && gen.total_s,1) + 's</div><div class="lbl">generation time</div></div>' +
      '<div class="kpi"><div class="val">' + fmt(e.delta_w,1) + ' W</div><div class="lbl">delta above idle</div></div>' +
      '</div>' +
      '<div class="conf-badge">' + e.confidence.flag + ' ' + e.confidence.label + '</div>' +
      imgHtml + '</div>';
  }}
  document.getElementById('image-status').innerHTML = html;
  document.getElementById('image-btns').style.display = 'none';
  revealNext(3);
}}

// ─── Summary ─────────────────────────────────────────────────────────────────
function buildSummary() {{
  const el = document.getElementById('summary-content');
  let rows = '';
  try {{

  // Video
  try {{
    if (videoResult && videoResult.mode === 'both') {{
      const a = videoResult.analysis || {{}};
      const ce = videoResult.cpu && videoResult.cpu.energy;
      const ge = videoResult.gpu && videoResult.gpu.energy;
      rows += `<tr><td>Video · CPU energy</td><td>${{fmt(ce && ce.delta_e_wh,4)}} Wh ${{a.energy_winner==='CPU'?'✓':''}}</td></tr>`;
      rows += `<tr><td>Video · GPU energy</td><td>${{fmt(ge && ge.delta_e_wh,4)}} Wh ${{a.energy_winner==='GPU'?'✓':''}}</td></tr>`;
      rows += `<tr><td>Video · Finding</td><td style="color:#aaa;font-size:0.78rem">${{a.finding || a.energy_winner + ' used less energy'}}</td></tr>`;
    }} else if (videoResult) {{
      const e = videoResult.energy || (videoResult.result && videoResult.result.energy);
      rows += `<tr><td>Video · Energy</td><td>${{fmt(e && e.delta_e_wh,4)}} Wh</td></tr>`;
    }} else {{
      rows += `<tr><td>Video</td><td style="color:#333">—</td></tr>`;
    }}
  }} catch(err) {{ rows += `<tr><td>Video</td><td style="color:#555">error: ${{err.message}}</td></tr>`; }}

  // LLM
  try {{
    if (llmResult && llmResult.mode === 'both') {{
      const a = llmResult.analysis || {{}};
      const ce = llmResult.cpu && llmResult.cpu.energy;
      const ge = llmResult.gpu && llmResult.gpu.energy;
      rows += `<tr><td>LLM · Model</td><td>${{llmResult.model_label || ''}}</td></tr>`;
      rows += `<tr><td>LLM · CPU mWh/token</td><td>${{fmt(ce && ce.mwh_per_token,4)}} ${{a.mwh_winner==='CPU'?'✓':''}}</td></tr>`;
      rows += `<tr><td>LLM · GPU mWh/token</td><td>${{fmt(ge && ge.mwh_per_token,4)}} ${{a.mwh_winner==='GPU'?'✓':''}}</td></tr>`;
    }} else if (llmResult) {{
      let e = llmResult.energy;
      let inf = llmResult.inference;
      if (!e && llmResult.runs && llmResult.runs.length) {{
        e = llmResult.runs[llmResult.runs.length-1].energy;
        inf = llmResult.runs[llmResult.runs.length-1].inference;
      }}
      if (!e && llmResult.summary) {{
        e = {{ mwh_per_token: llmResult.summary.mwh_per_token_mean }};
        inf = {{ tokens_per_sec: llmResult.summary.tokens_per_sec_mean }};
      }}
      rows += `<tr><td>LLM · Model</td><td>${{llmResult.model_label || ''}}</td></tr>`;
      rows += `<tr><td>LLM · Energy / token</td><td>${{fmt(e && e.mwh_per_token,4)}} mWh/token</td></tr>`;
      rows += `<tr><td>LLM · Speed</td><td>${{fmt(inf && inf.tokens_per_sec,1)}} tok/s</td></tr>`;
    }} else {{
      rows += `<tr><td>LLM</td><td style="color:#333">—</td></tr>`;
    }}
  }} catch(err) {{ rows += `<tr><td>LLM</td><td style="color:#555">error: ${{err.message}}</td></tr>`; }}

  // Image
  try {{
    if (imageResult && imageResult.mode === 'both') {{
      const a = imageResult.analysis || {{}};
      const ce = imageResult.cpu && imageResult.cpu.energy;
      const ge = imageResult.gpu && imageResult.gpu.energy;
      const cg = imageResult.cpu && imageResult.cpu.generation;
      const gg = imageResult.gpu && imageResult.gpu.generation;
      rows += `<tr><td>Image · CPU Wh/image</td><td>${{fmt(ce && (ce.wh_per_image||ce.delta_e_wh),4)}} Wh ${{a.energy_winner==='cpu'?'✓':''}}</td></tr>`;
      rows += `<tr><td>Image · GPU Wh/image</td><td>${{fmt(ge && (ge.wh_per_image||ge.delta_e_wh),4)}} Wh ${{a.energy_winner==='gpu'?'✓':''}}</td></tr>`;
      rows += `<tr><td>Image · Time CPU/GPU</td><td>${{fmt(cg && cg.gen_s,1)}}s / ${{fmt(gg && (gg.gen_s_per_image||gg.gen_s),1)}}s</td></tr>`;
    }} else if (imageResult) {{
      const e = imageResult.energy;
      const gen = imageResult.generation;
      rows += `<tr><td>Image · Wh / image</td><td>${{fmt(e && (e.wh_per_image||e.delta_e_wh),4)}} Wh</td></tr>`;
      rows += `<tr><td>Image · Generation time</td><td>${{fmt(gen && gen.total_s,1)}}s</td></tr>`;
    }} else {{
      rows += `<tr><td>Image</td><td style="color:#333">—</td></tr>`;
    }}
  }} catch(err) {{ rows += `<tr><td>Image</td><td style="color:#555">error: ${{err.message}}</td></tr>`; }}

  }} catch(outerErr) {{
    el.innerHTML = '<p style="color:#ff4400;font-family:monospace;font-size:0.82rem">Summary error: ' + outerErr + '</p>';
    return;
  }}

  el.innerHTML = `
    <table class="summary-table"><tbody>${{rows}}</tbody></table>
    <p style="color:#555;font-size:0.82rem;line-height:1.7;margin-top:1.5rem;max-width:560px">
      These figures are from live measurements on GoS1, a server in France,
      using a calibrated smart plug. Not modelled. Not averaged.
      Reproducible by anyone with the same hardware.
    </p>`;
}}
</script>
    {_CONF_HELP_WIDGET}
    {_FOOTER}
</body>
</html>"""

@app.get("/image", response_class=HTMLResponse)
async def image_page():
    queue_depth = len(pending_queue) + (1 if current_job_id else 0)
    busy_banner = (f'<div style="background:#333;color:#ffaa00;padding:0.75rem 1rem;'
                   f'margin-bottom:1rem;font-size:0.85rem">'
                   f'⏱ {queue_depth} job{"s" if queue_depth != 1 else ""} in queue — '
                   f'yours will be added and run automatically.</div>') \
        if queue_depth > 0 else ""

    prev_runs = list_results("image", limit=5)
    prev_html = ""
    if prev_runs:
        prev_html = '<div class="prev-runs"><h3>Previous runs</h3>'
        for r in prev_runs:
            fp = r.get("full_prompt", "")
            date_str = (r.get("saved_at") or "")[:16].replace("T", " ")
            mode = r.get("mode", "cpu")
            downloads = (
                f'<a href="/results/image/{r["job_id"]}/download.json" download '
                f'style="color:#333;font-size:0.72rem;text-decoration:none;margin-right:0.75rem">↓ JSON</a>'
                f'<a href="/results/image/{r["job_id"]}/download.csv" download '
                f'style="color:#333;font-size:0.72rem;text-decoration:none">↓ CSV</a>'
            )
            if mode == "both":
                def _side_html(label, s):
                    img = (f'<img src="data:image/png;base64,{s["b64_png"]}" '
                           f'style="width:64px;height:64px;object-fit:cover;margin-right:0.5rem">'
                           if s.get("b64_png") else "")
                    conf = s.get("confidence", {})
                    return (f'<div style="display:flex;align-items:center;margin-top:0.4rem">'
                            f'{img}<span style="color:#555;font-size:0.78rem">'
                            f'<span style="color:#aaa">{label}</span> &nbsp;·&nbsp; '
                            f'{conf.get("flag","")} {conf.get("label","")} &nbsp;·&nbsp; '
                            f'{s.get("delta_e_wh","?")} Wh &nbsp;·&nbsp; {s.get("delta_t_s","?")}s'
                            f'</span></div>')
                prev_html += f"""<div class="prev-item" style="flex-direction:column;align-items:flex-start">
                  <span class="prev-meta">{date_str} &nbsp;·&nbsp; CPU vs GPU</span>
                  {_side_html("CPU", r.get("cpu", {}))}
                  {_side_html("GPU", r.get("gpu", {}))}
                  <div class="prev-prompt" style="color:#555;font-size:0.75rem;margin-top:0.3rem">{fp[:80]}</div>
                  <div style="margin-top:0.3rem">{downloads}</div>
                </div>"""
            else:
                conf = r.get("confidence", {})
                img_tag = (f'<img src="data:image/png;base64,{r["b64_png"]}" '
                           f'style="width:80px;height:80px;object-fit:cover;vertical-align:middle;margin-right:0.75rem">'
                           if r.get("b64_png") else "")
                mode_label = {"cpu": "CPU", "gpu": "GPU"}.get(mode, mode)
                prev_html += f"""<div class="prev-item">
                  {img_tag}
                  <div>
                    <span class="prev-meta">
                      {date_str} &nbsp;·&nbsp; {mode_label}
                      &nbsp;·&nbsp; {conf.get("flag","")} {conf.get("label","")}
                      &nbsp;·&nbsp; {r.get("delta_e_wh","?")} Wh/image
                      &nbsp;·&nbsp; {r.get("delta_t_s","?")}s
                    </span>
                    <div class="prev-prompt" style="color:#555;font-size:0.75rem;margin-top:0.3rem">{fp[:80]}</div>
                    <div style="margin-top:0.3rem">{downloads}</div>
                  </div>
                </div>"""
        prev_html += "</div>"

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>WattLab — Image Generation Test</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: monospace; background: #0a0a0a; color: #e0e0e0;
               max-width: 780px; margin: 0 auto; padding: 2rem; }}
        h1 {{ color: #00ff99; margin-bottom: 0.25rem; font-size: 1.6rem; }}
        .subtitle {{ color: #555; font-size: 0.8rem; margin-bottom: 1.5rem; }}
        .info {{ color: #777; font-size: 0.82rem; margin-bottom: 1.5rem;
                 border-left: 2px solid #222; padding-left: 1rem; line-height: 1.6; }}
        textarea {{ width: 100%; background: #111; color: #e0e0e0; border: 1px solid #333;
                    padding: 0.75rem; font-family: monospace; font-size: 0.9rem;
                    resize: vertical; margin-bottom: 1rem; }}
        button {{ background: #00ff99; color: #000; border: none;
                  padding: 0.75rem 2rem; cursor: pointer;
                  font-family: monospace; font-size: 1rem; }}
        button:disabled {{ background: #222; color: #555; cursor: not-allowed; }}
        button:hover:not(:disabled) {{ background: #00dd88; }}
        #status {{ margin-top: 1.5rem; }}
        .progress-box {{ border: 1px solid #222; padding: 1.5rem; }}
        .progress-header {{ color: #ffaa00; font-size: 0.9rem; margin-bottom: 1.25rem; }}
        .stages {{ display: flex; flex-direction: column; gap: 0.5rem; margin-bottom: 1.25rem; }}
        .stage {{ display: flex; align-items: center; gap: 0.75rem; font-size: 0.82rem; }}
        .stage-icon {{ width: 1.2rem; text-align: center; flex-shrink: 0; }}
        .live-watts {{ font-size: 2rem; color: #00ff99; font-weight: bold; margin-top: 0.5rem; }}
        .result-box {{ border: 1px solid #00ff9944; padding: 1.5rem; margin-top: 1.5rem; }}
        .result-box h2 {{ color: #00ff99; font-size: 1.1rem; margin-bottom: 1.25rem; }}
        .kpis {{ display: flex; gap: 1.5rem; flex-wrap: wrap; margin-bottom: 1.25rem; }}
        .kpi {{ display: flex; flex-direction: column; gap: 0.25rem; }}
        .kpi .val {{ font-size: 1.4rem; color: #00ff99; font-weight: bold; }}
        .kpi .lbl {{ font-size: 0.72rem; color: #555; }}
        .conf-badge {{ display: inline-block; border: 1px solid #333; padding: 0.25rem 0.75rem;
                       font-size: 0.8rem; color: #aaa; margin-bottom: 1rem; }}
        .scope-note {{ color: #333; font-size: 0.75rem; margin-top: 1rem; }}
        .prev-runs {{ margin-top: 2rem; border-top: 1px solid #1a1a1a; padding-top: 1.5rem; }}
        .prev-runs h3 {{ color: #444; font-size: 0.85rem; margin-bottom: 1rem; }}
        .prev-item {{ padding: 0.75rem 0; border-bottom: 1px solid #111;
                      display: flex; align-items: flex-start; flex-wrap: wrap; }}
        .prev-meta {{ color: #555; font-size: 0.78rem; }}
        .image-preview {{ margin-top: 1.25rem; }}
        .image-preview img {{ max-width: 100%; border: 1px solid #222; display: block; }}
        .image-caption {{ color: #444; font-size: 0.75rem; margin-top: 0.5rem; font-style: italic; }}
        .back {{ color: #555; font-size: 0.8rem; margin-bottom: 1.5rem; display: block; }}
        .back:hover {{ color: #00ff99; }}
    </style>
</head>
<body>
    {_BACK}
    {busy_banner}
    <h1>Image Generation Test</h1>
    <div class="subtitle">SD-Turbo · CPU {IMAGE_STEPS_CPU} steps / GPU {IMAGE_STEPS_GPU} steps × {GPU_BATCH_SIZE} images · 512×512</div>
    <div class="info">
        Measures the wall-power cost of generating one AI image from text.<br>
        CPU: {IMAGE_STEPS_CPU} steps (~12s). GPU: batch of {GPU_BATCH_SIZE} images × {IMAGE_STEPS_GPU} steps (~10s total) → energy per image = total/{GPU_BATCH_SIZE}.<br>
        Each run appends a random colour/mood modifier — live proof the image is generated, not replayed.<br>
        Model: <code>stabilityai/sd-turbo</code> · GPU backend: ROCm (<code>HSA_OVERRIDE_GFX_VERSION=11.0.0</code>)
    </div>

    <label style="color:#888;font-size:0.8rem;display:block;margin-bottom:0.4rem">Prompt</label>
    <textarea id="prompt" rows="3">a lone wind turbine in an open landscape</textarea>
    <div style="color:#555;font-size:0.75rem;margin-bottom:1.2rem">
        A random colour/mood modifier is appended per run (e.g. "bathed in emerald light").
    </div>

    <div style="margin-bottom:1.25rem">
      <span style="color:#888;font-size:0.8rem;margin-right:1rem">Backend:</span>
      <label style="font-size:0.85rem;margin-right:1.2rem;cursor:pointer">
        <input type="radio" name="img-device" value="cpu" checked onchange="selectedDevice=this.value"> CPU
      </label>
      <label style="font-size:0.85rem;margin-right:1.2rem;cursor:pointer">
        <input type="radio" name="img-device" value="gpu" onchange="selectedDevice=this.value"> GPU
      </label>
      <label style="font-size:0.85rem;cursor:pointer">
        <input type="radio" name="img-device" value="both" onchange="selectedDevice=this.value"> Both ⚡
      </label>
    </div>

    <button id="run-btn" onclick="startMeasurement()">Generate &amp; Measure</button>
    <div id="status"></div>
    {prev_html}
    </div>

<script>
const CPU_STAGES = ['baseline','generating','done'];
const GPU_STAGES = ['baseline','generating','done'];
const BOTH_STAGES = ['cpu_baseline','cpu_generating','cooldown','gpu_baseline','gpu_generating','done'];
const STAGE_LABELS = {{
  'baseline': 'Measuring baseline power',
  'generating': 'Generating image',
  'cpu_baseline': 'CPU — measuring baseline',
  'cpu_generating': 'CPU — generating image',
  'cooldown': 'Cooldown between passes',
  'gpu_baseline': 'GPU — measuring baseline',
  'gpu_generating': 'GPU — generating images (batch)',
  'done': 'Complete',
}};
let pollTimer = null;
let selectedDevice = 'cpu';
let imgStartTime = null;

function fmt(v, dp=2) {{
  if (v === null || v === undefined) return '—';
  return Number(v).toFixed(dp);
}}

async function startMeasurement() {{
  const prompt = document.getElementById('prompt').value.trim();
  if (!prompt) {{ alert('Enter a prompt'); return; }}

  document.getElementById('run-btn').disabled = true;
  document.getElementById('status').innerHTML = '';

  const resp = await fetch('/image/start', {{
    method: 'POST',
    headers: {{'Content-Type':'application/x-www-form-urlencoded'}},
    body: 'prompt=' + encodeURIComponent(prompt) + '&device=' + encodeURIComponent(selectedDevice)
  }});
  const data = await resp.json();
  if (data.error) {{ alert(data.error); document.getElementById('run-btn').disabled=false; return; }}
  const jobId = data.job_id;

  imgStartTime = Date.now();
  renderProgress('baseline', null, null);
  pollTimer = setInterval(() => pollJob(jobId), 1500);
}}

async function pollJob(jobId) {{
  const r = await fetch('/image/job/' + jobId);
  const j = await r.json();

  if (j.stage === 'queued') {{ wlRenderQueued(j.queue_position); return; }}

  const powerR = await fetch('/power');
  const powerJ = await powerR.json().catch(() => ({{}}));
  renderProgress(j.stage, j.result, powerJ.watts ?? null);

  if (j.stage === 'done' && j.result) {{
    clearInterval(pollTimer);
    if (j.result.mode === 'both') renderImageBoth(j.result);
    else renderResult(j.result);
    document.getElementById('run-btn').disabled = false;
  }}
  if (j.error) {{
    clearInterval(pollTimer);
    document.getElementById('status').innerHTML =
      '<p style="color:#ff4400">Error: ' + j.error + '</p>';
    document.getElementById('run-btn').disabled = false;
  }}
}}

function renderProgress(stage, result, watts) {{
  const isBoth = BOTH_STAGES.includes(stage) && stage !== 'done';
  const stageKeys = isBoth ? BOTH_STAGES : CPU_STAGES;
  const stageIdx = stageKeys.indexOf(stage);
  wlRenderProgress({{
    header: '\u26a1 Measuring\u2026 do not close this tab',
    stagesHtml: wlStageList(stageKeys.map(s => STAGE_LABELS[s] || s), stageIdx),
    watts: watts,
    elapsed: imgStartTime ? Date.now() - imgStartTime : null,
  }});
}}

function _imageCard(label, pass_r, isWinner) {{
  const e = pass_r.energy;
  const gen = pass_r.generation;
  const borderCol = isWinner ? '#00ff9966' : '#222';
  const imgHtml = gen.b64_png
    ? `<div style="margin-top:0.75rem"><img src="data:image/png;base64,${{gen.b64_png}}" style="max-width:180px;border:1px solid #222"></div>`
    : '';
  return `<div style="border:1px solid ${{borderCol}};padding:1rem;flex:1;min-width:220px">
    <div style="color:${{isWinner?'#00ff99':'#777'}};font-size:0.85rem;font-weight:bold;margin-bottom:0.75rem">${{label}}${{isWinner?' 🏆':''}}</div>
    <div class="kpis">
      <div class="kpi"><div class="val" style="font-size:1.15rem">${{fmt(e.wh_per_image,4)}} Wh</div><div class="lbl">per image</div></div>
      <div class="kpi"><div class="val" style="font-size:1.15rem">${{fmt(gen.gen_s_per_image,1)}} s</div><div class="lbl">gen/image</div></div>
      <div class="kpi"><div class="val" style="font-size:1.1rem">${{fmt(e.delta_w,1)}} W</div><div class="lbl">delta W</div></div>
      <div class="kpi"><div class="val" style="font-size:1.1rem">${{e.poll_count}}</div><div class="lbl">polls</div></div>
    </div>
    <div style="font-size:0.78rem;color:#555;margin-top:0.5rem">${{e.confidence.flag}} ${{e.confidence.label}} · ${{gen.batch_size}}×${{gen.steps}} steps</div>
    ${{imgHtml}}
  </div>`;
}}

function renderImageBoth(r) {{
  const a = r.analysis;
  const cpuWinsEnergy = a.energy_winner === 'cpu';
  const gpuWinsEnergy = a.energy_winner === 'gpu';
  document.getElementById('status').innerHTML = `
    <div class="result-box">
      <h2>CPU vs GPU — Image Generation</h2>
      <div style="background:#111;border:1px solid #333;padding:0.75rem 1rem;margin-bottom:1.25rem;font-size:0.85rem;color:#ccc">
        ${{a.finding}}
      </div>
      <div style="display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:1rem">
        ${{_imageCard('CPU · Ryzen 9 7900', r.cpu, cpuWinsEnergy)}}
        ${{_imageCard('GPU · RX 7800 XT', r.gpu, gpuWinsEnergy)}}
      </div>
      <div style="font-size:0.75rem;color:#444;margin-top:0.5rem">
        Prompt: "${{r.full_prompt}}" · modifier: <em>${{r.modifier}}</em>
      </div>
      <p class="scope-note">${{r.scope}}</p>
    </div>`;
}}

function renderResult(r) {{
  const e = r.energy;
  const gen = r.generation;
  const batch = gen.batch_size || 1;
  const batchNote = batch > 1 ? ` (batch of ${{batch}})` : '';
  const imgHtml = gen.b64_png
    ? `<div class="image-preview">
         <img src="data:image/png;base64,${{gen.b64_png}}" alt="Generated image">
         <div class="image-caption">"${{r.full_prompt}}"</div>
       </div>`
    : '';
  document.getElementById('status').innerHTML = `
    <div class="result-box">
      <h2>Result</h2>
      <div class="kpis">
        <div class="kpi">
          <div class="val">${{fmt(e.wh_per_image,4)}} Wh</div>
          <div class="lbl">energy / image</div>
        </div>
        <div class="kpi">
          <div class="val">${{fmt(e.delta_w,1)}} W</div>
          <div class="lbl">delta above idle</div>
        </div>
        <div class="kpi">
          <div class="val">${{fmt(gen.gen_s_per_image,1)}} s</div>
          <div class="lbl">gen time / image${{batchNote}}</div>
        </div>
        <div class="kpi">
          <div class="val">${{fmt(gen.load_s,1)}} s</div>
          <div class="lbl">model load</div>
        </div>
        <div class="kpi">
          <div class="val">${{e.poll_count}}</div>
          <div class="lbl">P110 polls</div>
        </div>
      </div>
      <div class="conf-badge">${{e.confidence.flag}} ${{e.confidence.label}}</div>
      ${{imgHtml}}
      <div class="modifier-note" style="color:#444;font-size:0.75rem;margin-top:0.75rem">
        Modifier applied this run: "<em>${{r.modifier}}</em>"
      </div>
      <p class="scope-note">${{r.scope}}</p>
    </div>`;
}}
const _resumeJob = new URLSearchParams(location.search).get('job');
if (_resumeJob) {{ document.getElementById('run-btn').disabled = true; pollTimer = setInterval(() => pollJob(_resumeJob), 1500); }}
</script>
    {_PROGRESS_JS}
    {_CONF_HELP_WIDGET}
    {_FOOTER}
</body>
</html>"""


@app.post("/image/start")
async def image_start(prompt: str = Form(...), device: str = Form("cpu")):
    if device not in ("cpu", "gpu", "both"):
        device = "cpu"
    job_id = uuid.uuid4().hex[:8]
    label = f"Image ({device.upper()}) — {prompt[:35]}"

    async def coro():
        try:
            if device == "both":
                result = await run_image_both_measurement(prompt, job_id, jobs)
            else:
                result = await run_image_measurement(prompt, job_id, jobs, device=device)
            save_result("image", job_id, result)
            jobs[job_id]["result"] = result
        except Exception as e:
            jobs[job_id]["error"] = str(e)
            LOCK_FILE.unlink(missing_ok=True)

    position = enqueue(job_id, "image", label, coro)
    if position is None:
        return JSONResponse({"error": "Queue full — try again later."}, status_code=429)
    return {"job_id": job_id, "queue_position": position}


@app.get("/queue-status", response_class=HTMLResponse)
async def queue_page():
    return """<!DOCTYPE html>
<html>
<head>
    <title>WattLab — Queue</title>
    <meta http-equiv="refresh" content="4">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: monospace; background: #0a0a0a; color: #e0e0e0;
               max-width: 620px; margin: 0 auto; padding: 2rem; }
        h1 { color: #00ff99; font-size: 1.3rem; margin-bottom: 0.25rem; }
        .sub { color: #444; font-size: 0.78rem; margin-bottom: 2rem; }
        .empty { color: #333; font-size: 0.85rem; padding: 1.5rem 0; }
        .card { border: 1px solid #222; padding: 1rem 1.25rem; margin-bottom: 0.75rem; }
        .card.running { border-color: #00ff9966; }
        .card.waiting { border-color: #333; }
        .badge { display: inline-block; font-size: 0.7rem; padding: 0.15rem 0.5rem;
                 margin-bottom: 0.5rem; }
        .badge.run { background: #00ff9922; color: #00ff99; }
        .badge.wait { background: #22222299; color: #555; }
        .label { font-size: 0.9rem; color: #ccc; margin-bottom: 0.25rem; }
        .stage { font-size: 0.75rem; color: #555; }
        .back { color: #444; font-size: 0.78rem; text-decoration: none;
                display: block; margin-bottom: 1.5rem; }
        .back:hover { color: #00ff99; }
        .depth { font-size: 2.5rem; color: #00ff99; font-weight: bold; }
        .depth-lbl { color: #444; font-size: 0.75rem; margin-bottom: 2rem; }
    </style>
</head>
<body>
    <a href="/" style="color:#555;text-decoration:none;font-size:0.82rem;display:block;margin-bottom:1.5rem">← Home</a>
    <h1>Queue</h1>
    <div class="sub">Auto-refreshes every 4s</div>
    <div id="content"><p class="empty">Loading…</p></div>
<script>
function resumeLink(type, jobId) {
    if (!type || !jobId) return '';
    return ' <a href="/' + type + '?job=' + jobId + '" style="color:#00ff99;font-size:0.75rem;' +
           'text-decoration:none;margin-left:0.75rem">↩ Resume</a>';
}
async function load() {
    const r = await fetch('/queue');
    const q = await r.json();
    const el = document.getElementById('content');
    if (q.depth === 0) {
        el.innerHTML = '<div class="depth">0</div><div class="depth-lbl">jobs in queue — GoS1 is idle</div>';
        return;
    }
    let html = '<div class="depth">' + q.depth + '</div>' +
               '<div class="depth-lbl">job' + (q.depth !== 1 ? 's' : '') + ' in queue</div>';
    if (q.running) {
        html += '<div class="card running">' +
                '<span class="badge run">▶ RUNNING</span>' +
                resumeLink(q.running.type, q.running.job_id) +
                '<div class="label">' + (q.running.label || q.running.job_id) + '</div>' +
                '<div class="stage">stage: ' + (q.running.stage || '…') + '</div></div>';
    }
    (q.pending || []).forEach((j, i) => {
        html += '<div class="card waiting">' +
                '<span class="badge wait"># ' + j.position + '</span>' +
                resumeLink(j.type, j.job_id) +
                '<div class="label">' + j.label + '</div>' +
                '<div class="stage">waiting</div></div>';
    });
    el.innerHTML = html;
}
load();
</script>
""" + _FOOTER + """
</body>
</html>"""


@app.get("/demo", response_class=HTMLResponse)
async def demo_page():
    return _DEMO_HTML
