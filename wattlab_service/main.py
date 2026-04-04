import asyncio
import io
import json
import uuid
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, Form
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from dotenv import dotenv_values
from tapo import ApiClient
from video import run_video_measurement, run_both_measurement, run_video_measurement_path, run_both_measurement_path, UPLOAD_DIR, LOCK_FILE
from sources import get_all_sources, PRELOADED
from llm import run_llm_measurement, run_llm_batch_measurement, MODELS, TASKS
from persist import save_result, list_results, load_result, to_csv
from image_gen import run_image_measurement, IMAGE_STEPS
import settings as cfg

config = dotenv_values("/home/gos/wattlab/.env")
app = FastAPI()
jobs = {}

# --- Queue ---
pending_queue = []          # list of {"job_id", "type", "label", "coro_fn"}
queue_event = asyncio.Event()
current_job_id = None       # job currently executing


def enqueue(job_id: str, job_type: str, label: str, coro_fn) -> int:
    """Add a job to the FIFO queue. Returns 1-based queue position."""
    position = len(pending_queue) + 1
    jobs[job_id] = {"stage": "queued", "queue_position": position, "result": None, "error": None}
    pending_queue.append({"job_id": job_id, "type": job_type, "label": label, "coro_fn": coro_fn})
    queue_event.set()
    return position


@app.on_event("startup")
async def startup():
    asyncio.create_task(queue_worker())


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

# --- P110 ---

async def get_power_watts() -> float:
    client = ApiClient(config["TAPO_EMAIL"], config["TAPO_PASSWORD"])
    device = await client.p110(config["TAPO_P110_IP"])
    result = await device.get_energy_usage()
    return result.current_power / 1000

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
        .nav {{ margin-top: 3rem; display: flex; gap: 1.5rem; }}
        .nav a {{ color: #00ff99; text-decoration: none;
                  border: 1px solid #00ff99; padding: 0.5rem 1.5rem; }}
        .nav a:hover {{ background: #00ff9922; }}
    </style>
</head>
<body>
    <div style="position:fixed;top:1rem;left:1.5rem">{_LOGO}</div>
    <div class="watts">{watts:.1f} W</div>
    <div class="label">GoS1 current power draw</div>
    <div class="scope">Device layer only · Tapo P110 · refreshes every 10s</div>
    <div class="nav">
        <a href="/video">▶ Video transcode test</a>
        <a href="/llm">▶ LLM inference test</a>
        <a href="/image">▶ Image generation test</a>
        <a href="/demo">◆ Demo mode</a>
        <a href="/queue-status">⏱ Queue</a>
        <a href="/settings">⚙ Settings</a>
    </div>
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
    {busy_banner}
    {_LOGO}
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
    <a class="back" href="/">← Back to power monitor</a>
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

    function formatElapsed(ms) {{
        const s = Math.floor(ms / 1000);
        const m = Math.floor(s / 60);
        return m > 0 ? `${{m}}m ${{s % 60}}s` : `${{s}}s`;
    }}

    function renderProgress(jobId, mode, serverStage) {{
        const stages = STAGES[mode];

        const stageMap = STAGE_MAP[mode];
        const currentStage = stageMap[serverStage] !== undefined
            ? stageMap[serverStage] : 0;

        const stageHTML = stages.map((label, i) => {{
            let state = i < currentStage ? 'done' : i === currentStage ? 'active' : 'pending';
            let icon = state === 'done' ? '✓' : state === 'active' ? '▶' : '·';
            let iconColor = state === 'done' ? '#00ff99' : state === 'active' ? '#ffaa00' : '#333';
            return `<div class="stage ${{state}}">
                <span class="stage-icon" style="color:${{iconColor}}">${{icon}}</span>
                <span class="stage-label">${{label}}</span>
            </div>`;
        }}).join('');

        const elapsed = Date.now() - startTime;
        document.getElementById('status').innerHTML = `
            <div class="progress-box">
                <div class="progress-header">Running measurement — do not close this tab</div>
                <div class="stages">${{stageHTML}}</div>
                <div class="progress-footer">
                    <span>Job: ${{jobId}}</span>
                    <span class="elapsed">Elapsed: ${{formatElapsed(elapsed)}}</span>
                    <span>polling every 5s · zero load on GoS1</span>
                </div>
            </div>`;
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
            const resp = await fetch('/video/job/' + jobId);
            const data = await resp.json();
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
                renderProgress(jobId, mode, data.stage || "starting");
                setTimeout(() => pollJob(jobId, mode), 5000);
            }}
        }} catch(e) {{
            setTimeout(() => pollJob(jobId, mode), 5000);
        }}
    }}

    function renderQueued(position) {{
        document.getElementById('status').innerHTML =
            '<div style="border:1px solid #333;padding:1.5rem">' +
            '<div style="color:#ffaa00;font-size:0.9rem;margin-bottom:0.75rem">⏱ Queued — position ' + position + '</div>' +
            '<div style="color:#555;font-size:0.82rem">Another measurement is running. Your job will start automatically.</div>' +
            '</div>';
    }}

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
        const elapsed = startTime ? formatElapsed(Date.now() - startTime) : '';
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
    </script>
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
    return {"job_id": job_id, "queue_position": position}


