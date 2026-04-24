import asyncio
import io
import ipaddress
import json
import uuid
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from dotenv import dotenv_values
from power import get_power_watts
from video import run_video_measurement, run_both_measurement, run_all_measurement, run_video_measurement_path, run_both_measurement_path, UPLOAD_DIR, LOCK_FILE
from sources import get_all_sources, PRELOADED
from llm import run_llm_measurement, run_llm_batch_measurement, run_llm_both_measurement, MODELS, TASKS
from persist import save_result, list_results, load_result, to_csv
from image_gen import (run_image_measurement, run_image_both_measurement,
                        run_image_compare_models_measurement, IMAGE_MODELS,
                        IMAGE_STEPS_CPU, IMAGE_STEPS_GPU, GPU_BATCH_SIZE)
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
  <link rel="icon" type="image/png" href="https://static.wixstatic.com/media/b1006e_f5e9aff607cf4133abf7089207dc3cab~mv2.png">
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

# --- Power cache ---
# Single background loop updates this every 5s. All /power reads come from here,
# so multiple browser sessions don't each independently hammer the P110.
_power_cache: dict = {"watts": None}

async def power_poller():
    while True:
        try:
            _power_cache["watts"] = await get_power_watts()
        except Exception:
            pass  # keep stale value on transient errors
        await asyncio.sleep(5)

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
    asyncio.create_task(power_poller())
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
_QUEUE_BADGE = (
    '<div id="gos-qbadge" style="position:fixed;bottom:1rem;right:1rem;'
    'font-family:monospace;font-size:0.72rem;background:#111;border:1px solid #1a1a1a;'
    'padding:0.3rem 0.6rem">'
    '<a href="/queue-status" style="color:#555;text-decoration:none">'
    '<span id="gos-qw">— W</span><span id="gos-qd"></span></a></div>'
    '<script>(function(){'
    'async function qp(){'
    'try{'
    'var q=await(await fetch("/queue")).json();'
    'var p=await(await fetch("/power")).json();'
    'var wd=document.getElementById("gos-qw");'
    'var qd=document.getElementById("gos-qd");'
    'if(wd)wd.textContent=p.watts.toFixed(1)+" W";'
    'if(qd)qd.textContent=q.depth>0?" · ⏱ "+q.depth+(q.depth===1?" job":" jobs"):"";'
    '}catch(e){}'
    'setTimeout(qp,10000);}'
    'qp();})();' + '</script>'
)
_FOOTER = f'<footer style="margin-top:3rem;padding-top:1rem;border-top:1px solid #111">{_LOGO}</footer>' + _QUEUE_BADGE

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

# --- Home ---

@app.get("/", response_class=HTMLResponse)
async def index():
    watts = _power_cache["watts"]
    watts_str = f"{watts:.1f}" if watts is not None else "—"
    return f"""<!DOCTYPE html>
<html>
<head>
    <link rel="icon" type="image/png" href="https://static.wixstatic.com/media/b1006e_f5e9aff607cf4133abf7089207dc3cab~mv2.png">
  <title>WattLab — GoS</title>
    <meta http-equiv="refresh" content="10">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: monospace; background: #0a0a0a; color: #e0e0e0;
               display: flex; flex-direction: column; align-items: center;
               justify-content: center; min-height: 100vh; padding: 2rem 1rem; }}
        .watts {{ font-size: 6rem; color: #00ff99; font-weight: bold; }}
        .label {{ font-size: 1.2rem; color: #888; margin-top: 1rem; }}
        .scope {{ font-size: 0.8rem; color: #444; margin-top: 0.5rem; }}
        .nav {{ margin-top: 3rem; display: flex; flex-direction: column; align-items: center;
                gap: 1.25rem; width: 100%; max-width: 600px; }}
        .nav-label {{ font-size: 0.65rem; color: #333; letter-spacing: 0.1em;
                      text-transform: uppercase; margin-bottom: -0.5rem; }}
        .nav-tour a {{ color: #0a0a0a; background: #00ff99; text-decoration: none;
                       padding: 0.6rem 2.5rem; font-size: 1rem; font-weight: bold;
                       display: inline-block; }}
        .nav-tour a:hover {{ background: #00cc77; }}
        .nav-video a {{ color: #00ff99; text-decoration: none;
                        border: 1px solid #00ff99; padding: 0.55rem 2rem;
                        font-size: 1rem; display: inline-block; }}
        .nav-video a:hover {{ background: #00ff9922; }}
        .nav-ai {{ display: flex; gap: 0.6rem; flex-wrap: wrap; justify-content: center; }}
        .nav-ai a {{ color: #888; text-decoration: none;
                     border: 1px solid #2a2a2a; padding: 0.4rem 1rem;
                     font-size: 0.85rem; }}
        .nav-ai a:hover {{ color: #ccc; border-color: #444; }}
        .nav-util {{ display: flex; gap: 0.5rem; flex-wrap: wrap; justify-content: center; }}
        .nav-util a {{ color: #444; text-decoration: none;
                       border: 1px solid #1a1a1a; padding: 0.3rem 0.75rem;
                       font-size: 0.75rem; }}
        .nav-util a:hover {{ color: #777; border-color: #333; }}
    </style>
</head>
<body>
    <div class="watts">{watts_str} W</div>
    <div class="label">GoS1 current power draw</div>
    <div class="scope">Device layer only · Tapo P110 · refreshes every 10s</div>
    <div class="nav">
        <div class="nav-tour"><a href="/demo">◆ Guided Tour</a></div>
        <div class="nav-video"><a href="/video">▶ Video transcode</a></div>
        <div class="nav-label">AI workloads</div>
        <div class="nav-ai">
            <a href="/image">Image generation</a>
            <a href="/llm">LLM inference</a>
            <a href="/rag">RAG energy test</a>
        </div>
        <div class="nav-util">
            <a href="/queue-status">⏱ Queue</a>
            <a href="/settings">⚙ Settings</a>
            <a href="/methodology">📐 Methodology</a>
        </div>
    </div>
    {_FOOTER}
</body>
</html>"""

@app.get("/power")
async def power_json():
    return {"watts": _power_cache["watts"], "scope": "device_only", "source": "tapo_p110"}

# --- Video page ---