@app.get("/video/sources")
async def video_sources():
    return get_all_sources()


# --- LLM job runner ---

async def run_llm_job(job_id: str, model_key: str, task_key: str,
                      repeats: int = 1, warm: bool = False, prompt: str = None):
    try:
        jobs[job_id] = {"status": "running", "stage": "baseline", "partial_response": ""}
        if repeats > 1:
            result = await run_llm_batch_measurement(
                model_key, task_key, repeats, warm, prompt, jobs, job_id)
        else:
            result = await run_llm_measurement(
                model_key, task_key, jobs, job_id, warm, prompt)
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
    {_LOGO}
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
            <div style="color:#555;font-size:0.75rem;text-transform:uppercase;letter-spacing:0.05em">Prompt</div>
            <button onclick="resetPrompt()" style="background:none;border:none;color:#444;
                font-size:0.75rem;cursor:pointer;padding:0;font-family:monospace">Reset</button>
        </div>
        <textarea id="promptText" rows="3"
            style="width:100%;background:#0f0f0f;border:1px solid #333;color:#ccc;
                   font-family:monospace;font-size:0.8rem;padding:0.75rem;
                   resize:vertical;line-height:1.5"></textarea>
    </div>

    <div style="display:flex;gap:2rem;margin-bottom:1.5rem;flex-wrap:wrap">
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

    <button id="runBtn" onclick="runInference()">Run Measurement</button>
    <div id="status"></div>
    <a class="back" href="/">← Back to power monitor</a>
    <div id="prev-runs" style="margin-top:2rem;border-top:1px solid #111;padding-top:1.5rem"></div>

    <script>
    let selectedModel = 'tinyllama';
    let selectedTask = 'T1';
    let selectedWarm = false;
    let selectedRepeats = 1;
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

    function formatElapsed(ms) {{
        const s = Math.floor(ms / 1000);
        const m = Math.floor(s / 60);
        return m > 0 ? m + 'm ' + (s%60) + 's' : s + 's';
    }}

    function renderProgress(stage) {{
        // Normalise batch stages for display
        const displayStage = stage.startsWith('inference_') ? 'inference' :
                             stage.startsWith('rest_') ? 'rest' : stage;
        const stageLabel = stage.startsWith('inference_') ? 'Running inference (' + stage.replace('inference_','').replace('_',' ') + ')' :
                           stage.startsWith('rest_') ? 'Resting between runs…' : null;
        const stages = [
            ['baseline', 'Measuring baseline'],
            ['inference', stageLabel || 'Running inference'],
            ['rest', 'Resting between runs…'],
            ['done', 'Done'],
        ].filter(([k]) => k !== 'rest' || displayStage === 'rest');
        const stageHTML = stages.map(([key, label]) => {{
            const state = displayStage === key ? 'active' :
                stages.findIndex(s=>s[0]===key) < stages.findIndex(s=>s[0]===displayStage) ? 'done' : 'pending';
            const icon = state === 'done' ? '✓' : state === 'active' ? '▶' : '·';
            const color = state === 'done' ? '#00ff99' : state === 'active' ? '#ffaa00' : '#333';
            return `<div class="stage ${{state}}">
                <span style="color:${{color}};width:1.2rem">${{icon}}</span>
                <span class="stage-label">${{label}}</span>
            </div>`;
        }}).join('');
        const elapsed = startTime ? formatElapsed(Date.now() - startTime) : '0s';
        document.getElementById('status').innerHTML = `
            <div class="progress-box">
                <div class="progress-header">Running — do not close this tab</div>
                ${{stageHTML}}
                <div style="color:#444;font-size:0.78rem;margin-top:0.75rem">
                    Elapsed: ${{elapsed}}
                </div>
                <div id="stream-preview" style="margin-top:0.75rem;background:#111;
                    padding:0.75rem;font-size:0.78rem;color:#888;line-height:1.6;
                    min-height:2rem;border-left:2px solid #00ff9933;max-height:120px;
                    overflow-y:auto;white-space:pre-wrap"></div>
            </div>`;
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
            const resp = await fetch('/llm/job/' + jobId);
            const data = await resp.json();
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
                document.getElementById('status').innerHTML =
                    '<div style="border:1px solid #333;padding:1.5rem">' +
                    '<div style="color:#ffaa00;font-size:0.9rem;margin-bottom:0.75rem">⏱ Queued — position ' + data.queue_position + '</div>' +
                    '<div style="color:#555;font-size:0.82rem">Another measurement is running. Your job will start automatically.</div>' +
                    '</div>';
                streamTimer = setTimeout(() => pollLLM(jobId), 3000);
            }} else {{
                const stage = data.stage || 'baseline';
                renderProgress(stage);
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
        const elapsed = startTime ? formatElapsed(Date.now() - startTime) : '';
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
        if (r.mode === 'batch') {{
            body = renderLLMBatch(r);
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
                <div class="scope-note">${{r.scope}}</div>
            </div>`;
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
    </script>
</body>
</html>"""

@app.post("/llm/run")
async def llm_run(
    model_key: str = Form(...),
    task_key: str = Form(...),
    repeats: int = Form(1),
    warm: bool = Form(False),
    prompt: str = Form(None),
):
    if model_key not in MODELS:
        return JSONResponse({"error": "Invalid model"}, status_code=400)
    if task_key not in TASKS:
        return JSONResponse({"error": "Invalid task"}, status_code=400)
    if repeats not in (1, 3, 5):
        return JSONResponse({"error": "repeats must be 1, 3, or 5"}, status_code=400)

    effective_prompt = prompt.strip() if prompt and prompt.strip() else None
    job_id = str(uuid.uuid4())[:8]
    label = f"LLM — {MODELS[model_key]['label']} · {TASKS[task_key]['label']}"

    async def coro():
        await run_llm_job(job_id, model_key, task_key, repeats, warm, effective_prompt)

    position = enqueue(job_id, "llm", label, coro)
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
        running = {"job_id": current_job_id, "stage": j.get("stage")}
    pending_info = [
        {"job_id": e["job_id"], "type": e["type"], "label": e["label"], "position": i + 1}
        for i, e in enumerate(pending_queue)
    ]
    return {
        "depth": len(pending_queue) + (1 if current_job_id else 0),
        "running": running,
        "pending": pending_info,
    }


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

@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    s = cfg.load()
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
        .row {{ display:flex; justify-content:space-between; align-items:baseline;
                padding:0.5rem 0; border-bottom:1px solid #0d0d0d; gap:1rem; }}
        .row label {{ color:#aaa; font-size:0.85rem; flex:1; }}
        .row .hint {{ color:#333; font-size:0.72rem; margin-top:0.2rem; }}
        .row-right {{ display:flex; align-items:baseline; gap:0.5rem; }}
        input[type=number] {{ background:#111; border:1px solid #333; color:#e0e0e0;
                              font-family:monospace; font-size:0.9rem;
                              padding:0.3rem 0.5rem; width:80px; text-align:right; }}
        input[type=number]:focus {{ border-color:#00ff99; outline:none; }}
        .unit {{ color:#555; font-size:0.8rem; }}
        button {{ background:#00ff99; color:#000; border:none; padding:0.75rem 2rem;
                  cursor:pointer; font-family:monospace; font-size:1rem; margin-top:2rem; }}
        button:hover {{ background:#00dd88; }}
        #msg {{ margin-top:1rem; font-size:0.85rem; }}
        a.back {{ color:#555; text-decoration:none; font-size:0.82rem;
                  display:inline-block; margin-top:1.5rem; }}
        a.back:hover {{ color:#00ff99; }}
    </style>
</head>
<body>
    {_LOGO}
    <h1>Settings</h1>
    <div class="subtitle">WattLab · GoS1 · Lab mode</div>

    <div class="section">Measurement</div>

    <div class="row">
        <div><label>Baseline polls</label>
        <div class="hint">× 1s = baseline window duration</div></div>
        <div class="row-right">
            <input type="number" id="baseline_polls" min="5" max="60" value="{s['baseline_polls']}">
            <span class="unit">× 1s</span>
        </div>
    </div>

    <div class="row">
        <div><label>Video cooldown</label>
        <div class="hint">Rest between CPU and GPU runs (Both mode)</div></div>
        <div class="row-right">
            <input type="number" id="video_cooldown_s" min="10" max="300" value="{s['video_cooldown_s']}">
            <span class="unit">s</span>
        </div>
    </div>

    <div class="row">
        <div><label>LLM rest between runs</label>
        <div class="hint">Pause between each run in batch mode</div></div>
        <div class="row-right">
            <input type="number" id="llm_rest_s" min="5" max="120" value="{s['llm_rest_s']}">
            <span class="unit">s</span>
        </div>
    </div>

    <div class="row">
        <div><label>LLM unload settle</label>
        <div class="hint">Wait after model unload before baseline</div></div>
        <div class="row-right">
            <input type="number" id="llm_unload_settle_s" min="1" max="30" value="{s['llm_unload_settle_s']}">
            <span class="unit">s</span>
        </div>
    </div>

    <div class="section">Confidence thresholds</div>

    <div class="row">
        <div><label>🟢 Green: ΔW &gt;</label>
        <div class="hint">AND polls ≥ green polls</div></div>
        <div class="row-right">
            <input type="number" id="conf_green_delta_w" min="0" max="50" step="0.5" value="{s['conf_green_delta_w']}">
            <span class="unit">W</span>
        </div>
    </div>

    <div class="row">
        <div><label>🟢 Green: polls ≥</label></div>
        <div class="row-right">
            <input type="number" id="conf_green_polls" min="1" max="100" value="{s['conf_green_polls']}">
            <span class="unit">polls</span>
        </div>
    </div>

    <div class="row">
        <div><label>🟡 Yellow: ΔW ≥</label>
        <div class="hint">OR polls ≥ yellow polls</div></div>
        <div class="row-right">
            <input type="number" id="conf_yellow_delta_w" min="0" max="20" step="0.5" value="{s['conf_yellow_delta_w']}">
            <span class="unit">W</span>
        </div>
    </div>

    <div class="row">
        <div><label>🟡 Yellow: polls ≥</label></div>
        <div class="row-right">
            <input type="number" id="conf_yellow_polls" min="1" max="50" value="{s['conf_yellow_polls']}">
            <span class="unit">polls</span>
        </div>
    </div>

    <button onclick="saveSettings()">Save Settings</button>
    <div id="msg"></div>
    <a class="back" href="/">← Back to power monitor</a>

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
</body>
</html>"""


@app.post("/settings")
async def settings_save(data: dict):
    saved = cfg.save(data)
    return {"ok": True, "settings": saved}


# --- Demo mode ---

_DEMO_HTML = f"""<!DOCTYPE html>
<html>
<head>
<title>WattLab — Demo · Greening of Streaming</title>
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

<div class="page-header">
  {_LOGO}
  <div id="step-nav" class="step-nav">
    <span class="dot active" id="dot-0"></span>
    <span class="dot" id="dot-1"></span>
    <span class="dot" id="dot-2"></span>
    <span class="dot" id="dot-3"></span>
    <span class="dot" id="dot-4"></span>
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
    <button class="btn btn-primary" onclick="goStep(1)">Start Demo →</button>
    <a href="/" class="btn btn-secondary" style="text-decoration:none;
       display:inline-block;line-height:1">← Lab mode</a>
  </div>
</div>

<!-- Step 1: Video -->
<div class="step" id="step-1">
  <h1>Video Transcode</h1>
  <p style="color:#555;font-size:0.85rem;margin-bottom:1.5rem">Step 1 of 2</p>

  <p style="color:#aaa;line-height:1.8;max-width:560px;margin-bottom:1rem">
    We encode the same 4K source clip (Meridian, Netflix Open Content, CC BY 4.0)
    to 1080p H.264 — first in software (CPU) then with hardware acceleration (GPU).
    Both runs use the same input and quality target. Energy is measured throughout.
  </p>

  <details>
    <summary>Measurement protocol</summary>
    <p>10-second baseline measurement (×1s polls) before each run.
    P110 sampled every second during encode. A 60-second thermal cooldown
    separates the two runs so GPU baseline reflects true idle state.
    Confidence flag: 🟢 Repeatable = ΔW &gt; 5W and ≥ 10 polls.</p>
    <p>Codec: libx264 (CPU) · h264_vaapi (GPU) · CRF/QP 23 · 1080p output.
    Source: 4K, 812 MB. Encode time ~2–3 min CPU, ~90s GPU.</p>
  </details>

  <details>
    <summary>Why CPU vs GPU?</summary>
    <p>The answer is not obvious: GPU is faster but draws more peak power.
    Whether it saves energy depends on how long the task runs. WattLab
    measures the crossover point empirically. Our Session 1 data shows
    GPU uses 9.7% more total energy despite being 34.5% faster on this workload.</p>
  </details>

  <div id="video-action">
    <div class="btn-row" id="video-btns">
      <button class="btn btn-primary" id="btn-run-video" onclick="runDemoVideo()">
        Run new measurement (~5 min)</button>
      <button class="btn btn-secondary" id="btn-prev-video" onclick="showPrevVideo()">
        See last result</button>
    </div>
    <div id="video-status"></div>
  </div>
</div>

<!-- Step 2: LLM -->
<div class="step" id="step-2">
  <h1>LLM Inference</h1>
  <p style="color:#555;font-size:0.85rem;margin-bottom:1.5rem">Step 2 of 2</p>

  <p style="color:#aaa;line-height:1.8;max-width:560px;margin-bottom:1rem">
    We run a fixed technical prompt through Mistral 7B and measure how much
    energy each generated token costs. The model is unloaded before the
    baseline measurement so we capture the true cold-start cost.
  </p>

  <details>
    <summary>Measurement protocol</summary>
    <p>Model unloaded from VRAM. 3-second settle. 10-second baseline.
    Single inference run. P110 at 1-second intervals throughout.
    Primary metric: mWh per output token (energy per token of generated text).</p>
    <p>Model: Mistral 7B (4.4 GB, ROCm GPU). Prompt: T3 Long —
    "Write a detailed technical briefing on network energy attribution
    challenges in streaming impact measurement."</p>
  </details>

  <details>
    <summary>Why mWh per token?</summary>
    <p>Token count varies between models and prompts, making raw Wh figures
    hard to compare. Energy per token normalises for output length, letting
    us compare TinyLlama (0.06 mWh/tok) against Mistral 7B (0.94 mWh/tok)
    on the same basis. TinyLlama is ~15× more energy-efficient per token.</p>
  </details>

  <div id="llm-action">
    <div class="btn-row" id="llm-btns">
      <button class="btn btn-primary" id="btn-run-llm" onclick="runDemoLLM()">
        Run new measurement (~3 min)</button>
      <button class="btn btn-secondary" id="btn-prev-llm" onclick="showPrevLLM()">
        See last result</button>
    </div>
    <div id="llm-status"></div>
  </div>
</div>

<!-- Step 3: Image generation -->
<div class="step" id="step-3">
  <h1>Image generation</h1>
  <p class="step-intro">How much energy does one AI image cost? WattLab measures the full device draw — not an estimate.</p>
  <div class="method-box">
    <strong>Protocol:</strong> Baseline (10s idle) → CPU diffusion (SD-Turbo, 8 steps) → P110 measurement.
    A random colour modifier is appended to prove the image is generated live, not replayed.
  </div>

  <div id="image-btns" class="btn-row">
    <button class="btn btn-primary" onclick="runDemoImage()">Generate &amp; measure</button>
    <button class="btn btn-secondary" onclick="showPrevImage()">See previous run</button>
  </div>
  <div id="image-status"></div>
</div>

<!-- Step 4: Summary -->
<div class="step" id="step-4">
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
    <a href="/" class="btn btn-secondary"
       style="text-decoration:none;display:inline-block;line-height:1">Lab mode</a>
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
const stepLabels = ['Welcome', 'Video', 'LLM', 'Image', 'Findings'];
let streamTimer = null;
let imageTimer = null;

// ─── Step navigation ─────────────────────────────────────────────────────────
function goStep(n) {{
  document.querySelectorAll('.step').forEach(el => el.classList.remove('active'));
  document.getElementById('step-' + n).classList.add('active');
  for (let i = 0; i < 5; i++) {{
    const dot = document.getElementById('dot-' + i);
    dot.className = 'dot' + (i < n ? ' done' : i === n ? ' active' : '');
  }}
  const lbl = document.getElementById('nav-label');
  lbl.textContent = stepLabels[n];
  lbl.className = 'label active';
  currentStep = n;
  window.scrollTo(0, 0);
  if (n === 4) buildSummary();
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
  document.getElementById('video-status').innerHTML =
    '<p class="progress-note">Loading last result…</p>';
  try {{
    const resp = await fetch('/results/video/list');
    const list = await resp.json();
    if (!list || list.length === 0) {{
      document.getElementById('video-status').innerHTML =
        '<p class="progress-note" style="color:#555">No previous runs found.</p>' +
        '<div class="btn-row" style="margin-top:1rem">' +
        '<button class="btn btn-primary" onclick="runDemoVideo()">Run new measurement</button></div>';
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
  document.getElementById('llm-status').innerHTML =
    '<p class="progress-note">Loading last result…</p>';
  try {{
    const resp = await fetch('/results/llm/list');
    const list = await resp.json();
    if (!list || list.length === 0) {{
      document.getElementById('llm-status').innerHTML =
        '<p class="progress-note" style="color:#555">No previous runs found.</p>' +
        '<div class="btn-row" style="margin-top:1rem">' +
        '<button class="btn btn-primary" onclick="runDemoLLM()">Run new measurement</button></div>';
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

// Check on load whether previous results exist and update button labels
async function checkPrevResults() {{
  try {{
    const [vr, lr] = await Promise.all([
      fetch('/results/video/list').then(r => r.json()),
      fetch('/results/llm/list').then(r => r.json()),
    ]);
    if (vr && vr.length > 0) {{
      const btn = document.getElementById('btn-prev-video');
      btn.textContent = 'See last result (' + timeAgo(vr[0].saved_at) + ')';
    }} else {{
      document.getElementById('btn-prev-video').disabled = true;
    }}
    if (lr && lr.length > 0) {{
      const btn = document.getElementById('btn-prev-llm');
      btn.textContent = 'See last result (' + timeAgo(lr[0].saved_at) + ')';
    }} else {{
      document.getElementById('btn-prev-llm').disabled = true;
    }}
  }} catch(e) {{}}
}}
checkPrevResults();

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
  // Show Next button
  document.getElementById('video-status').innerHTML +=
    '<div class="btn-row" style="margin-top:1.5rem">' +
    '<button class="btn btn-primary" onclick="goStep(2)">Next: LLM inference →</button>' +
    '<button class="btn btn-secondary" onclick="resetVideoStep()">Run again</button></div>';
}}

function renderLLMResult(r, savedAt, isPrev) {{
  const prevNote = isPrev ? '<p class="prev-note">↩ Previous run · ' + timeAgo(savedAt) + '</p>' : '';
  const e = r.energy || (r.runs && r.runs[r.runs.length-1].energy);
  const inf = r.inference || (r.runs && r.runs[r.runs.length-1].inference);
  const modeNote = r.warm ? '🌡 Warm' : '❄ Cold';
  const html = prevNote + `<div class="result-card">
    <div class="kpi-row">
      <div class="kpi">
        <div class="val">${{fmt(e.mwh_per_token,4)}}</div>
        <div class="lbl">mWh / token</div>
      </div>
      <div class="kpi">
        <div class="val">${{fmt(inf.tokens_per_sec,1)}}</div>
        <div class="lbl">tokens / sec</div>
      </div>
      <div class="kpi">
        <div class="val">${{fmt(e.delta_e_wh,4)}} Wh</div>
        <div class="lbl">total energy</div>
      </div>
      <div class="kpi">
        <div class="val">${{inf.output_tokens}}</div>
        <div class="lbl">output tokens</div>
      </div>
    </div>
    <div class="conf-badge">${{e.confidence.flag}} ${{e.confidence.label}} · ${{r.model_label}} · ${{modeNote}}</div>
    <div class="response-preview">${{inf.response}}</div>
    <p class="scope-note">Device layer only (GoS1). No amortised training cost.</p>
  </div>`;
  document.getElementById('llm-status').innerHTML = html;
  document.getElementById('llm-btns').style.display = 'none';
  document.getElementById('llm-status').innerHTML +=
    '<div class="btn-row" style="margin-top:1.5rem">' +
    '<button class="btn btn-primary" onclick="goStep(3)">Next: Image generation →</button>' +
    '<button class="btn btn-secondary" onclick="resetLLMStep()">Run again</button></div>';
}}

function resetVideoStep() {{
  document.getElementById('video-btns').style.display = 'flex';
  document.getElementById('video-status').innerHTML = '';
  checkPrevResults();
}}
function resetLLMStep() {{
  document.getElementById('llm-btns').style.display = 'flex';
  document.getElementById('llm-status').innerHTML = '';
  checkPrevResults();
}}
function resetImageStep() {{
  document.getElementById('image-btns').style.display = 'flex';
  document.getElementById('image-status').innerHTML = '';
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
  document.getElementById('image-status').innerHTML =
    '<p class="progress-note">Loading last result…</p>';
  try {{
    const resp = await fetch('/results/image/list');
    const list = await resp.json();
    if (!list || list.length === 0) {{
      document.getElementById('image-status').innerHTML =
        '<p class="progress-note" style="color:#555">No previous runs found.</p>';
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
  const e = r.energy;
  const gen = r.generation;
  const imgHtml = gen && gen.b64_png
    ? '<img src="data:image/png;base64,' + gen.b64_png +
      '" style="max-width:100%;border:1px solid #222;display:block;margin-top:1rem">' +
      '<div style="color:#444;font-size:0.75rem;margin-top:0.5rem;font-style:italic">"' +
      r.full_prompt + '"</div>'
    : '';
  document.getElementById('image-status').innerHTML =
    '<div class="result-card">' +
    '<div class="result-kpis">' +
    '<div class="kpi"><div class="kval">' + fmt(e.delta_e_wh,4) + ' Wh</div>' +
    '<div class="klbl">energy / image</div></div>' +
    '<div class="kpi"><div class="kval">' + fmt(gen && gen.total_s,1) + 's</div>' +
    '<div class="klbl">generation time</div></div>' +
    '<div class="kpi"><div class="kval">' + fmt(e.delta_w,1) + ' W</div>' +
    '<div class="klbl">delta above idle</div></div>' +
    '</div>' +
    '<div class="conf">' + e.confidence.flag + ' ' + e.confidence.label + '</div>' +
    imgHtml +
    '</div>' +
    '<div class="btn-row" style="margin-top:1.5rem">' +
    '<button class="btn btn-primary" onclick="goStep(4)">See findings →</button>' +
    '<button class="btn btn-secondary" onclick="resetImageStep()">Run again</button></div>';
}}

// ─── Summary ─────────────────────────────────────────────────────────────────
function buildSummary() {{
  const el = document.getElementById('summary-content');
  let rows = '';

  if (videoResult && videoResult.mode === 'both') {{
    const a = videoResult.analysis;
    const ce = videoResult.cpu.energy, ge = videoResult.gpu.energy;
    rows += `<tr><td>Video · CPU energy</td><td>${{fmt(ce.delta_e_wh,4)}} Wh ${{a.energy_winner==='CPU'?'✓':''}}</td></tr>`;
    rows += `<tr><td>Video · GPU energy</td><td>${{fmt(ge.delta_e_wh,4)}} Wh ${{a.energy_winner==='GPU'?'✓':''}}</td></tr>`;
    rows += `<tr><td>Video · Speed delta</td><td>GPU ${{a.speed_diff_pct}}% faster</td></tr>`;
    rows += `<tr><td>Video · Finding</td><td style="color:#aaa;font-size:0.78rem">${{a.energy_winner}} used less energy</td></tr>`;
  }} else if (videoResult) {{
    rows += `<tr><td>Video result available</td><td style="color:#555">See Step 1</td></tr>`;
  }} else {{
    rows += `<tr><td>Video</td><td style="color:#333">No result this session</td></tr>`;
  }}

  if (llmResult) {{
    const e = llmResult.energy || (llmResult.runs && llmResult.runs[llmResult.runs.length-1].energy);
    const inf = llmResult.inference || (llmResult.runs && llmResult.runs[llmResult.runs.length-1].inference);
    rows += `<tr><td>LLM · Model</td><td>${{llmResult.model_label}}</td></tr>`;
    rows += `<tr><td>LLM · Energy / token</td><td>${{fmt(e.mwh_per_token,4)}} mWh/token</td></tr>`;
    rows += `<tr><td>LLM · Speed</td><td>${{fmt(inf.tokens_per_sec,1)}} tokens/sec</td></tr>`;
    rows += `<tr><td>LLM · Mode</td><td>${{llmResult.warm ? '🌡 Warm' : '❄ Cold'}}</td></tr>`;
  }} else {{
    rows += `<tr><td>LLM</td><td style="color:#333">No result this session</td></tr>`;
  }}

  if (imageResult) {{
    const e = imageResult.energy;
    rows += `<tr><td>Image · Energy / image</td><td>${{fmt(e.delta_e_wh,4)}} Wh</td></tr>`;
    rows += `<tr><td>Image · Generation time</td><td>${{fmt(imageResult.generation && imageResult.generation.total_s,1)}}s</td></tr>`;
    rows += `<tr><td>Image · Confidence</td><td>${{e.confidence.flag}} ${{e.confidence.label}}</td></tr>`;
  }} else {{
    rows += `<tr><td>Image</td><td style="color:#333">No result this session</td></tr>`;
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
            conf = r.get("confidence", {})
            fp = r.get("full_prompt", "")
            img_tag = ""
            if r.get("b64_png"):
                img_tag = f'<img src="data:image/png;base64,{r["b64_png"]}" style="width:80px;height:80px;object-fit:cover;vertical-align:middle;margin-right:0.75rem">'
            date_str = (r.get("saved_at") or "")[:10]
            prev_html += f"""<div class="prev-item">
              {img_tag}
              <span class="prev-meta">
                {date_str} &nbsp;·&nbsp; {conf.get("flag","")} {conf.get("label","")}
                &nbsp;·&nbsp; {r.get("delta_e_wh","?")} Wh/image
                &nbsp;·&nbsp; {r.get("delta_t_s","?")}s
              </span>
              <div class="prev-prompt" style="color:#555;font-size:0.75rem;margin-top:0.3rem">{fp[:80]}</div>
              <div style="margin-top:0.3rem">
                <a href="/results/image/{r['job_id']}/download.json" download style="color:#333;font-size:0.72rem;text-decoration:none;margin-right:0.75rem">↓ JSON</a>
                <a href="/results/image/{r['job_id']}/download.csv" download style="color:#333;font-size:0.72rem;text-decoration:none">↓ CSV</a>
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
    <div style="position:absolute;top:1rem;left:1.5rem">{_LOGO}</div>
    <div style="padding-top:3rem">
    <a href="/" class="back">← back to dashboard</a>
    {busy_banner}
    <h1>Image Generation Test</h1>
    <div class="subtitle">CPU diffusion · SD-Turbo · {IMAGE_STEPS} steps · 512×512 · Ryzen 9 7900</div>
    <div class="info">
        Measures the wall-power cost of generating one AI image from text.<br>
        Each run appends a random colour/mood modifier — a live proof that
        generation is happening, not replaying a cached result.<br>
        Model: <code>stabilityai/sd-turbo</code> (CPU, ~14s/image).
        GPU image generation deferred — model requires 12GB VRAM, card has 12GB.
    </div>

    <label style="color:#888;font-size:0.8rem;display:block;margin-bottom:0.4rem">Prompt</label>
    <textarea id="prompt" rows="3">a lone wind turbine in an open landscape</textarea>
    <div style="color:#555;font-size:0.75rem;margin-bottom:1.2rem">
        A random colour/mood modifier is appended per run (e.g. "bathed in emerald light").
    </div>

    <button id="run-btn" onclick="startMeasurement()">Generate &amp; Measure</button>
    <div id="status"></div>
    {prev_html}
    </div>

<script>
const STAGES = ['baseline','generating','done'];
const STAGE_LABELS = {{
  'baseline': 'Measuring baseline power',
  'generating': 'Generating image (CPU diffusion)',
  'done': 'Complete',
}};
let pollTimer = null;

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
    body: 'prompt=' + encodeURIComponent(prompt)
  }});
  const data = await resp.json();
  if (data.error) {{ alert(data.error); document.getElementById('run-btn').disabled=false; return; }}
  const jobId = data.job_id;

  renderProgress('baseline', null, null);
  pollTimer = setInterval(() => pollJob(jobId), 1500);
}}

async function pollJob(jobId) {{
  const r = await fetch('/image/job/' + jobId);
  const j = await r.json();

  if (j.stage === 'queued') {{
    document.getElementById('status').innerHTML =
      '<div style="border:1px solid #333;padding:1.5rem">' +
      '<div style="color:#ffaa00;font-size:0.9rem;margin-bottom:0.75rem">⏱ Queued — position ' + j.queue_position + '</div>' +
      '<div style="color:#555;font-size:0.82rem">Another measurement is running. Your job will start automatically.</div>' +
      '</div>';
    return;
  }}

  const powerR = await fetch('/power');
  const powerJ = await powerR.json().catch(() => ({{}}));
  renderProgress(j.stage, j.result, powerJ.watts ?? null);

  if (j.stage === 'done' && j.result) {{
    clearInterval(pollTimer);
    renderResult(j.result);
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
  const stageIdx = STAGES.indexOf(stage);
  let stagesHtml = '';
  STAGES.forEach((s, i) => {{
    let icon = i < stageIdx ? '✓' : (i === stageIdx ? '⏳' : '·');
    let col = i < stageIdx ? '#00ff99' : (i === stageIdx ? '#ffaa00' : '#333');
    stagesHtml += `<div class="stage">
      <span class="stage-icon" style="color:${{col}}">${{icon}}</span>
      <span style="color:${{col}}">${{STAGE_LABELS[s] || s}}</span>
    </div>`;
  }});
  const wattsHtml = watts !== null
    ? `<div class="live-watts">${{fmt(watts,1)}} W</div>
       <div style="color:#444;font-size:0.75rem">live wall power</div>`
    : '';
  document.getElementById('status').innerHTML = `
    <div class="progress-box">
      <div class="progress-header">⚡ Measuring…</div>
      <div class="stages">${{stagesHtml}}</div>
      ${{wattsHtml}}
    </div>`;
}}

function renderResult(r) {{
  const e = r.energy;
  const gen = r.generation;
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
          <div class="val">${{fmt(e.delta_e_wh,4)}} Wh</div>
          <div class="lbl">energy / image</div>
        </div>
        <div class="kpi">
          <div class="val">${{fmt(e.delta_w,1)}} W</div>
          <div class="lbl">delta above idle</div>
        </div>
        <div class="kpi">
          <div class="val">${{fmt(gen.total_s,1)}} s</div>
          <div class="lbl">generation time</div>
        </div>
        <div class="kpi">
          <div class="val">${{fmt(gen.load_s,1)}} s / ${{fmt(gen.gen_s,1)}} s</div>
          <div class="lbl">load / diffusion</div>
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
</script>
</body>
</html>"""


@app.post("/image/start")
async def image_start(prompt: str = Form(...)):
    job_id = uuid.uuid4().hex[:8]
    label = f"Image — {prompt[:40]}"

    async def coro():
        try:
            result = await run_image_measurement(prompt, job_id, jobs)
            save_result("image", job_id, result)
            jobs[job_id]["result"] = result
        except Exception as e:
            jobs[job_id]["error"] = str(e)
            LOCK_FILE.unlink(missing_ok=True)

    position = enqueue(job_id, "image", label, coro)
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
    <a href="/" class="back">← dashboard</a>
    <h1>Queue</h1>
    <div class="sub">Auto-refreshes every 4s</div>
    <div id="content"><p class="empty">Loading…</p></div>
<script>
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
                '<div class="label">' + (q.running.label || q.running.job_id) + '</div>' +
                '<div class="stage">stage: ' + (q.running.stage || '…') + '</div></div>';
    }
    (q.pending || []).forEach((j, i) => {
        html += '<div class="card waiting">' +
                '<span class="badge wait"># ' + j.position + '</span>' +
                '<div class="label">' + j.label + '</div>' +
                '<div class="stage">waiting</div></div>';
    });
    el.innerHTML = html;
}
load();
</script>
</body>
</html>"""


@app.get("/demo", response_class=HTMLResponse)
async def demo_page():
    return _DEMO_HTML