@app.get("/video", response_class=HTMLResponse)
async def video_page(request: Request):
    is_lan = _is_local(request)
    queue_depth = len(pending_queue) + (1 if current_job_id else 0)
    busy_banner = (f'<div style="background:#333;color:#ffaa00;padding:0.75rem 1rem;'
                   f'margin-bottom:1rem;font-size:0.85rem">'
                   f'⏱ {queue_depth} job{"s" if queue_depth != 1 else ""} in queue — '
                   f'yours will be added and run automatically.</div>') \
        if queue_depth > 0 else ""

    return f"""<!DOCTYPE html>
<html>
<head>
    <link rel="icon" type="image/png" href="https://static.wixstatic.com/media/b1006e_f5e9aff607cf4133abf7089207dc3cab~mv2.png">
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
        .pdesc {{ margin-top: 0.4rem; }}
        .pdesc summary {{ color: #444; font-size: 0.7rem; cursor: pointer; list-style: none; }}
        .pdesc summary::-webkit-details-marker {{ display: none; }}
        .pdesc summary::before {{ content: '▸ '; }}
        details[open].pdesc summary::before {{ content: '▾ '; }}
        .pdesc[open] {{ color: #555; font-size: 0.72rem; line-height: 1.5; padding-top: 0.3rem; }}
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

    <div style="margin-bottom:1rem;font-size:0.78rem;color:#555">
        First time here? <a href="/demo" style="color:#00ff99;text-decoration:none">Try the Guided Tour →</a>
    </div>

    <details style="margin-bottom:1.5rem;border-left:2px solid #222;padding-left:1rem">
        <summary style="cursor:pointer;color:#888;font-size:0.82rem;list-style:none;outline:none">
            ⓘ About this test <span style="color:#444;font-size:0.72rem">(click to expand)</span>
        </summary>
        <div style="color:#777;font-size:0.82rem;line-height:1.6;margin-top:0.75rem">
            Transcode a source video and measure the server's wall-power draw during the encode.<br>
            Accepted: MP4, MOV, MKV, AVI, WebM, TS · Max 1GB.<br>
            Baseline measured 10s before each run · P110 + thermals at 1s intervals.<br>
            All GPU presets use the full VAAPI pipeline (hardware decode + encode + scale) — representative of live encoding workflows.<br>
            Rate control is ABR (constant bitrate target) across all 6 presets so CPU and GPU receive identical tasks.<br>
            Scope: device layer only — network, CDN, and client devices (CPE) excluded.
        </div>
    </details>

    <div style="color:#555;font-size:0.75rem;text-transform:uppercase;
                letter-spacing:0.05em;margin-bottom:0.5rem">H.264</div>
    <div class="presets" style="margin-bottom:0.75rem">
        <div class="preset" id="preset-cpu" onclick="selectPreset('cpu')">
            <h3>H.264 CPU</h3>
            <p class="pspec">libx264 · ABR · 1080p</p>
            <details class="pdesc"><summary>details</summary>Software encode across all 24 cores.</details>
        </div>
        <div class="preset" id="preset-gpu" onclick="selectPreset('gpu')">
            <h3>H.264 GPU</h3>
            <p class="pspec">h264_vaapi · ABR · 1080p · full pipeline</p>
            <details class="pdesc"><summary>details</summary>Hardware decode + encode. Full GPU pipeline — representative of live encoding.</details>
        </div>
        <div class="preset selected" id="preset-both" onclick="selectPreset('both')">
            <h3>H.264 Both</h3>
            <p class="pspec">CPU then GPU · same file</p>
            <details class="pdesc"><summary>details</summary>Side-by-side energy + thermal report with analysis.</details>
        </div>
    </div>
    <div style="color:#555;font-size:0.75rem;text-transform:uppercase;
                letter-spacing:0.05em;margin-bottom:0.5rem">H.265</div>
    <div class="presets" style="margin-bottom:0.75rem">
        <div class="preset" id="preset-h265_cpu" onclick="selectPreset('h265_cpu')">
            <h3>H.265 CPU</h3>
            <p class="pspec">libx265 · ABR · 1080p</p>
            <details class="pdesc"><summary>details</summary>Software HEVC encode.</details>
        </div>
        <div class="preset" id="preset-h265_gpu" onclick="selectPreset('h265_gpu')">
            <h3>H.265 GPU</h3>
            <p class="pspec">hevc_vaapi · ABR · 1080p · full pipeline</p>
            <details class="pdesc"><summary>details</summary>Hardware decode + encode. Full GPU pipeline.</details>
        </div>
        <div class="preset" id="preset-h265_both" onclick="selectPreset('h265_both')">
            <h3>H.265 Both</h3>
            <p class="pspec">CPU then GPU · same file</p>
            <details class="pdesc"><summary>details</summary>Side-by-side H.265 CPU vs GPU comparison.</details>
        </div>
    </div>
    <div style="color:#555;font-size:0.75rem;text-transform:uppercase;
                letter-spacing:0.05em;margin-bottom:0.5rem">AV1</div>
    <div class="presets" style="margin-bottom:1.5rem">
        <div class="preset" id="preset-av1_cpu" onclick="selectPreset('av1_cpu')">
            <h3>AV1 CPU</h3>
            <p class="pspec">libsvtav1 · ABR · 1080p</p>
            <details class="pdesc"><summary>details</summary>SVT-AV1 software encode.</details>
        </div>
        <div class="preset" id="preset-av1_gpu" onclick="selectPreset('av1_gpu')">
            <h3>AV1 GPU</h3>
            <p class="pspec">av1_vaapi · ABR · 1080p · full pipeline</p>
            <details class="pdesc"><summary>details</summary>Hardware decode + AV1 encode. RDNA3 AV1 engine.</details>
        </div>
        <div class="preset" id="preset-av1_both" onclick="selectPreset('av1_both')">
            <h3>AV1 Both</h3>
            <p class="pspec">CPU then GPU · same file</p>
            <details class="pdesc"><summary>details</summary>Side-by-side AV1 CPU vs GPU comparison.</details>
        </div>
    </div>

    <div style="border:1px solid #00ff9933;padding:0.9rem 1rem;margin-bottom:1.5rem;
                display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.5rem"
         id="preset-all_codecs" onclick="selectPreset('all_codecs')"
         style="cursor:pointer">
        <div>
            <div style="color:#00ff99;font-size:0.9rem;font-weight:bold">Compare all codecs</div>
            <div style="color:#555;font-size:0.75rem;margin-top:0.2rem">H.264 · H.265 · AV1 · CPU + GPU · same source · same target bitrate — full matrix</div>
        </div>
        <div style="color:#444;font-size:0.75rem">~6× longer · locks queue</div>
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
                    <div style="color:#555;font-size:0.75rem">MP4, MOV, MKV, AVI, WebM, TS · Max 1GB</div>
                </div>
            </label>
            <label style="display:flex;align-items:flex-start;gap:0.75rem;
                          border:1px solid #333;padding:0.75rem;cursor:pointer">
                <input type="radio" name="source" value="meridian_120s"
                       onchange="selectSource('meridian_120s')"
                       style="margin-top:0.2rem;accent-color:#00ff99">
                <div>
                    <div style="color:#e0e0e0;font-size:0.85rem">Meridian 4K — 2 min extract</div>
                    <div style="color:#555;font-size:0.75rem">
                        3840×2160 · H.264 · 2min · ~200MB · fast demo · CC BY 4.0
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
                    <div style="color:#e0e0e0;font-size:0.85rem">Meridian 4K — full 12 min</div>
                    <div style="color:#555;font-size:0.75rem">
                        3840×2160 · 59.94fps · H.264 · 12min · 812MB · CC BY 4.0 · ⚠ Both mode ~6-8 min
                    </div>
                </div>
            </label>
        </div>
    </div>

    <div id="cmd-preview-area" style="margin-bottom:1.5rem;display:none">
        <div style="color:#555;font-size:0.75rem;text-transform:uppercase;
                    letter-spacing:0.05em;margin-bottom:0.5rem">ffmpeg command</div>
        <div id="cmd-preview-box"></div>
    </div>

    <div id="upload-area">
        <input type="file" id="fileInput" accept=".mp4,.mov,.mkv,.avi,.webm,.ts">
    </div>
    <button id="runBtn" onclick="uploadAndRun()">Upload & Measure</button>

    <div id="status"></div>
    <div id="prev-runs" style="margin-top:2rem;border-top:1px solid #111;padding-top:1.5rem"></div>

    <script>
    const IS_LAN = {'true' if is_lan else 'false'};
    let selectedPreset = 'both';
    let selectedSource = 'upload';
    let customCmds = {{}};   // {{single: str}} or {{cpu: str, gpu: str}}

    function selectSource(src) {{
        selectedSource = src;
        document.getElementById('upload-area').style.display =
            src === 'upload' ? 'block' : 'none';
        document.getElementById('runBtn').textContent =
            src === 'upload' ? 'Upload & Measure' : 'Run Measurement';
    }}

    function _cmdBox(id, value, forceReadonly=false) {{
        if (IS_LAN && !forceReadonly) {{
            return '<textarea id="' + id + '" rows="3" spellcheck="false" '
                + 'style="width:100%;background:#0d0d0d;border:1px solid #2a2a2a;'
                + 'color:#aaa;font-family:monospace;font-size:0.72rem;'
                + 'padding:0.5rem;resize:vertical;line-height:1.5">'
                + value + '</textarea>';
        }} else {{
            return '<div style="background:#0d0d0d;border:1px solid #1a1a1a;'
                + 'padding:0.5rem;font-family:monospace;font-size:0.72rem;'
                + 'color:#555;word-break:break-all;line-height:1.5">' + value + '</div>';
        }}
    }}

    async function fetchCmdPreview(preset) {{
        const area = document.getElementById('cmd-preview-area');
        const box  = document.getElementById('cmd-preview-box');
        try {{
            const resp = await fetch('/video/preview-cmd?preset=' + preset);
            const data = await resp.json();
            if (data.mode === 'all_codecs') {{
                customCmds = {{}};
                const labels = {{cpu:'H.264 CPU',gpu:'H.264 GPU',h265_cpu:'H.265 CPU',h265_gpu:'H.265 GPU',av1_cpu:'AV1 CPU',av1_gpu:'AV1 GPU'}};
                box.innerHTML = Object.entries(data.cmds).map(([k,v]) =>
                    '<div style="color:#444;font-size:0.7rem;margin:0.4rem 0 0.2rem">' + (labels[k]||k) + '</div>'
                    + _cmdBox('cmd_'+k, v, true)
                ).join('');
            }} else if (data.mode === 'both') {{
                customCmds = {{cpu: data.cpu_cmd, gpu: data.gpu_cmd}};
                box.innerHTML =
                    '<div style="color:#444;font-size:0.7rem;margin-bottom:0.3rem">CPU</div>'
                    + _cmdBox('cmd_cpu', data.cpu_cmd)
                    + '<div style="color:#444;font-size:0.7rem;margin:0.5rem 0 0.3rem">GPU</div>'
                    + _cmdBox('cmd_gpu', data.gpu_cmd);
            }} else {{
                customCmds = {{single: data.cmd}};
                box.innerHTML = _cmdBox('cmd_single', data.cmd);
            }}
            area.style.display = 'block';
        }} catch(e) {{
            box.innerHTML = '<div style="color:#555;font-size:0.72rem">Could not load preview</div>';
            area.style.display = 'block';
        }}
    }}

    function _getCustomCmds() {{
        if (selectedPreset === 'both') {{
            const cpu = document.getElementById('cmd_cpu');
            const gpu = document.getElementById('cmd_gpu');
            return {{
                custom_cmd_cpu: cpu ? cpu.value : '',
                custom_cmd_gpu: gpu ? gpu.value : '',
            }};
        }} else {{
            const el = document.getElementById('cmd_single');
            return {{ custom_cmd: el ? el.value : '' }};
        }}
    }}
    let progressTimer = null;
    let elapsedTimer = null;
    let startTime = null;

    const _SINGLE = ['Baseline', 'Encode', 'Done'];
    const _SINGLE_MAP = {{'starting':0, 'baseline':0, 'cpu_encode':1, 'gpu_encode':1,
                          'h265_cpu_encode':1, 'h265_gpu_encode':1, 'av1_cpu_encode':1, 'done':2}};
    const _BOTH_STAGES = ['Baseline', 'CPU encode', 'Rest', 'Baseline 2', 'GPU encode', 'Done'];
    const _BOTH_MAP = {{'starting':0, 'baseline':0, 'cpu_encode':1, 'rest':2,
                        'baseline_2':3, 'gpu_encode':4, 'done':5}};
    const _ALL_STAGES = ['H.264 CPU','Rest','H.264 GPU','Rest','H.265 CPU','Rest','H.265 GPU','Rest','AV1 CPU','Rest','AV1 GPU','Done'];
    const _ALL_MAP = {{'starting':0,
        'h264_cpu_baseline':0,'h264_cpu_encode':0,
        'h264_rest':1,
        'h264_gpu_baseline':2,'h264_gpu_encode':2,
        'h264_inter_rest':3,
        'h265_cpu_baseline':4,'h265_cpu_encode':4,
        'h265_rest':5,
        'h265_gpu_baseline':6,'h265_gpu_encode':6,
        'h265_inter_rest':7,
        'av1_cpu_baseline':8,'av1_cpu_encode':8,
        'av1_rest':9,
        'av1_gpu_baseline':10,'av1_gpu_encode':10,
        'done':11}};
    const STAGES = {{
        cpu:        _SINGLE,
        gpu:        _SINGLE,
        h265_cpu:   _SINGLE,
        h265_gpu:   _SINGLE,
        av1_cpu:    _SINGLE,
        av1_gpu:    _SINGLE,
        both:       _BOTH_STAGES,
        h265_both:  _BOTH_STAGES,
        av1_both:   _BOTH_STAGES,
        all_codecs: _ALL_STAGES,
    }};

    const STAGE_MAP = {{
        cpu:        _SINGLE_MAP,
        gpu:        _SINGLE_MAP,
        h265_cpu:   _SINGLE_MAP,
        h265_gpu:   _SINGLE_MAP,
        av1_cpu:    _SINGLE_MAP,
        av1_gpu:    _SINGLE_MAP,
        both:       _BOTH_MAP,
        h265_both:  _BOTH_MAP,
        av1_both:   _BOTH_MAP,
        all_codecs: _ALL_MAP,
    }};

    function selectPreset(key) {{
        selectedPreset = key;
        document.querySelectorAll('.preset').forEach(el => el.classList.remove('selected'));
        // also reset all_codecs highlight
        const allEl = document.getElementById('preset-all_codecs');
        if (allEl) allEl.style.borderColor = '#00ff9933';
        const el = document.getElementById('preset-' + key);
        if (el) {{
            el.classList.add('selected');
            if (key === 'all_codecs') el.style.borderColor = '#00ff99';
        }}
        fetchCmdPreview(key);
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
        let isUpload = false;
        try {{
            const cmds = _getCustomCmds();
            if (selectedSource === 'upload') {{
                isUpload = true;
                const file = document.getElementById('fileInput').files[0];
                if (!file) {{ alert('Please select a file first'); btn.disabled = false; return; }}
                if (file.size > 1024 * 1024 * 1024) {{ alert('File too large (max 1GB)'); btn.disabled = false; return; }}
                status.innerHTML = '<div style="color:#ffaa00">Uploading ' + file.name + '...</div>';
                const form = new FormData();
                form.append('file', file);
                form.append('preset', selectedPreset);
                for (const [k, v] of Object.entries(cmds)) form.append(k, v);
                resp = await fetch('/video/upload', {{ method: 'POST', body: form }});
            }} else {{
                status.innerHTML = '<div style="color:#ffaa00">Starting measurement on ' + selectedSource + '...</div>';
                const form = new FormData();
                form.append('source_key', selectedSource);
                form.append('preset', selectedPreset);
                for (const [k, v] of Object.entries(cmds)) form.append(k, v);
                resp = await fetch('/video/use-source', {{ method: 'POST', body: form }});
            }}

            let data;
            try {{
                data = await resp.json();
            }} catch(_) {{
                const hint = isUpload && resp.status === 413
                    ? ' — file too large for server (nginx limit)'
                    : '';
                status.innerHTML = '<div style="color:#ff4400">Failed (HTTP ' + resp.status + ')' + hint + '.</div>';
                btn.disabled = false;
                return;
            }}
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
        const pptNote = t.gpu_ppt_mean_w
            ? metricRow('GPU PPT mean / peak', t.gpu_ppt_mean_w + ' / ' + t.gpu_ppt_peak_w, 'W')
              + '<div style="color:#444;font-size:0.72rem;padding:0.1rem 0 0.6rem 1rem">'
              + 'GPU self-reported power (PPT). P110 ΔW above is the full system delta — includes CPU, RAM, drives.'
              + '</div>'
            : '';
        const cmdNote = r.transcode && r.transcode.ffmpeg_cmd
            ? '<details style="margin-top:0.75rem"><summary style="color:#333;font-size:0.72rem;cursor:pointer">ffmpeg command</summary>'
              + '<div style="color:#555;font-size:0.7rem;font-family:monospace;word-break:break-all;margin-top:0.4rem;padding:0.5rem;background:#0d0d0d;border:1px solid #1a1a1a">'
              + r.transcode.ffmpeg_cmd + '</div></details>'
            : '';
        return `
        <div class="single-report">
            <h2>Energy Report — ${{r.preset_label}}</h2>
            <div class="section-title">Encode</div>
            ${{metricRow('Preset', r.preset_detail)}}
            ${{metricRow('Duration', e.delta_t_s, 's')}}
            ${{metricRow('Output size', r.output_size_mb, 'MB')}}
            ${{cmdNote}}
            <div class="section-title">Power (P110)</div>
            ${{metricRow('Baseline', e.w_base, 'W')}}
            ${{metricRow('Task mean', e.w_task, 'W')}}
            ${{metricRow('Delta (ΔW)', e.delta_w, 'W')}}
            ${{metricRow('Energy (ΔE)', e.delta_e_wh, 'Wh')}}
            ${{metricRow('Polls', e.poll_count)}}
            <div class="section-title">Thermals</div>
            ${{metricRow('CPU base → peak', t.cpu_base + ' → ' + t.cpu_peak, '°C')}}
            ${{metricRow('GPU base → peak', t.gpu_base + ' → ' + t.gpu_peak, '°C')}}
            ${{pptNote}}
            <div style="margin-top:0.75rem">${{e.confidence.flag}} ${{e.confidence.label}}</div>
            ${{e.confidence.hint ? '<div style="margin-top:0.35rem;color:#888;font-size:0.72rem">' + e.confidence.hint + '</div>' : ''}}
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
            const pptNote = t.gpu_ppt_mean_w
                ? metricRow('GPU PPT mean', t.gpu_ppt_mean_w, 'W')
                  + '<div style="color:#444;font-size:0.72rem;padding:0.1rem 0 0.6rem 1rem">'
                  + 'GPU self-reported · P110 ΔW is full system delta.'
                  + '</div>'
                : '';
            const cmdNote = res.transcode && res.transcode.ffmpeg_cmd
                ? '<details style="margin-top:0.5rem"><summary style="color:#333;font-size:0.7rem;cursor:pointer">ffmpeg command</summary>'
                  + '<div style="color:#555;font-size:0.68rem;font-family:monospace;word-break:break-all;margin-top:0.3rem;padding:0.4rem;background:#0d0d0d;border:1px solid #1a1a1a">'
                  + res.transcode.ffmpeg_cmd + '</div></details>'
                : '';
            return `<div class="col">
                <h3>${{res.preset_label}}</h3>
                <div class="sub">${{res.preset_detail}}</div>
                <div class="section-title">Encode</div>
                ${{metricRow('Duration', e.delta_t_s + (isSpeedWinner ? ' 🏁' : ''), 's')}}
                ${{metricRow('Output size', res.output_size_mb, 'MB')}}
                ${{cmdNote}}
                <div class="section-title">Power (P110)</div>
                ${{metricRow('Baseline', e.w_base, 'W')}}
                ${{metricRow('Task mean', e.w_task, 'W')}}
                ${{metricRow('Peak delta', e.delta_w, 'W')}}
                ${{metricRow('Energy (ΔE)', e.delta_e_wh + (isEnergyWinner ? ' ✓' : ''), 'Wh')}}
                ${{metricRow('Polls', e.poll_count)}}
                <div class="section-title">Thermals</div>
                ${{metricRow('CPU base → peak', t.cpu_base + ' → ' + t.cpu_peak, '°C')}}
                ${{metricRow('GPU base → peak', t.gpu_base + ' → ' + t.gpu_peak, '°C')}}
                ${{pptNote}}
                <div style="margin-top:0.75rem;font-size:0.8rem">
                    ${{e.confidence.flag}} ${{e.confidence.label}}
                </div>
                ${{e.confidence.hint ? '<div style="margin-top:0.3rem;color:#888;font-size:0.7rem">' + e.confidence.hint + '</div>' : ''}}
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

    function renderAllCodecs(r) {{
        const codecs = r.codecs;
        const a = r.analysis;
        const codecOrder = [['h264','H.264'],['h265','H.265'],['av1','AV1']];
        const fmt = v => v != null ? v : '—';

        // Summary matrix table
        let tableRows = codecOrder.map(([key, label]) => {{
            const cd = codecs[key];
            if (!cd) return '';
            const ce = cd.cpu.energy, ge = cd.gpu.energy;
            const ca = cd.analysis;
            const ew = ca.energy_winner, sw = ca.speed_winner;
            const cpuWin = (ew==='CPU'?'✓':'') + (sw==='CPU'?' 🏁':'');
            const gpuWin = (ew==='GPU'?'✓':'') + (sw==='GPU'?' 🏁':'');
            return `<tr>
                <td style="color:#e0e0e0;font-weight:bold">${{label}}</td>
                <td>${{fmt(ce.delta_t_s)}}s</td>
                <td style="color:${{ew==='CPU'?'#00ff99':'#888'}}">${{fmt(ce.delta_e_wh)}} Wh ${{cpuWin}}</td>
                <td style="color:#555;font-size:0.75rem">${{fmt(cd.cpu.output_size_mb)}} MB</td>
                <td>${{fmt(ge.delta_t_s)}}s</td>
                <td style="color:${{ew==='GPU'?'#00ff99':'#888'}}">${{fmt(ge.delta_e_wh)}} Wh ${{gpuWin}}</td>
                <td style="color:#555;font-size:0.75rem">${{fmt(cd.gpu.output_size_mb)}} MB</td>
                <td style="font-size:0.78rem">${{ce.confidence.flag}} ${{ge.confidence.flag}}</td>
            </tr>`;
        }}).join('');

        const bestE = a.most_efficient;
        const bestS = a.fastest;
        const highlights = `
            <div style="display:flex;gap:1.5rem;flex-wrap:wrap;margin-top:0.75rem;font-size:0.82rem">
                <span>⚡ Most efficient: <span style="color:#00ff99">${{bestE ? bestE.label + ' (' + bestE.delta_e_wh + ' Wh)' : '—'}}</span></span>
                <span>🏁 Fastest: <span style="color:#00ff99">${{bestS ? bestS.label + ' (' + bestS.delta_t_s + 's)' : '—'}}</span></span>
            </div>`;

        // Per-codec collapsible detail
        const details = codecOrder.map(([key, label]) => {{
            const cd = codecs[key];
            if (!cd) return '';
            function miniCol(res, tag) {{
                const e = res.energy, t = res.thermals;
                return `<div style="flex:1;min-width:180px">
                    <div style="color:#888;font-size:0.72rem;margin-bottom:0.4rem">${{tag}}</div>
                    ${{metricRow('Duration', e.delta_t_s, 's')}}
                    ${{metricRow('Output size', res.output_size_mb, 'MB')}}
                    ${{metricRow('Baseline', e.w_base, 'W')}}
                    ${{metricRow('ΔW', e.delta_w, 'W')}}
                    ${{metricRow('ΔE', e.delta_e_wh, 'Wh')}}
                    ${{metricRow('Polls', e.poll_count)}}
                    ${{metricRow('CPU peak', t.cpu_peak, '°C')}}
                    ${{metricRow('GPU peak', t.gpu_peak, '°C')}}
                    <div style="margin-top:0.5rem;font-size:0.78rem">${{e.confidence.flag}} ${{e.confidence.label}}</div>
                    ${{e.confidence.hint ? '<div style="color:#888;font-size:0.7rem;margin-top:0.2rem">' + e.confidence.hint + '</div>' : ''}}
                </div>`;
            }}
            return `<details style="margin-top:0.5rem;border:1px solid #1a1a1a;padding:0.75rem">
                <summary style="color:#888;font-size:0.8rem;cursor:pointer;list-style:none">
                    <span style="color:#00ff99">${{label}}</span> — ${{cd.analysis.finding.slice(0,80)}}…
                </summary>
                <div style="display:flex;gap:1.5rem;flex-wrap:wrap;margin-top:0.75rem">
                    ${{miniCol(cd.cpu, cd.cpu.preset_label)}}
                    ${{miniCol(cd.gpu, cd.gpu.preset_label)}}
                </div>
            </details>`;
        }}).join('');

        return `
        <div class="report">
            <h2>All Codecs — Energy &amp; Speed Matrix</h2>
            <table style="width:100%;border-collapse:collapse;font-size:0.82rem;margin-bottom:0.5rem">
                <thead><tr style="color:#444;font-size:0.72rem;text-transform:uppercase;letter-spacing:0.05em">
                    <th style="text-align:left;padding:0.3rem 0.5rem 0.5rem 0">Codec</th>
                    <th style="text-align:right;padding:0.3rem 0.5rem">CPU time</th>
                    <th style="text-align:right;padding:0.3rem 0.5rem">CPU energy</th>
                    <th style="text-align:right;padding:0.3rem 0.5rem">CPU out</th>
                    <th style="text-align:right;padding:0.3rem 0.5rem">GPU time</th>
                    <th style="text-align:right;padding:0.3rem 0.5rem">GPU energy</th>
                    <th style="text-align:right;padding:0.3rem 0.5rem">GPU out</th>
                    <th style="text-align:center;padding:0.3rem 0.5rem">Conf</th>
                </tr></thead>
                <tbody style="font-family:monospace">${{tableRows}}</tbody>
            </table>
            <div style="font-size:0.7rem;color:#333;margin-bottom:0.25rem">✓ energy winner · 🏁 speed winner · CPU out / GPU out should match — confirms same bitrate target</div>
            ${{highlights}}
            <div style="margin-top:1rem;color:#555;font-size:0.75rem;text-transform:uppercase;letter-spacing:0.05em">Per-codec detail</div>
            ${{details}}
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
        }} else if (r.mode === 'all_codecs') {{
            html = renderAllCodecs(r) + links;
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
            let summary, codec;
            if (r.mode === 'both') {{
                codec = [r.cpu_preset, r.gpu_preset].filter(Boolean).join(' vs ');
                summary = `CPU ${{r.cpu_delta_e_wh}} Wh ${{r.cpu_confidence||''}} · GPU ${{r.gpu_delta_e_wh}} Wh ${{r.gpu_confidence||''}}`;
            }} else if (r.mode === 'all_codecs') {{
                codec = 'H.264 · H.265 · AV1 — all codecs';
                summary = `Best: ${{r.most_efficient||'—'}} (${{r.best_delta_e_wh||'—'}} Wh) · Fastest: ${{r.fastest||'—'}} ${{r.all_green ? '🟢' : ''}}`;
            }} else {{
                codec = r.preset || '';
                summary = `${{r.delta_e_wh}} Wh ${{r.confidence||''}}`;
            }}
            const base = '/results/video/' + r.job_id;
            return `<div style="border-bottom:1px solid #111;padding:0.6rem 0">
                <div style="display:flex;justify-content:space-between;align-items:baseline">
                    <span style="color:#e0e0e0;font-size:0.82rem">${{date}}</span>
                    <span style="color:#555;font-size:0.75rem;font-family:monospace">${{r.job_id}}</span>
                </div>
                <div style="color:#888;font-size:0.75rem;margin:0.1rem 0">${{codec}}</div>
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
    fetchCmdPreview(selectedPreset);
    const _resumeJob = new URLSearchParams(location.search).get('job');
    if (_resumeJob) {{ pollJob(_resumeJob, 'both'); }}
    </script>
    {_PROGRESS_JS}
    {_CONF_HELP_WIDGET}
    {_FOOTER}
</body>
</html>"""

# --- Job runner ---

async def run_job(job_id: str, input_path: Path, preset: str, delete_after: bool = True,
                  custom_cmd: str = None, custom_cmd_cpu: str = None, custom_cmd_gpu: str = None):
    try:
        jobs[job_id].update({"status": "running", "stage": "starting"})
        _BOTH_PRESETS = {
            "both":      ("cpu",      "gpu"),
            "h265_both": ("h265_cpu", "h265_gpu"),
            "av1_both":  ("av1_cpu",  "av1_gpu"),
        }
        if preset == "all_codecs":
            result = await run_all_measurement(input_path, job_id, jobs)
        elif preset in _BOTH_PRESETS:
            p_cpu, p_gpu = _BOTH_PRESETS[preset]
            result = await run_both_measurement(input_path, job_id, jobs,
                                                custom_cmd_cpu=custom_cmd_cpu,
                                                custom_cmd_gpu=custom_cmd_gpu,
                                                preset_cpu=p_cpu, preset_gpu=p_gpu)
        else:
            result = await run_video_measurement(input_path, job_id, preset, jobs,
                                                 custom_cmd=custom_cmd)
        save_result("video", job_id, result)
        jobs[job_id].update({"status": "done", "stage": "done", "result": result})
    except Exception as e:
        jobs[job_id] = {"status": "error", "stage": "error", "error": str(e)}
    finally:
        if delete_after:
            input_path.unlink(missing_ok=True)


@app.post("/video/use-source")
async def use_preloaded_source(
    source_key: str = Form(...),
    preset: str = Form("both"),
    custom_cmd: str = Form(None),
    custom_cmd_cpu: str = Form(None),
    custom_cmd_gpu: str = Form(None),
):
    if preset not in ("cpu", "gpu", "both", "h265_cpu", "h265_gpu", "h265_both", "av1_cpu", "av1_gpu", "av1_both", "all_codecs"):
        return JSONResponse({"error": "Invalid preset"}, status_code=400)

    source = PRELOADED.get(source_key)
    if not source or not source["path"].exists():
        return JSONResponse({"error": f"Source '{source_key}' not found"}, status_code=404)

    job_id = str(uuid.uuid4())[:8]
    label = f"Video — {preset} · {source['label']}"

    async def coro():
        await run_job(job_id, source["path"], preset, False,
                      custom_cmd=custom_cmd,
                      custom_cmd_cpu=custom_cmd_cpu,
                      custom_cmd_gpu=custom_cmd_gpu)

    position = enqueue(job_id, "video", label, coro)
    if position is None:
        return JSONResponse({"error": "Queue full — try again later."}, status_code=429)
    return {"job_id": job_id, "queue_position": position}

@app.post("/video/upload")
async def upload_video(
    file: UploadFile = File(...),
    preset: str = Form("both"),
    custom_cmd: str = Form(None),
    custom_cmd_cpu: str = Form(None),
    custom_cmd_gpu: str = Form(None),
):
    if preset not in ("cpu", "gpu", "both", "h265_cpu", "h265_gpu", "h265_both", "av1_cpu", "av1_gpu", "av1_both", "all_codecs"):
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
        await run_job(job_id, input_path, preset, True,
                      custom_cmd=custom_cmd,
                      custom_cmd_cpu=custom_cmd_cpu,
                      custom_cmd_gpu=custom_cmd_gpu)

    position = enqueue(job_id, "video", label, coro)
    if position is None:
        return JSONResponse({"error": "Queue full — try again later."}, status_code=429)
    return {"job_id": job_id, "queue_position": position}


@app.get("/video/sources")
async def video_sources():
    return get_all_sources()


@app.get("/video/preview-cmd")
async def video_preview_cmd(preset: str = "both"):
    from video import PRESETS, build_preset_cmd
    placeholder_in = Path("{input}")
    placeholder_out = Path("{output}")
    _BOTH_MAP = {"both": ("cpu","gpu"), "h265_both": ("h265_cpu","h265_gpu"), "av1_both": ("av1_cpu","av1_gpu")}
    if preset == "all_codecs":
        pairs = [("cpu","gpu"),("h265_cpu","h265_gpu"),("av1_cpu","av1_gpu")]
        cmds = {}
        for cpu_k, gpu_k in pairs:
            cmds[cpu_k] = " ".join(build_preset_cmd(cpu_k, placeholder_in, placeholder_out))
            cmds[gpu_k] = " ".join(build_preset_cmd(gpu_k, placeholder_in, placeholder_out))
        return {"mode": "all_codecs", "cmds": cmds}
    elif preset in _BOTH_MAP:
        p_cpu, p_gpu = _BOTH_MAP[preset]
        cpu_cmd = " ".join(build_preset_cmd(p_cpu, placeholder_in, placeholder_out))
        gpu_cmd = " ".join(build_preset_cmd(p_gpu, placeholder_in, placeholder_out))
        return {"mode": "both", "cpu_cmd": cpu_cmd, "gpu_cmd": gpu_cmd}
    elif preset in PRESETS:
        cmd = " ".join(build_preset_cmd(preset, placeholder_in, placeholder_out))
        return {"mode": "single", "cmd": cmd}
    else:
        return JSONResponse({"error": "Unknown preset"}, status_code=400)


@app.post("/variance/run")
async def variance_run(request: Request):
    if not _is_local(request):
        return JSONResponse({"error": "Forbidden — variance calibration is lab-only"}, status_code=403)
    from video import run_variance_calibration
    job_id = str(uuid.uuid4())[:8]
    label = "Variance calibration — system offline"

    async def coro():
        try:
            jobs[job_id].update({"status": "running", "stage": "starting"})
            result = await run_variance_calibration(job_id, jobs)
            jobs[job_id].update({"status": "done", "stage": "done", "result": result})
        except Exception as e:
            jobs[job_id] = {"status": "error", "stage": "error", "error": str(e)}

    position = enqueue(job_id, "variance", label, coro)
    if position is None:
        return JSONResponse({"error": "Queue full — try again later."}, status_code=429)
    return {"job_id": job_id, "queue_position": position}


# --- LLM job runner ---

async def run_llm_job(job_id: str, model_key: str, task_key: str,
                      repeats: int = 1, warm: bool = False, prompt: str = None,
                      device: str = "gpu"):
    try:
        jobs[job_id].update({"status": "running", "stage": "baseline", "partial_response": ""})
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
        jobs[job_id].update({"status": "done", "stage": "done", "result": result})
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
    <link rel="icon" type="image/png" href="https://static.wixstatic.com/media/b1006e_f5e9aff607cf4133abf7089207dc3cab~mv2.png">
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

    <div style="margin-bottom:1rem;font-size:0.78rem;color:#555">
        First time here? <a href="/demo" style="color:#00ff99;text-decoration:none">Try the Guided Tour →</a>
    </div>

    <details style="margin-bottom:1.5rem;border-left:2px solid #222;padding-left:1rem">
        <summary style="cursor:pointer;color:#888;font-size:0.82rem;list-style:none;outline:none">
            ⓘ About this test <span style="color:#444;font-size:0.72rem">(click to expand)</span>
        </summary>
        <div style="color:#777;font-size:0.82rem;line-height:1.6;margin-top:0.75rem">
            Run a language model on a fixed prompt and measure energy per token.<br>
            Models span small → large: TinyLlama 1.1B · Mistral 7B · Gemma 3 12B. CPU + ROCm GPU (via Ollama).<br>
            Cold mode unloads the model first; warm mode reuses a loaded model. Batch mode runs N inferences with a rest between.<br>
            Primary metric: <strong style="color:#aaa">mWh per output token</strong> · P110 polled at 1s intervals.<br>
            Scope: device layer only — no amortised training cost included.
        </div>
    </details>

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
        jobs[job_id].update({"status": "running", "stage": "baseline",
                             "current_task": "T1", "current_device": devices[0], "partial_response": ""})
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
    <link rel="icon" type="image/png" href="https://static.wixstatic.com/media/b1006e_f5e9aff607cf4133abf7089207dc3cab~mv2.png">
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

    <div style="margin-bottom:1rem;font-size:0.78rem;color:#555">
        First time here? <a href="/demo" style="color:#00ff99;text-decoration:none">Try the Guided Tour →</a>
    </div>

    <details style="margin-bottom:1.5rem;border-left:2px solid #222;padding-left:1rem">
        <summary style="cursor:pointer;color:#888;font-size:0.82rem;list-style:none;outline:none">
            ⓘ About this test <span style="color:#444;font-size:0.72rem">(click to expand)</span>
        </summary>
        <div style="color:#777;font-size:0.82rem;line-height:1.6;margin-top:0.75rem">
            Retrieval-Augmented Generation (RAG) augments an LLM with chunks retrieved from a PDF corpus (ChromaDB + sentence-transformer embeddings).<br>
            Compare three modes: <strong style="color:#aaa">baseline</strong> (no retrieval), <strong style="color:#aaa">RAG</strong> (top 3 chunks, 4096 ctx), <strong style="color:#aaa">RAG Large</strong> (top 8 chunks, 8192 ctx).<br>
            Use "Compare 3 modes" to run all three sequentially with fresh baselines — a side-by-side energy comparison for the same question.<br>
            Scope: device layer only — no network, no amortised training cost.
        </div>
    </details>

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

    def slider_field(fid, val, min_, max_, step, unit, hint=""):
        if local:
            ctrl = (f'<input type="range" id="{fid}" min="{min_}" max="{max_}" step="{step}"'
                    f' value="{val}"'
                    f' oninput="document.getElementById(\'{fid}_disp\').textContent=this.value"'
                    f' style="width:130px;accent-color:#00ff99;vertical-align:middle"> '
                    f'<span id="{fid}_disp" style="font-family:monospace;color:#00ff99;'
                    f'font-size:0.9rem;min-width:2.5rem;display:inline-block">{val}</span>')
        else:
            ctrl = f'<span style="font-family:monospace;color:#00ff99;font-size:0.95rem">{val}</span>'
        hint_html = f'<div style="color:#333;font-size:0.72rem;margin-top:0.2rem">{hint}</div>' if hint else ""
        return (f'<div style="display:flex;justify-content:space-between;align-items:center;'
                f'padding:0.5rem 0;border-bottom:1px solid #0d0d0d;gap:1rem">'
                f'<div><label style="color:#aaa;font-size:0.85rem">{fid.replace("_"," ").title()}</label>'
                f'{hint_html}</div>'
                f'<div style="display:flex;align-items:center;gap:0.5rem">'
                f'{ctrl}<span style="color:#555;font-size:0.8rem">{unit}</span>'
                f'</div></div>')

    def textarea_field(fid, val, hint="", rows=3):
        if local:
            ctrl = (f'<textarea id="{fid}" rows="{rows}" spellcheck="false"'
                    f' style="width:100%;background:#0d0d0d;border:1px solid #222;'
                    f'color:#888;font-family:monospace;font-size:0.72rem;'
                    f'padding:0.4rem 0.5rem;resize:vertical;line-height:1.5">{val}</textarea>')
        else:
            ctrl = (f'<div style="background:#0d0d0d;border:1px solid #1a1a1a;padding:0.4rem 0.5rem;'
                    f'font-family:monospace;font-size:0.72rem;color:#444;word-break:break-all;'
                    f'line-height:1.5">{val}</div>')
        hint_html = f'<div style="color:#333;font-size:0.72rem;margin-top:0.2rem;margin-bottom:0.3rem">{hint}</div>' if hint else ""
        return (f'<div style="padding:0.5rem 0;border-bottom:1px solid #0d0d0d">'
                f'<label style="color:#aaa;font-size:0.85rem">{fid.replace("_"," ").title()}</label>'
                f'{hint_html}{ctrl}</div>')

    def calib_field(fid, val, hint=""):
        disp = f"{val:.2f} %" if val is not None else "—"
        color = "#00ff99" if val is not None else "#333"
        hint_html = f'<div style="color:#333;font-size:0.72rem;margin-top:0.1rem">{hint}</div>' if hint else ""
        return (f'<div style="display:flex;justify-content:space-between;align-items:baseline;'
                f'padding:0.5rem 0;border-bottom:1px solid #0d0d0d;gap:1rem">'
                f'<div><label style="color:#666;font-size:0.85rem">{fid.replace("_"," ").title()}</label>'
                f'{hint_html}</div>'
                f'<div style="display:flex;align-items:baseline;gap:0.5rem">'
                f'<span style="font-family:monospace;color:{color};font-size:0.95rem">{disp}</span>'
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
    <link rel="icon" type="image/png" href="https://static.wixstatic.com/media/b1006e_f5e9aff607cf4133abf7089207dc3cab~mv2.png">
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

    <div class="section">Encoding targets</div>
    <div style="color:#444;font-size:0.75rem;line-height:1.6;margin-bottom:0.75rem">
      ABR target bitrate applied to both CPU and GPU presets for each codec — ensures apples-to-apples energy comparison. Custom ffmpeg commands on the video page override these.
    </div>
    {field("h264_bitrate_kbps", s['h264_bitrate_kbps'], 500, 20000, "kbps", "H.264 target bitrate (libx264 + h264_vaapi)", step=100)}
    {field("h265_bitrate_kbps", s['h265_bitrate_kbps'], 500, 20000, "kbps", "H.265 target bitrate (libx265 + hevc_vaapi)", step=100)}
    {field("av1_bitrate_kbps",  s['av1_bitrate_kbps'],  500, 20000, "kbps", "AV1 target bitrate (libsvtav1 + av1_vaapi)", step=100)}

    <div class="section">Confidence thresholds</div>
    {calib_field("variance_idle_pct", s['variance_idle_pct'], "CV of raw idle P110 readings — set by calibration")}
    {calib_field("variance_cpu_pct",  s['variance_cpu_pct'],  "CV of ΔW across H264-CPU runs — set by calibration")}
    {calib_field("variance_gpu_pct",  s['variance_gpu_pct'],  "CV of ΔW across H265-GPU runs — set by calibration")}
    {field("variance_pct",     s['variance_pct'],     0, 50,  "%",     "composite variance (mean of above) — editable override", step=0.1)}
    {field("variance_green_x", s['variance_green_x'], 1, 20,  "× noise", "🟢 ΔW must exceed this multiple of noise floor", step=0.5)}
    {field("variance_yellow_x",s['variance_yellow_x'],1, 10,  "× noise", "🟡 ΔW must exceed this multiple of noise floor", step=0.5)}
    {field("conf_green_polls", s['conf_green_polls'],  1, 100, "polls", "🟢 minimum poll count")}
    {field("conf_yellow_polls",s['conf_yellow_polls'], 1, 50,  "polls", "🟡 minimum poll count")}

    <div class="section">Variance calibration</div>
    <div style="color:#444;font-size:0.75rem;line-height:1.6;margin-bottom:0.75rem">
      Runs H.264 CPU then H.265 GPU on Meridian N times. Computes three coefficients of
      variation: idle (raw P110 baseline readings), CPU (ΔW per H264 run), GPU (ΔW per H265 run).
      Their mean is written to Variance % above. Queue is blocked for the duration.
    </div>
    {slider_field("variance_runs",      s['variance_runs'],      2,  100, 1,  "runs",    "number of H264-CPU + H265-GPU run pairs")}
    {slider_field("variance_cooldown_s",s['variance_cooldown_s'],10, 300, 10, "s",       "cooldown between each run pair")}
    {textarea_field("variance_cpu_cmd", s['variance_cpu_cmd'], "H.264 CPU command — {input} and {output} are substituted at runtime")}
    {textarea_field("variance_gpu_cmd", s['variance_gpu_cmd'], "H.265 GPU command — {input} and {output} are substituted at runtime")}
    {'<button onclick="runVarianceCalibration()" id="varCalBtn" style="background:#222;color:#00ff99;border:1px solid #00ff9944;padding:0.5rem 1.25rem;cursor:pointer;font-family:monospace;font-size:0.85rem;margin-top:0.75rem">▶ Run variance calibration</button><div id="var-cal-msg" style="margin-top:0.5rem;font-size:0.82rem"></div>' if local else '<div style="color:#333;font-size:0.78rem;margin-top:0.5rem">Calibration requires lab access.</div>'}

    {save_block}
    <script>
    async function saveSettings() {{
        const num_fields = ['baseline_polls','video_cooldown_s','llm_rest_s','llm_unload_settle_s',
                            'h264_bitrate_kbps','h265_bitrate_kbps','av1_bitrate_kbps',
                            'variance_pct','variance_green_x','variance_yellow_x',
                            'conf_green_polls','conf_yellow_polls',
                            'variance_runs','variance_cooldown_s'];
        const str_fields = ['variance_cpu_cmd','variance_gpu_cmd'];
        const body = {{}};
        for (const f of num_fields) {{
            const el = document.getElementById(f);
            if (el) body[f] = parseFloat(el.value);
        }}
        for (const f of str_fields) {{
            const el = document.getElementById(f);
            if (el) body[f] = el.value;
        }}
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

    async function runVarianceCalibration() {{
        const btn = document.getElementById('varCalBtn');
        const msg = document.getElementById('var-cal-msg');
        if (!btn) return;
        btn.disabled = true;
        msg.innerHTML = '<span style="color:#ffaa00">Saving settings…</span>';
        await saveSettings();
        msg.innerHTML = '<span style="color:#ffaa00">Queuing calibration job…</span>';
        try {{
            const resp = await fetch('/variance/run', {{method: 'POST'}});
            const data = await resp.json();
            if (data.job_id) {{
                msg.innerHTML = '<span style="color:#00ff99">Job ' + data.job_id
                    + ' queued (position ' + data.queue_position + '). '
                    + '<a href="/queue-status" style="color:#00ff99">View queue →</a>'
                    + '</span>';
            }} else {{
                msg.innerHTML = '<span style="color:#ff4400">Error: ' + JSON.stringify(data) + '</span>';
                btn.disabled = false;
            }}
        }} catch(e) {{
            msg.innerHTML = '<span style="color:#ff4400">Failed: ' + e + '</span>';
            btn.disabled = false;
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
<link rel="icon" type="image/png" href="https://static.wixstatic.com/media/b1006e_f5e9aff607cf4133abf7089207dc3cab~mv2.png">
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
    <span class="dot" id="dot-6"></span>
    <span class="label active" id="nav-label">Welcome</span>
    <span id="step-counter" style="color:#333;font-size:0.7rem;margin-left:0.25rem">1 / 7</span>
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
      once in software (libx264, CPU only) and once as a full GPU pipeline
      (hardware decode + encode via h264_vaapi). Same source. Same quality target.
      P110 sampled every second throughout.
    </p>
    <details>
      <summary>How this is measured</summary>
      <p>10s idle baseline before each run. 60s thermal cooldown between CPU and GPU.
      Energy = ΔW × duration / 3600. Confidence 🟢 = ΔW &gt; 5W and ≥ 10 polls.</p>
      <p>Source: 812 MB, 4K. Encode time ~2–3 min CPU, ~90s GPU (full pipeline).
      Previous runs (partial pipeline): CPU 174s / 4.06 Wh · GPU 114s / 4.42 Wh.
      Full pipeline results pending first run.</p>
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
      <button class="btn btn-secondary" onclick="goStep(0)">← Welcome</button>
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
      <button class="btn btn-secondary" onclick="goStep(1)">← Video</button>
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
      <button class="btn btn-secondary" onclick="goStep(2)">← LLM</button>
      <button class="btn btn-primary" onclick="goStep(4)">Next: RAG →</button>
      <button class="btn btn-secondary" onclick="resetImageStep()">Run again</button>
    </div>
  </div>
</div>

<!-- Step 4: RAG -->
<div class="step" id="step-4">
  <h1>RAG Energy Cost</h1>

  <div class="band">
    <div class="band-label">What this shows</div>
    <p style="color:#aaa;line-height:1.8;max-width:560px">
      Whether retrieval-augmented generation (RAG) — searching a local corpus
      before answering — costs meaningfully more energy than plain inference,
      and see the difference in context size the model must process.
    </p>
  </div>

  <div class="band">
    <div class="band-label">What we're doing</div>
    <p style="color:#555;line-height:1.7;max-width:560px;margin-bottom:0.75rem">
      Running three modes back-to-back on Mistral 7B: baseline (no retrieval),
      RAG (small corpus), and RAG Large (with re-ranking).
      Same question, same model, same hardware — only the retrieval pipeline changes.
    </p>
    <details>
      <summary>How this is measured</summary>
      <p>Each mode: 10s idle baseline, inference with P110 at 1s intervals.
      Metric: mWh per output token. ChromaDB embeddings via sentence-transformers.
      Corpus: academic papers on streaming energy.</p>
    </details>
  </div>

  <div>
    <div class="band-label">Result</div>
    <div id="rag-btns" class="btn-row" style="display:none">
      <button class="btn btn-primary" onclick="runDemoRAG()">Run 3-mode comparison (~10 min)</button>
    </div>
    <div id="rag-status"></div>
    <p class="limitation">Scope: device layer only (GoS1). Network excluded.
    RAG retrieval adds overhead but the dominant cost remains token generation.</p>
  </div>

  <div id="next-4" style="display:none;margin-top:2rem;padding-top:1.5rem;border-top:1px solid #111">
    <div class="btn-row">
      <button class="btn btn-secondary" onclick="goStep(3)">← Image</button>
      <button class="btn btn-primary" onclick="goStep(5)">Next: How we flag confidence →</button>
      <button class="btn btn-secondary" onclick="resetRAGStep()">Run again</button>
    </div>
  </div>
</div>

<!-- Step 5: Confidence -->
<div class="step" id="step-5">
  <h1>How We Flag Confidence</h1>

  <div class="band">
    <div class="band-label">The problem</div>
    <p style="color:#aaa;line-height:1.8;max-width:560px">
      Not every measurement we take is equally trustworthy.
      System noise — P110 quantisation, OS jitter, Wi-Fi polling variance — is real.
      A task that adds a small delta above baseline might be signal or artefact.
      We need a principled way to say which.
    </p>
  </div>

  <div class="band">
    <div class="band-label">The system</div>
    <p style="color:#555;line-height:1.7;max-width:560px;margin-bottom:1rem">
      Every result carries a traffic light. Thresholds are <em>variance-relative</em>:
      anchored to empirically measured system noise, not fixed watt values.
      <code style="font-family:monospace;font-size:0.82rem;color:#888">noise = (variance% / 100) × W_base</code>
    </p>
    <div style="display:flex;flex-direction:column;gap:0.75rem;max-width:480px">
      <div style="border-left:2px solid #1a3a1a;padding:0.6rem 1rem">
        <div style="font-family:monospace;font-size:0.9rem">🟢 Repeatable</div>
        <div style="color:#555;font-size:0.82rem;margin-top:0.25rem">
          ΔW &gt; 5× noise <em>and</em> ≥ 10 polls. Well above noise floor. Reliable enough to cite.</div>
      </div>
      <div style="border-left:2px solid #3a3a00;padding:0.6rem 1rem">
        <div style="font-family:monospace;font-size:0.9rem">🟡 Early insight</div>
        <div style="color:#555;font-size:0.82rem;margin-top:0.25rem">
          ΔW ≥ 2× noise <em>or</em> ≥ 5 polls. Directional signal, but needs more runs
          before we'd stake a public claim on it.</div>
      </div>
      <div style="border-left:2px solid #2a0000;padding:0.6rem 1rem">
        <div style="font-family:monospace;font-size:0.9rem">🔴 Need more data</div>
        <div style="color:#555;font-size:0.82rem;margin-top:0.25rem">
          Below yellow threshold. Could be measurement artefact.
          We publish it anyway — but we won't cite it yet.</div>
      </div>
    </div>
  </div>

  <div class="band">
    <div class="band-label">Why variance-relative?</div>
    <p style="color:#555;line-height:1.7;max-width:560px;margin-bottom:0.75rem">
      Fixed thresholds (e.g. "5W = green") don't adapt to the machine's actual noise
      level. Our calibration run measures idle variance, CPU encode variance, and GPU
      encode variance separately — then sets the noise floor from real data.
      At 55W idle with 2% variance: noise ≈ 1.1W, green threshold ≈ 5.5W.
      As the system is calibrated further, these thresholds tighten automatically.
    </p>
    <p style="color:#555;line-height:1.7;max-width:560px">
      On any result page, click a 🟢 🟡 🔴 badge for a quick reminder of the formula.
    </p>
  </div>

  <div class="btn-row" style="margin-top:0.5rem">
    <button class="btn btn-secondary" onclick="goStep(4)">← RAG</button>
    <button class="btn btn-primary" onclick="goStep(6)">See findings →</button>
  </div>
</div>

<!-- Step 6: Findings -->
<div class="step" id="step-6">
  <h1>Findings</h1>
  <p style="color:#555;font-size:0.85rem;margin-bottom:1.5rem">
    Greening of Streaming · WattLab · GoS1</p>

  <div id="summary-content">
    <p style="color:#555;font-size:0.85rem">Loading results…</p>
  </div>

  <hr class="divider">
  <div class="btn-row">
    <button class="btn btn-secondary" onclick="goStep(5)">← Confidence</button>
    <button class="btn btn-secondary" onclick="goStep(1)">↺ Start over</button>
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
let ragResult = null;
const stepLabels = ['Welcome', 'Video Transcode', 'LLM Inference', 'Image Generation', 'RAG', 'Confidence', 'Findings'];
let streamTimer = null;
let imageTimer = null;

// ─── Step navigation ─────────────────────────────────────────────────────────
function goStep(n) {{
  document.querySelectorAll('.step').forEach(el => el.classList.remove('active'));
  document.getElementById('step-' + n).classList.add('active');
  for (let i = 0; i < 7; i++) {{
    const dot = document.getElementById('dot-' + i);
    dot.className = 'dot' + (i < n ? ' done' : i === n ? ' active' : '');
  }}
  const lbl = document.getElementById('nav-label');
  lbl.textContent = stepLabels[n];
  lbl.className = 'label active';
  document.getElementById('step-counter').textContent = (n + 1) + ' / 7';
  currentStep = n;
  window.scrollTo(0, 0);
  if (n === 1 && !videoResult) loadVideoStep();
  if (n === 2 && !llmResult) loadLLMStep();
  if (n === 3 && !imageResult) loadImageStep();
  if (n === 4 && !ragResult) loadRAGStep();
  if (n === 1 && videoResult) revealNext(1);
  if (n === 2 && llmResult) revealNext(2);
  if (n === 3 && imageResult) revealNext(3);
  if (n === 4 && ragResult) revealNext(4);
  if (n === 6) buildSummary();
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
function loadRAGStep() {{
  document.getElementById('rag-status').innerHTML = '<p class="progress-note" style="color:#555">Loading last result…</p>';
  showPrevRAG();
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
function resetRAGStep() {{
  ragResult = null;
  document.getElementById('rag-btns').style.display = 'flex';
  document.getElementById('rag-status').innerHTML = '';
  document.getElementById('next-4').style.display = 'none';
}}

// ─── RAG ─────────────────────────────────────────────────────────────────────
async function showPrevRAG() {{
  document.getElementById('rag-btns').style.display = 'none';
  try {{
    const resp = await fetch('/results/llm/list');
    const list = await resp.json();
    const ragRuns = (list || []).filter(r => r.task === 'RAG compare (3 modes)');
    if (!ragRuns.length) {{
      document.getElementById('rag-status').innerHTML = '';
      document.getElementById('rag-btns').style.display = 'flex';
      return;
    }}
    const r2 = await fetch('/results/llm/' + ragRuns[0].job_id + '/download.json');
    const full = await r2.json();
    ragResult = full;
    renderRAGResult(full, ragRuns[0].saved_at, true);
  }} catch(e) {{
    document.getElementById('rag-btns').style.display = 'flex';
  }}
}}

async function runDemoRAG() {{
  document.getElementById('rag-btns').style.display = 'none';
  document.getElementById('rag-status').innerHTML = '<p class="progress-note">▶ Starting RAG comparison…</p>';
  try {{
    const form = new FormData();
    form.append('model_key', 'mistral');
    form.append('question', 'How does codec choice affect streaming energy consumption?');
    const resp = await fetch('/rag/run-compare', {{method:'POST', body:form}});
    const data = await resp.json();
    if (data.job_id) pollDemoRAG(data.job_id, Date.now());
    else document.getElementById('rag-status').innerHTML =
      '<p class="progress-note" style="color:#ff4400">' + JSON.stringify(data) + '</p>';
  }} catch(e) {{
    document.getElementById('rag-status').innerHTML =
      '<p class="progress-note" style="color:#ff4400">Error: ' + e + '</p>';
    document.getElementById('rag-btns').style.display = 'flex';
  }}
}}

function pollDemoRAG(jobId, t0) {{
  const elapsed = Math.floor((Date.now()-t0)/1000);
  const m = Math.floor(elapsed/60), s = elapsed%60;
  const eStr = m > 0 ? m+'m '+s+'s' : s+'s';
  fetch('/rag/job/' + jobId).then(r=>r.json()).then(data => {{
    if (data.stage === 'done' && data.result) {{
      ragResult = data.result;
      renderRAGResult(data.result, new Date().toISOString(), false);
    }} else if (data.error) {{
      document.getElementById('rag-status').innerHTML =
        '<p class="progress-note" style="color:#ff4400">Error: ' + data.error + '</p>';
      document.getElementById('rag-btns').style.display = 'flex';
    }} else {{
      const stage = data.stage || '';
      const lbl = stage.startsWith('baseline') ? 'Measuring baseline…' :
                  stage.startsWith('inference') ? 'Running ' + stage + '…' : stage || '…';
      document.getElementById('rag-status').innerHTML =
        '<p class="progress-note">▶ ' + lbl + '</p>' +
        '<p class="dim mono" style="font-size:0.78rem;margin-top:0.4rem">Elapsed: ' + eStr + '</p>';
      setTimeout(() => pollDemoRAG(jobId, t0), 3000);
    }}
  }}).catch(() => setTimeout(() => pollDemoRAG(jobId, t0), 5000));
}}

function renderRAGResult(r, savedAt, isPrev) {{
  const prevNote = isPrev ? '<p class="prev-note">↩ Previous run · ' + timeAgo(savedAt) + '</p>' : '';
  const modes = ['baseline', 'rag', 'rag_large'];
  const labels = {{'baseline': 'No retrieval', 'rag': 'RAG', 'rag_large': 'RAG Large'}};
  const results = r.results || {{}};
  const modelLine = r.model_label
    ? `<div style="font-family:monospace;font-size:0.78rem;color:#555;margin-bottom:1rem">
         Model: ${{r.model_label}}${{r.model_params ? ' · ' + r.model_params : ''}}</div>`
    : '';
  let cols = '';
  modes.forEach(m => {{
    const res = results[m];
    if (!res) return;
    const e = res.energy || {{}}, inf = res.inference || {{}};
    const inTok = inf.input_tokens != null ? inf.input_tokens : '—';
    const outTok = inf.output_tokens != null ? inf.output_tokens : '—';
    const retMs = res.retrieval_ms > 0 ? fmt(res.retrieval_ms, 0) + ' ms retrieval' : 'no retrieval';
    cols += `<div style="flex:1;min-width:130px;border-left:2px solid #1a1a1a;padding-left:0.75rem">
      <div style="font-family:monospace;font-size:0.78rem;color:#555;margin-bottom:0.5rem">${{labels[m]}}</div>
      <div class="kpi" style="margin-bottom:0.4rem">
        <div class="val">${{fmt(e.mwh_per_token, 3)}}</div>
        <div class="lbl">mWh / token</div>
      </div>
      <div style="font-size:0.75rem;color:#444;line-height:1.8">
        ${{fmt(inf.tokens_per_sec, 1)}} tok/s<br>
        ${{inTok}} in · ${{outTok}} out tokens<br>
        ${{retMs}}<br>
        ${{e.confidence ? e.confidence.flag + ' ' + e.confidence.label : ''}}
      </div>
    </div>`;
  }});
  document.getElementById('rag-status').innerHTML = prevNote +
    `<div class="result-card">
       ${{modelLine}}
       <div style="display:flex;gap:1rem;flex-wrap:wrap">${{cols}}</div>
       <p class="scope-note" style="margin-top:1rem">
         Input tokens show how much context the model processes per mode —
         retrieval grows the prompt significantly.<br>
         Device layer only. Network excluded.
       </p>
     </div>`;
  document.getElementById('rag-btns').style.display = 'none';
  revealNext(4);
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

  // RAG
  try {{
    if (ragResult && ragResult.results) {{
      const bl = ragResult.results.baseline, rl = ragResult.results.rag_large;
      if (bl && rl) {{
        const overhead = bl.energy && rl.energy && bl.energy.mwh_per_token > 0
          ? (((rl.energy.mwh_per_token - bl.energy.mwh_per_token) / bl.energy.mwh_per_token) * 100).toFixed(1)
          : null;
        rows += `<tr><td>RAG · Baseline mWh/tok</td><td>${{fmt(bl.energy && bl.energy.mwh_per_token,3)}}</td></tr>`;
        rows += `<tr><td>RAG Large mWh/tok</td><td>${{fmt(rl.energy && rl.energy.mwh_per_token,3)}}</td></tr>`;
        if (overhead !== null) rows += `<tr><td>RAG overhead</td><td>${{overhead}}%</td></tr>`;
      }}
    }} else {{
      rows += `<tr><td>RAG</td><td style="color:#333">—</td></tr>`;
    }}
  }} catch(err) {{ rows += `<tr><td>RAG</td><td style="color:#555">error: ${{err.message}}</td></tr>`; }}

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
                model_lbl = r.get("model_label") or ""
                model_tag = f' &nbsp;·&nbsp; {model_lbl}' if model_lbl else ""
                prev_html += f"""<div class="prev-item" style="flex-direction:column;align-items:flex-start">
                  <span class="prev-meta">{date_str} &nbsp;·&nbsp; CPU vs GPU{model_tag}</span>
                  {_side_html("CPU", r.get("cpu", {}))}
                  {_side_html("GPU", r.get("gpu", {}))}
                  <div class="prev-prompt" style="color:#555;font-size:0.75rem;margin-top:0.3rem">{fp[:80]}</div>
                  <div style="margin-top:0.3rem">{downloads}</div>
                </div>"""
            elif mode == "compare_models":
                def _mdl_html(s):
                    img = (f'<img src="data:image/png;base64,{s["b64_png"]}" '
                           f'style="width:64px;height:64px;object-fit:cover;margin-right:0.5rem">'
                           if s.get("b64_png") else "")
                    conf = s.get("confidence", {})
                    lbl = s.get("model_label", "?")
                    px = s.get("size_px", "?")
                    return (f'<div style="display:flex;align-items:center;margin-top:0.4rem">'
                            f'{img}<span style="color:#555;font-size:0.78rem">'
                            f'<span style="color:#aaa">{lbl} ({px}px)</span> &nbsp;·&nbsp; '
                            f'{conf.get("flag","")} {conf.get("label","")} &nbsp;·&nbsp; '
                            f'{s.get("wh_per_image","?")} Wh/img &nbsp;·&nbsp; {s.get("delta_t_s","?")}s'
                            f'</span></div>')
                prev_html += f"""<div class="prev-item" style="flex-direction:column;align-items:flex-start">
                  <span class="prev-meta">{date_str} &nbsp;·&nbsp; Compare models (GPU)</span>
                  {_mdl_html(r.get("small", {}))}
                  {_mdl_html(r.get("large", {}))}
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
    <link rel="icon" type="image/png" href="https://static.wixstatic.com/media/b1006e_f5e9aff607cf4133abf7089207dc3cab~mv2.png">
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
    <div class="subtitle">SD-Turbo (~1B) · SDXL-Turbo (~3.5B) · 512×512 · ROCm fp16 on RX 7800 XT</div>

    <div style="margin-bottom:1rem;font-size:0.78rem;color:#555">
        First time here? <a href="/demo" style="color:#00ff99;text-decoration:none">Try the Guided Tour →</a>
    </div>

    <details style="margin-bottom:1.5rem;border-left:2px solid #222;padding-left:1rem">
        <summary style="cursor:pointer;color:#888;font-size:0.82rem;list-style:none;outline:none">
            ⓘ About this test <span style="color:#444;font-size:0.72rem">(click to expand)</span>
        </summary>
        <div style="color:#777;font-size:0.82rem;line-height:1.6;margin-top:0.75rem">
            Measures the wall-power cost of generating one AI image from text.<br>
            <strong style="color:#aaa">SD-Turbo</strong>: CPU {IMAGE_STEPS_CPU} steps (~12s) or GPU batch of {GPU_BATCH_SIZE} × {IMAGE_STEPS_GPU} steps (~10s). Note: solo-mode GPU over-samples (native is 1–4 steps) to keep runtime above the P110 polling floor.<br>
            <strong style="color:#aaa">SDXL-Turbo</strong>: GPU only, 4 steps (native), batch of 15 (~10s).<br>
            <strong style="color:#aaa">Compare Models ⚡</strong>: both run at 4 steps (native for each), 512×512, same seed — SD-Turbo batch 30, SDXL-Turbo batch 15. Model size is the only variable.<br>
            Each run appends a random colour/mood modifier — live proof the image is generated, not replayed.
        </div>
    </details>

    <label style="color:#888;font-size:0.8rem;display:block;margin-bottom:0.4rem">Model</label>
    <div id="model-row" style="display:flex;gap:0.75rem;margin-bottom:1.2rem">
      <div class="preset selected" id="mdl-sd-turbo" onclick="selectModelKey('sd-turbo')"
           style="border:1px solid #00ff99;background:#00ff9911;padding:0.75rem 1rem;
                  cursor:pointer;flex:1">
        <div style="color:#00ff99;font-size:0.85rem;font-weight:bold">SD-Turbo</div>
        <div style="color:#555;font-size:0.72rem">~1B params · 512×512 · CPU + GPU</div>
      </div>
      <div class="preset" id="mdl-sdxl-turbo" onclick="selectModelKey('sdxl-turbo')"
           style="border:1px solid #333;padding:0.75rem 1rem;cursor:pointer;flex:1">
        <div style="color:#aaa;font-size:0.85rem;font-weight:bold">SDXL-Turbo</div>
        <div style="color:#555;font-size:0.72rem">~3.5B params · 512×512 · GPU only</div>
      </div>
    </div>

    <label style="color:#888;font-size:0.8rem;display:block;margin-bottom:0.4rem">Prompt</label>
    <textarea id="prompt" rows="3">a lone wind turbine in an open landscape</textarea>
    <div style="color:#555;font-size:0.75rem;margin-bottom:1.2rem">
        A random colour/mood modifier is appended per run (e.g. "bathed in emerald light").
    </div>

    <div style="margin-bottom:1.25rem">
      <span style="color:#888;font-size:0.8rem;margin-right:1rem">Backend:</span>
      <label style="font-size:0.85rem;margin-right:1.2rem;cursor:pointer" id="lbl-cpu">
        <input type="radio" name="img-device" value="cpu" checked onchange="selectedDevice=this.value"> CPU
      </label>
      <label style="font-size:0.85rem;margin-right:1.2rem;cursor:pointer">
        <input type="radio" name="img-device" value="gpu" onchange="selectedDevice=this.value"> GPU
      </label>
      <label style="font-size:0.85rem;cursor:pointer" id="lbl-both">
        <input type="radio" name="img-device" value="both" onchange="selectedDevice=this.value"> Both ⚡
      </label>
    </div>

    <div style="display:flex;gap:0.75rem;flex-wrap:wrap">
      <button id="run-btn" onclick="startMeasurement()">Generate &amp; Measure</button>
      <button id="compare-btn" onclick="startCompareModels()"
              style="background:#0a0a0a;border:1px solid #00ff99;color:#00ff99;
                     padding:0.75rem 1.5rem;font-family:monospace;font-size:0.95rem;cursor:pointer">
        Compare Models (GPU) ⚡
      </button>
    </div>
    <div id="status"></div>
    {prev_html}
    </div>

<script>
const CPU_STAGES = ['baseline','generating','done'];
const GPU_STAGES = ['baseline','generating','done'];
const BOTH_STAGES = ['cpu_baseline','cpu_generating','cooldown','gpu_baseline','gpu_generating','done'];
const COMPARE_STAGES = ['small_baseline','small_generating','cooldown','large_baseline','large_generating','done'];
const STAGE_LABELS = {{
  'baseline': 'Measuring baseline power',
  'generating': 'Generating image',
  'cpu_baseline': 'CPU — measuring baseline',
  'cpu_generating': 'CPU — generating image',
  'cooldown': 'Cooldown between passes',
  'gpu_baseline': 'GPU — measuring baseline',
  'gpu_generating': 'GPU — generating images (batch)',
  'small_baseline': 'SD-Turbo — measuring baseline',
  'small_generating': 'SD-Turbo — generating (GPU batch)',
  'large_baseline': 'SDXL-Turbo — measuring baseline',
  'large_generating': 'SDXL-Turbo — generating (GPU batch)',
  'done': 'Complete',
}};
let pollTimer = null;
let selectedDevice = 'cpu';
let selectedModelKey = 'sd-turbo';
let imgStartTime = null;

function selectModelKey(k) {{
  selectedModelKey = k;
  const sd  = document.getElementById('mdl-sd-turbo');
  const sdxl = document.getElementById('mdl-sdxl-turbo');
  if (k === 'sd-turbo') {{
    sd.style.borderColor = '#00ff99';
    sd.style.background = '#00ff9911';
    sd.children[0].style.color = '#00ff99';
    sdxl.style.borderColor = '#333';
    sdxl.style.background = 'transparent';
    sdxl.children[0].style.color = '#aaa';
    // enable CPU / Both radios
    document.querySelector('input[name="img-device"][value="cpu"]').disabled = false;
    document.querySelector('input[name="img-device"][value="both"]').disabled = false;
    document.getElementById('lbl-cpu').style.opacity = '1';
    document.getElementById('lbl-both').style.opacity = '1';
  }} else {{
    sdxl.style.borderColor = '#00ff99';
    sdxl.style.background = '#00ff9911';
    sdxl.children[0].style.color = '#00ff99';
    sd.style.borderColor = '#333';
    sd.style.background = 'transparent';
    sd.children[0].style.color = '#aaa';
    // SDXL-Turbo is GPU only — disable CPU + Both, force GPU
    const cpuIn = document.querySelector('input[name="img-device"][value="cpu"]');
    const bothIn = document.querySelector('input[name="img-device"][value="both"]');
    cpuIn.disabled = true;
    bothIn.disabled = true;
    document.getElementById('lbl-cpu').style.opacity = '0.35';
    document.getElementById('lbl-both').style.opacity = '0.35';
    const gpuIn = document.querySelector('input[name="img-device"][value="gpu"]');
    gpuIn.checked = true;
    selectedDevice = 'gpu';
  }}
}}

function fmt(v, dp=2) {{
  if (v === null || v === undefined) return '—';
  return Number(v).toFixed(dp);
}}

async function startMeasurement() {{
  const prompt = document.getElementById('prompt').value.trim();
  if (!prompt) {{ alert('Enter a prompt'); return; }}

  document.getElementById('run-btn').disabled = true;
  document.getElementById('compare-btn').disabled = true;
  document.getElementById('status').innerHTML = '';

  const resp = await fetch('/image/start', {{
    method: 'POST',
    headers: {{'Content-Type':'application/x-www-form-urlencoded'}},
    body: 'prompt=' + encodeURIComponent(prompt)
        + '&device=' + encodeURIComponent(selectedDevice)
        + '&model_key=' + encodeURIComponent(selectedModelKey)
  }});
  const data = await resp.json();
  if (data.error) {{
    alert(data.error);
    document.getElementById('run-btn').disabled = false;
    document.getElementById('compare-btn').disabled = false;
    return;
  }}
  const jobId = data.job_id;

  imgStartTime = Date.now();
  renderProgress('baseline', null, null);
  pollTimer = setInterval(() => pollJob(jobId), 1500);
}}

async function startCompareModels() {{
  const prompt = document.getElementById('prompt').value.trim();
  if (!prompt) {{ alert('Enter a prompt'); return; }}

  document.getElementById('run-btn').disabled = true;
  document.getElementById('compare-btn').disabled = true;
  document.getElementById('status').innerHTML = '';

  const resp = await fetch('/image/start', {{
    method: 'POST',
    headers: {{'Content-Type':'application/x-www-form-urlencoded'}},
    body: 'prompt=' + encodeURIComponent(prompt)
        + '&device=compare_models'
        + '&model_key=sd-turbo'    // ignored by server for compare_models
  }});
  const data = await resp.json();
  if (data.error) {{
    alert(data.error);
    document.getElementById('run-btn').disabled = false;
    document.getElementById('compare-btn').disabled = false;
    return;
  }}
  const jobId = data.job_id;

  imgStartTime = Date.now();
  renderProgress('small_baseline', null, null);
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
    else if (j.result.mode === 'compare_models') renderCompareModels(j.result);
    else renderResult(j.result);
    document.getElementById('run-btn').disabled = false;
    document.getElementById('compare-btn').disabled = false;
  }}
  if (j.error) {{
    clearInterval(pollTimer);
    document.getElementById('status').innerHTML =
      '<p style="color:#ff4400">Error: ' + j.error + '</p>';
    document.getElementById('run-btn').disabled = false;
    document.getElementById('compare-btn').disabled = false;
  }}
}}

function renderProgress(stage, result, watts) {{
  const isCompare = COMPARE_STAGES.includes(stage) && stage !== 'done';
  const isBoth = !isCompare && BOTH_STAGES.includes(stage) && stage !== 'done';
  const stageKeys = isCompare ? COMPARE_STAGES : (isBoth ? BOTH_STAGES : CPU_STAGES);
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

function _modelCard(side_r) {{
  const e = side_r.energy;
  const gen = side_r.generation;
  const imgHtml = gen.b64_png
    ? `<div style="margin-top:0.75rem"><img src="data:image/png;base64,${{gen.b64_png}}" style="max-width:100%;border:1px solid #222"></div>`
    : '';
  return `<div style="border:1px solid #222;padding:1rem;flex:1;min-width:260px">
    <div style="color:#00ff99;font-size:0.9rem;font-weight:bold;margin-bottom:0.25rem">${{gen.model_label}}</div>
    <div style="color:#555;font-size:0.72rem;margin-bottom:0.75rem">${{gen.model}} · ${{gen.size}}px · ${{gen.steps}} steps × batch ${{gen.batch_size}}</div>
    <div class="kpis">
      <div class="kpi"><div class="val" style="font-size:1.2rem">${{fmt(e.wh_per_image,4)}} Wh</div><div class="lbl">per image</div></div>
      <div class="kpi"><div class="val" style="font-size:1.2rem">${{fmt(gen.gen_s_per_image,1)}} s</div><div class="lbl">gen/image</div></div>
      <div class="kpi"><div class="val" style="font-size:1.1rem">${{fmt(e.delta_w,1)}} W</div><div class="lbl">delta W</div></div>
      <div class="kpi"><div class="val" style="font-size:1.1rem">${{e.poll_count}}</div><div class="lbl">polls</div></div>
    </div>
    <div style="font-size:0.78rem;color:#555;margin-top:0.5rem">${{e.confidence.flag}} ${{e.confidence.label}}</div>
    ${{imgHtml}}
  </div>`;
}}

function renderCompareModels(r) {{
  const a = r.analysis;
  document.getElementById('status').innerHTML = `
    <div class="result-box">
      <h2>SD-Turbo vs SDXL-Turbo — Same Prompt + Seed</h2>
      <div style="background:#111;border:1px solid #333;padding:0.75rem 1rem;margin-bottom:1.25rem;font-size:0.85rem;color:#ccc">
        ${{a.finding}}
      </div>
      <div style="display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:1rem;align-items:stretch">
        ${{_modelCard(r.small)}}
        ${{_modelCard(r.large)}}
      </div>
      <div style="font-size:0.75rem;color:#444;margin-top:0.5rem">
        Prompt: "${{r.full_prompt}}" · modifier: <em>${{r.modifier}}</em> · seed: ${{r.seed}}
      </div>
      <div style="font-size:0.75rem;color:#666;margin-top:0.75rem;font-style:italic">
        Quality is subjective. Judge the visual output above — is the larger model's image worth
        ${{a.energy_ratio_large_over_small}}× the energy for this prompt?
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
async def image_start(prompt: str = Form(...),
                      device: str = Form("cpu"),
                      model_key: str = Form("sd-turbo")):
    if device not in ("cpu", "gpu", "both", "compare_models"):
        device = "cpu"
    if model_key not in IMAGE_MODELS:
        return JSONResponse({"error": f"Unknown model: {model_key}"}, status_code=400)
    cfg_m = IMAGE_MODELS[model_key]
    if device in ("cpu", "both") and not cfg_m["cpu_ok"]:
        return JSONResponse(
            {"error": f"{cfg_m['label']} is GPU-only — pick GPU or Compare Models."},
            status_code=400,
        )
    job_id = uuid.uuid4().hex[:8]
    if device == "compare_models":
        label = f"Image (compare SD/SDXL-Turbo) — {prompt[:35]}"
    else:
        label = f"Image ({cfg_m['label']} · {device.upper()}) — {prompt[:35]}"

    async def coro():
        try:
            if device == "compare_models":
                result = await run_image_compare_models_measurement(prompt, job_id, jobs)
            elif device == "both":
                result = await run_image_both_measurement(
                    prompt, job_id, jobs, model_key=model_key)
            else:
                result = await run_image_measurement(
                    prompt, job_id, jobs, device=device, model_key=model_key)
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
    <link rel="icon" type="image/png" href="https://static.wixstatic.com/media/b1006e_f5e9aff607cf4133abf7089207dc3cab~mv2.png">
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
    if (!type || !jobId || type === 'variance') return '';
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


_METHODOLOGY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" type="image/png" href="https://static.wixstatic.com/media/b1006e_f5e9aff607cf4133abf7089207dc3cab~mv2.png">
<title>WattLab — Methodology</title>
<style>
  :root {
    --bg: #0a0a0a;
    --surface: #141414;
    --surface-hover: #1a1a1a;
    --border: #2a2a2a;
    --text: #e0e0e0;
    --text-dim: #888;
    --accent: #00ff99;
    --accent-dim: rgba(0,255,153,0.15);
    --warning: #ffaa00;
    --red: #ff4444;
    --mono: 'SF Mono', 'Cascadia Code', 'Fira Code', Consolas, monospace;
    --sans: 'Inter', system-ui, -apple-system, sans-serif;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    font-size: 15px;
    line-height: 1.7;
    padding: 0;
  }

  /* ── Header bar (matches other WattLab pages) ── */
  .topbar {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 14px 24px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }
  .topbar img { height: 32px; border-radius: 50%; }
  .topbar .title {
    font-family: var(--mono);
    font-size: 14px;
    color: var(--accent);
    letter-spacing: 0.5px;
  }
  .topbar .back {
    margin-left: auto;
    color: var(--text-dim);
    text-decoration: none;
    font-size: 13px;
    font-family: var(--mono);
  }
  .topbar .back:hover { color: var(--accent); }

  /* ── Main content ── */
  .content {
    max-width: 780px;
    margin: 0 auto;
    padding: 40px 24px 80px;
  }

  h1 {
    font-family: var(--mono);
    font-size: 22px;
    color: var(--accent);
    margin-bottom: 6px;
    letter-spacing: 0.5px;
  }
  .subtitle {
    color: var(--text-dim);
    font-size: 13px;
    font-family: var(--mono);
    margin-bottom: 36px;
  }

  h2 {
    font-family: var(--mono);
    font-size: 15px;
    color: var(--accent);
    margin-top: 40px;
    margin-bottom: 16px;
    padding-bottom: 6px;
    border-bottom: 1px solid var(--border);
    letter-spacing: 0.3px;
  }

  h3 {
    font-family: var(--sans);
    font-size: 14px;
    font-weight: 600;
    color: var(--text);
    margin-top: 24px;
    margin-bottom: 8px;
  }

  p { margin-bottom: 14px; }

  /* ── Scope banner ── */
  .scope-banner {
    background: var(--accent-dim);
    border: 1px solid rgba(0,255,153,0.3);
    border-radius: 6px;
    padding: 16px 20px;
    margin-bottom: 32px;
    font-family: var(--mono);
    font-size: 13px;
    line-height: 1.6;
    color: var(--accent);
  }
  .scope-banner strong { color: #fff; }

  /* ── Protocol steps ── */
  .protocol-steps {
    counter-reset: step;
    list-style: none;
    padding: 0;
    margin: 16px 0 20px;
  }
  .protocol-steps li {
    counter-increment: step;
    position: relative;
    padding: 12px 16px 12px 52px;
    margin-bottom: 8px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 5px;
    font-size: 14px;
    line-height: 1.5;
  }
  .protocol-steps li::before {
    content: counter(step);
    position: absolute;
    left: 16px;
    top: 12px;
    width: 24px;
    height: 24px;
    background: var(--accent-dim);
    border: 1px solid rgba(0,255,153,0.3);
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--accent);
    font-weight: 600;
  }
  .protocol-steps li code {
    font-family: var(--mono);
    font-size: 12px;
    background: rgba(255,255,255,0.06);
    padding: 1px 5px;
    border-radius: 3px;
    color: var(--accent);
  }

  /* ── Confidence table ── */
  .confidence-table {
    width: 100%;
    border-collapse: collapse;
    margin: 16px 0 20px;
    font-size: 14px;
  }
  .confidence-table th {
    text-align: left;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--text-dim);
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .confidence-table td {
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
  }
  .confidence-table tr:last-child td { border-bottom: none; }
  .badge { font-size: 16px; }

  /* ── Hardware spec table ── */
  .hw-table {
    width: 100%;
    border-collapse: collapse;
    margin: 16px 0 20px;
    font-size: 14px;
  }
  .hw-table td {
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
  }
  .hw-table td:first-child {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--text-dim);
    width: 160px;
    white-space: nowrap;
  }

  /* ── Info callout ── */
  .callout {
    background: var(--surface);
    border-left: 3px solid var(--warning);
    padding: 14px 18px;
    margin: 16px 0 20px;
    border-radius: 0 5px 5px 0;
    font-size: 14px;
  }
  .callout.green { border-left-color: var(--accent); }

  /* ── Formula block ── */
  .formula {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 5px;
    padding: 16px 20px;
    margin: 14px 0 20px;
    font-family: var(--mono);
    font-size: 13px;
    line-height: 1.8;
    color: var(--text);
    overflow-x: auto;
  }
  .formula .label {
    color: var(--text-dim);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    display: block;
    margin-bottom: 4px;
  }
  .formula .var { color: var(--accent); }

  /* ── Open questions ── */
  .open-q {
    padding: 10px 16px;
    margin-bottom: 6px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 5px;
    font-size: 14px;
    display: flex;
    gap: 10px;
    align-items: baseline;
  }
  .open-q .marker {
    color: var(--warning);
    font-family: var(--mono);
    font-size: 12px;
    flex-shrink: 0;
  }

  /* ── Section links (bottom nav) ── */
  .section-nav {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 12px 0 28px;
  }
  .section-nav a {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--accent);
    text-decoration: none;
    padding: 5px 10px;
    background: var(--accent-dim);
    border: 1px solid rgba(0,255,153,0.2);
    border-radius: 4px;
  }
  .section-nav a:hover {
    background: rgba(0,255,153,0.25);
  }

  /* ── Timestamp footer ── */
  .footer-note {
    margin-top: 48px;
    padding-top: 20px;
    border-top: 1px solid var(--border);
    color: var(--text-dim);
    font-family: var(--mono);
    font-size: 11px;
    line-height: 1.6;
  }

  /* ── Home link (top + bottom) — matches `_BACK` used on other pages ── */
  .home-link {
    display: inline-block;
    color: #555;
    text-decoration: none;
    font-family: var(--mono);
    font-size: 13px;
  }
  .home-link:hover { color: var(--accent); }
  .home-link.top    { margin-bottom: 24px; }
  .home-link.bottom { margin-top: 32px; }

  /* ── Responsive ── */
  @media (max-width: 600px) {
    .content { padding: 24px 16px 60px; }
    h1 { font-size: 18px; }
    .protocol-steps li { padding-left: 44px; }
    .hw-table td:first-child { width: 120px; }
  }
</style>
</head>
<body>

<!-- Top bar -->
<div class="topbar">
  <a href="https://greeningofstreaming.org" target="_blank">
    <img src="https://static.wixstatic.com/media/b1006e_f5e9aff607cf4133abf7089207dc3cab~mv2.png" alt="GoS">
  </a>
  <span class="title">WattLab · Methodology</span>
  <a href="/" class="back">&larr; Home</a>
</div>

<div class="content">

  <a href="/" class="home-link top">&larr; Home</a>

  <h1>Measurement Methodology</h1>
  <p class="subtitle">How WattLab measures the energy cost of compute tasks &mdash; and what it doesn&rsquo;t measure.</p>

  <div class="section-nav">
    <a href="#scope">Scope</a>
    <a href="#principle">Principle</a>
    <a href="#protocol">Protocol</a>
    <a href="#energy">Energy maths</a>
    <a href="#confidence">Confidence</a>
    <a href="#hardware">Hardware</a>
    <a href="#tests">Test types</a>
    <a href="#limits">Limitations</a>
    <a href="#open">Open questions</a>
  </div>

  <h2 id="scope">Scope</h2>

  <div class="scope-banner">
    <strong>Device layer only.</strong><br>
    All measurements cover the GoS1 server: CPU, GPU, RAM, storage, fans, motherboard.<br>
    Network, CDN, client devices (CPE), and production/storage infrastructure are explicitly excluded.<br>
    LLM measurements do not include amortised training cost.
  </div>

  <p>WattLab measures what happens inside one machine when it performs a real task. This is intentionally narrow. The energy cost of streaming is distributed across data centres, networks, and consumer devices &mdash; each with different measurement challenges and attribution problems. We start with the layer we can measure directly, at the wall, with no modelling assumptions.</p>

  <p>This scoping decision means WattLab results are <em>not</em> lifecycle assessments and should not be cited as total-cost-of-delivery figures. They answer a specific question: how much additional energy does this server draw to perform this task, above its idle baseline?</p>

  <h2 id="principle">Measurement Principle</h2>

  <p>WattLab uses <strong>wall-power delta measurement</strong>: the difference between what the server draws at idle and what it draws under load, captured by an external smart plug.</p>

  <div class="callout green">
    The plug measures the entire system &mdash; not a model, not a software estimate, not a per-component reading. If the CPU fan spins faster, the PSU runs less efficiently, or the GPU draws from the 12V rail, it&rsquo;s all in the number.
  </div>

  <p>This follows the GoS <strong>REM (Remote Energy Measurement)</strong> approach: real devices, real workloads, measured externally, at polling intervals short enough to capture the task&rsquo;s energy profile.</p>

  <h2 id="protocol">Measurement Protocol</h2>

  <p>Every test in WattLab &mdash; video, LLM, image generation, RAG &mdash; follows the same core protocol:</p>

  <ol class="protocol-steps">
    <li>
      <strong>Focus mode.</strong> Suppress background system tasks (apt, cron, man-db, fwupd, etc.) that would introduce energy noise. Managed via <code>systemctl stop</code> with dedicated sudoers rules.
    </li>
    <li>
      <strong>Model unload</strong> (LLM/RAG only). Send <code>keep_alive=0</code> to Ollama and wait 3 seconds for GPU memory release. Ensures a cold start when cold-inference mode is selected.
    </li>
    <li>
      <strong>Baseline capture.</strong> Poll the Tapo P110 smart plug at 1-second intervals for a configurable period (default: 10 polls). The mean of these readings becomes W<sub>base</sub> &mdash; the server&rsquo;s idle power draw.
    </li>
    <li>
      <strong>Lock.</strong> Acquire <code>/tmp/gos-measure.lock</code> to prevent concurrent measurements from overlapping. A FIFO queue manages waiting jobs.
    </li>
    <li>
      <strong>Execute task.</strong> Run the actual workload (ffmpeg, Ollama inference, SD-Turbo diffusion) while continuing to poll the P110 at 1-second intervals. Thermal sensors (CPU Tctl, GPU junction, GPU PPT) are read in parallel.
    </li>
    <li>
      <strong>Compute energy.</strong> Calculate delta power, total energy, and per-unit metrics (see formulas below).
    </li>
    <li>
      <strong>Persist.</strong> Write the full result to a JSON file &mdash; parameters, energy report, raw poll data, thermal readings, confidence flag. Every result is reproducible and exportable.
    </li>
    <li>
      <strong>Focus exit.</strong> Restart suppressed system timers in parallel (via ThreadPoolExecutor) to minimise downtime.
    </li>
  </ol>

  <p>Between sequential runs (e.g., CPU vs GPU comparison), a configurable cooldown (default: 60 seconds) allows the system to return to thermal equilibrium.</p>

  <h2 id="energy">Energy Calculation</h2>

  <div class="formula">
    <span class="label">Delta power (average above idle)</span>
    <span class="var">&Delta;W</span> = mean(<span class="var">W<sub>polls</sub></span>) &minus; <span class="var">W<sub>base</sub></span>
  </div>

  <div class="formula">
    <span class="label">Total energy consumed by task</span>
    <span class="var">&Delta;E</span> = <span class="var">&Delta;W</span> &times; (<span class="var">&Delta;t</span> / 3600) &nbsp; [Wh]
    <br><br>
    where <span class="var">&Delta;t</span> = task duration in seconds
  </div>

  <div class="formula">
    <span class="label">Per-token energy (LLM / RAG)</span>
    <span class="var">E<sub>token</sub></span> = <span class="var">&Delta;E</span> / <span class="var">N<sub>tokens</sub></span> &nbsp; [mWh/token]
  </div>

  <div class="formula">
    <span class="label">Per-image energy (image generation)</span>
    <span class="var">E<sub>image</sub></span> = <span class="var">&Delta;E</span> / <span class="var">N<sub>images</sub></span> &nbsp; [Wh/image]
  </div>

  <p>All formulas use wall-power from the P110 (system-level), not component-level readings. The GPU&rsquo;s self-reported power (PPT via <code>amdgpu</code>) is captured for reference but is not used in the primary energy calculation &mdash; it covers only the GPU die, not the full system delta (CPU, RAM, drives, fans, PSU losses).</p>

  <h2 id="confidence">Confidence Framework</h2>

  <p>Every WattLab result carries a traffic-light confidence flag based on a <strong>variance-relative signal-to-noise ratio</strong>. The noise floor is not assumed &mdash; it is characterised empirically by running the same workload repeatedly and computing the coefficient of variation (CV = &sigma;/&mu;) across all &Delta;W readings. This CV, expressed as a percentage of baseline power, captures total system measurement noise: P110 quantisation, Wi-Fi polling jitter, background OS processes, and thermal drift combined.</p>

  <p>The current system variance and threshold multipliers are configurable in Settings and can be updated via the built-in calibration tool (H.264 CPU &rarr; cooldown &rarr; H.265 GPU, repeated N times on Meridian).</p>

  <table class="confidence-table">
    <tr>
      <th>Flag</th>
      <th>Meaning</th>
      <th>Criteria (defaults)</th>
    </tr>
    <tr>
      <td><span class="badge">&#x1F7E2;</span></td>
      <td><strong>Repeatable</strong> &mdash; Signal clearly exceeds the measured noise floor with enough poll samples to be reliable.</td>
      <td>&Delta;W &gt; 5 &times; noise<sub>W</sub> and &ge; 10 polls</td>
    </tr>
    <tr>
      <td><span class="badge">&#x1F7E1;</span></td>
      <td><strong>Early insight</strong> &mdash; Signal is detectable above noise but more data or a longer run would strengthen the result.</td>
      <td>&Delta;W &ge; 2 &times; noise<sub>W</sub> or &ge; 5 polls</td>
    </tr>
    <tr>
      <td><span class="badge">&#x1F534;</span></td>
      <td><strong>Need more data</strong> &mdash; Signal is at or below the noise floor; result cannot be reliably distinguished from measurement variance.</td>
      <td>Below yellow threshold</td>
    </tr>
  </table>

  <div class="formula">
    <span class="label">Noise floor (watts) from configured variance</span>
    <span class="var">noise<sub>W</sub></span> = (<span class="var">variance_pct</span> / 100) &times; <span class="var">W<sub>base</sub></span>
  </div>

  <div class="callout green">
    <strong>Why variance-relative thresholds?</strong> Fixed watt thresholds (e.g. &ldquo;&Delta;W &gt; 5W&rdquo;) do not account for the actual noise of the measurement system on a given day. A server with high background process noise requires a larger signal to be trustworthy. By anchoring thresholds to empirically measured variance, the confidence flag reflects real signal quality rather than an assumed noise floor.
  </div>

  <div class="callout">
    <strong>P110 and total system noise:</strong> The Tapo P110 smart plug contributes hardware quantisation noise (~1W resolution when polled via local API). However, the dominant noise sources in practice are OS background processes (apt, cron, systemd timers) and thermal drift between runs. Focus mode suppresses the worst offenders, but residual variance remains. The variance calibration process measures this combined noise empirically and stores it as the reference for all confidence calculations.
  </div>

  <p>The confidence framework follows GoS&rsquo;s broader principle: <em>if it can&rsquo;t be measured, it shouldn&rsquo;t be asserted.</em> A &#x1F534; result is not a failure &mdash; it&rsquo;s an honest signal that the measurement instrument isn&rsquo;t sensitive enough for that task. Publishing it transparently is more useful than hiding it.</p>

  <h2 id="hardware">Hardware Disclosure</h2>

  <p>All results are tied to specific hardware. Different CPUs, GPUs, RAM configurations, and PSU efficiencies will produce different numbers. WattLab results should always be cited with their hardware context.</p>

  <table class="hw-table">
    <tr><td>Server</td><td>GoS1 &mdash; custom build, Ubuntu 24, kernel 6.17</td></tr>
    <tr><td>CPU</td><td>AMD Ryzen 9 7900, 24 cores (12C/24T), 65W TDP</td></tr>
    <tr><td>GPU</td><td>AMD Radeon RX 7800 XT, 16GB VRAM (11.1GB usable), VAAPI + ROCm</td></tr>
    <tr><td>RAM</td><td>61 GB DDR5</td></tr>
    <tr><td>Storage</td><td>457 GB (NVMe)</td></tr>
    <tr><td>Idle power</td><td>~51&ndash;54W (stable), occasional drift to 58W</td></tr>
    <tr><td>Measurement</td><td>Tapo P110 smart plug, 1-second polling via local API (tapo 0.8.12)</td></tr>
    <tr><td>Video</td><td>ffmpeg 6.1.1 &mdash; libx264, libx265, libsvtav1 (CPU); h264_vaapi, hevc_vaapi, av1_vaapi (GPU, full VAAPI pipeline)</td></tr>
    <tr><td>LLM</td><td>Ollama 0.20.2 &mdash; TinyLlama 1.1B, Mistral 7B, Gemma 3 12B (CPU + ROCm GPU); Phi-4 14B available for RAG</td></tr>
    <tr><td>Image</td><td>PyTorch + diffusers &mdash; SD-Turbo (~1B), SDXL-Turbo (~3.5B, GPU only); CPU + ROCm GPU</td></tr>
  </table>

  <h2 id="tests">Test Types</h2>

  <h3>Video transcoding</h3>
  <p>Transcode a source file (default: Netflix Meridian 4K, CC BY 4.0) to a target codec and 1080p. Measures the energy cost of the full encode pipeline &mdash; decode, colour-space conversion, scale, encode. Supports CPU vs GPU comparison: both paths are run sequentially with a cooldown between them, and results are presented side by side.</p>
  <p>Six presets across three codecs: <strong>H.264</strong> (libx264 / h264_vaapi, 4000 kbps), <strong>H.265</strong> (libx265 / hevc_vaapi, 2000 kbps), <strong>AV1</strong> (libsvtav1 / av1_vaapi, 1500 kbps). A seventh <strong>Compare all codecs</strong> preset runs all six in sequence and produces a cross-codec energy matrix.</p>
  <p>All presets use <strong>ABR (Average Bit Rate)</strong> rate control at a shared per-codec bitrate target, so CPU and GPU receive the identical encoding task &mdash; output file sizes match across devices as confirmation. All GPU presets use the <strong>full VAAPI pipeline</strong>: hardware decode (<code>-hwaccel vaapi</code>) + <code>scale_vaapi</code> + hardware encode, with frames GPU-resident throughout. This represents real live-encoding workflows (Harmonic, Ateme); an earlier partial pipeline (CPU decode + GPU encode) has been replaced because it was unrepresentative and bottlenecked on CPU decode overhead.</p>
  <p>The ffmpeg command used for each run is logged in the result JSON, editable from the page (on LAN), and reproduced in the result card for full transparency.</p>

  <div class="callout">
    <strong>Open item (narrower than before):</strong> With ABR, the bitrate target is now equal across devices. GOP structure and profile level are not yet explicitly controlled and may differ between CPU and GPU encoder defaults &mdash; a working session with the measurement team is planned to confirm apples-to-apples output at the profile/GOP level. A second benchmark family at each codec&rsquo;s natural operating point (CRF for CPU, QP for GPU) is also on the roadmap.
  </div>

  <h3>LLM inference</h3>
  <p>Run a language model on a fixed prompt and measure energy per token. Three model sizes are available spanning small to large: <strong>TinyLlama 1.1B</strong>, <strong>Mistral 7B</strong>, <strong>Gemma 3 12B</strong>. Supports cold inference (model unloaded before each run, measuring load + inference cost) and warm inference (model pre-loaded, measuring steady-state cost). Batch mode runs the prompt multiple times in sequence, with a configurable rest period between iterations, and reports the aggregate. CPU vs GPU comparison is also available.</p>
  <p>Prompts are editable and saved in the result JSON. Streaming output is displayed word-by-word as proof that inference is happening live. The mWh/token metric divides total energy by total tokens generated.</p>

  <h3>Image generation</h3>
  <p>Generate images from text prompts using one of two diffusion models, both distilled &ldquo;turbo&rdquo; variants designed for 1&ndash;4 step inference:</p>
  <ul style="margin: 12px 0 18px 20px; font-size: 14px; line-height: 1.7;">
    <li><strong>SD-Turbo (~1B)</strong> &mdash; ADD-distilled SD 2.1. CPU or GPU. Solo-mode GPU uses 20 steps &times; batch 5 to keep runtime above the P110 polling floor (the model is over-sampled relative to its native 1&ndash;4 step range).</li>
    <li><strong>SDXL-Turbo (~3.5B)</strong> &mdash; ADD-distilled SDXL. GPU only (fp32 VAE upcast on Navi31 makes CPU impractical). 4 steps (native) &times; batch 15 at 512&times;512.</li>
  </ul>
  <p><strong>Compare Models ⚡</strong> runs both on GPU with the same prompt, same seed, same resolution (512&times;512) and each at its native 4-step operating point (SD-Turbo batch 30, SDXL-Turbo batch 15) &mdash; model size is the only variable so the energy comparison is apples-to-apples. Image quality is subjective and presented side-by-side for visual judgement. A random colour/mood modifier is appended to every prompt as live-generation proof.</p>

  <h3>RAG (Retrieval-Augmented Generation)</h3>
  <p>Compare three modes of LLM inference: baseline (no retrieval), RAG with 3 context chunks, and RAG with 8 context chunks. Uses ChromaDB with sentence-transformer embeddings to retrieve relevant passages from a document corpus before prompting the LLM. The &ldquo;Compare 3 modes&rdquo; function runs all three sequentially with fresh baselines, producing side-by-side energy comparisons.</p>

  <h2 id="limits">Known Limitations</h2>

  <div class="open-q"><span class="marker">&#9658;</span><span><strong>P110 temporal resolution.</strong> 1-second polling means tasks shorter than ~5 seconds produce few data points. Very fast models (e.g., TinyLlama single inference at 1&ndash;4 seconds) are at the edge of measurability. Batching mitigates this but changes what&rsquo;s being measured (batch cost, not single-inference cost).</span></div>

  <div class="open-q"><span class="marker">&#9658;</span><span><strong>P110 power resolution.</strong> The ~&plusmn;1W noise floor means low-delta tasks (e.g., idle audio processing, lightweight network operations) cannot be reliably measured with this instrument.</span></div>

  <div class="open-q"><span class="marker">&#9658;</span><span><strong>Single server.</strong> All results are from one machine. Generalisability to other hardware configurations is unknown without cross-platform measurement.</span></div>

  <div class="open-q"><span class="marker">&#9658;</span><span><strong>Baseline drift.</strong> The server&rsquo;s idle power occasionally drifts from ~51W to ~58W (thermal state, background processes). The per-run baseline capture mitigates this, but it introduces variance between runs taken at different times.</span></div>

  <div class="open-q"><span class="marker">&#9658;</span><span><strong>PSU efficiency curve.</strong> Wall power includes PSU conversion losses, which are non-linear (PSUs are less efficient at low and very high loads). Two tasks that consume the same <em>internal</em> power may report different wall-power deltas depending on where they sit on the PSU efficiency curve.</span></div>

  <h2 id="open">Open Questions</h2>

  <p>These are questions WattLab has surfaced but not yet answered. They are published here in the interest of transparency.</p>

  <div class="open-q"><span class="marker">?</span><span><strong>Confidence multipliers.</strong> The 5&times; / 2&times; noise multipliers for &#x1F7E2;/&#x1F7E1; are currently set by judgement. A working session with the measurement team is planned to derive statistically grounded values from repeated calibration runs across different workloads and thermal states.</span></div>

  <div class="open-q"><span class="marker">?</span><span><strong>Transcoding profile/GOP equivalence.</strong> ABR rate control now gives CPU and GPU the same bitrate target, and output file sizes match as confirmation. GOP structure and profile level are still default-per-encoder and have not been explicitly normalised. A working session is planned to confirm apples-to-apples at that level, and to add a second benchmark family at each codec&rsquo;s natural operating point (CRF for CPU, QP for GPU).</span></div>

  <div class="open-q"><span class="marker">?</span><span><strong>LLM batch size effect.</strong> Does mWh/token change as batch count increases (thermal saturation, memory pressure)? Are the first and last runs in a batch energetically equivalent?</span></div>

  <div class="open-q"><span class="marker">?</span><span><strong>RAG retrieval overhead.</strong> How much of the RAG energy delta is embedding lookup vs. increased context length? Can these be separated?</span></div>

  <div class="open-q"><span class="marker">?</span><span><strong>Cross-platform comparability.</strong> How should results from different hardware be compared? Normalisation by TDP? By performance tier? By workload-equivalent output quality?</span></div>

  <div class="footer-note">
    WattLab is built and maintained by <a href="https://greeningofstreaming.org" style="color:var(--accent);text-decoration:none;">Greening of Streaming</a>, a French NGO (loi 1901).<br>
    Methodology version 0.2 &middot; last updated 2026-04-24 &middot; Feedback: bs@ctoic.net<br>
    Source: <a href="https://github.com/greeningofstreaming/wattlab" style="color:var(--accent);text-decoration:none;">github.com/greeningofstreaming/wattlab</a>
  </div>

  <a href="/" class="home-link bottom">&larr; Home</a>

</div>
</body>
</html>"""


@app.get("/methodology", response_class=HTMLResponse)
async def methodology_page():
    return _METHODOLOGY_HTML
