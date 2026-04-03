import asyncio
import uuid
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, BackgroundTasks, Form
from fastapi.responses import HTMLResponse, JSONResponse
from dotenv import dotenv_values
from tapo import ApiClient
from video import run_video_measurement, run_both_measurement, UPLOAD_DIR, LOCK_FILE

config = dotenv_values("/home/gos/wattlab/.env")
app = FastAPI()
jobs = {}

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
    <div class="watts">{watts:.1f} W</div>
    <div class="label">GoS1 current power draw</div>
    <div class="scope">Device layer only · Tapo P110 · refreshes every 10s</div>
    <div class="nav">
        <a href="/video">▶ Video transcode test</a>
    </div>
</body>
</html>"""

@app.get("/power")
async def power_json():
    watts = await get_power_watts()
    return {{"watts": watts, "scope": "device_only", "source": "tapo_p110"}}

# --- Video page ---

@app.get("/video", response_class=HTMLResponse)
async def video_page():
    busy = LOCK_FILE.exists()
    busy_banner = """<div style="background:#ff4400;color:#fff;padding:1rem;
        text-align:center;margin-bottom:1rem">
        ⚠ GoS1 is currently running a measurement. Please wait.</div>""" \
        if busy else ""

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
    <h1>Video Transcode Energy Test</h1>
    <div class="subtitle">Greening of Streaming · WattLab · GoS1</div>

    <div class="info">
        Accepted: MP4, MOV, MKV, AVI, WebM, TS · Max 1GB<br>
        Baseline measured 10s before each run · P110 + thermals at 1s intervals<br>
        Scope: device layer only — network, CDN, CPE excluded
    </div>

    <div class="presets">
        <div class="preset" id="preset-cpu" onclick="selectPreset('cpu')">
            <h3>CPU Encode</h3>
            <p style="color:#555;font-size:0.75rem;margin-bottom:0.4rem">libx264 · CRF 23 · 1080p</p>
            <p>Software encode across all 24 cores.</p>
        </div>
        <div class="preset" id="preset-gpu" onclick="selectPreset('gpu')">
            <h3>GPU Encode</h3>
            <p style="color:#555;font-size:0.75rem;margin-bottom:0.4rem">h264_vaapi · QP 23 · 1080p</p>
            <p>AMD RX 7800 XT hardware acceleration.</p>
        </div>
        <div class="preset selected" id="preset-both" onclick="selectPreset('both')">
            <div class="badge">DEFAULT</div>
            <h3>Both — Compare</h3>
            <p style="color:#555;font-size:0.75rem;margin-bottom:0.4rem">CPU then GPU · same file</p>
            <p>Side-by-side energy + thermal report with analysis.</p>
        </div>
    </div>

    <input type="file" id="fileInput" accept=".mp4,.mov,.mkv,.avi,.webm,.ts">
    <button id="runBtn" onclick="uploadAndRun()">Upload & Measure</button>

    <div id="status"></div>
    <a class="back" href="/">← Back to power monitor</a>

    <script>
    let selectedPreset = 'both';
    let progressTimer = null;
    let elapsedTimer = null;
    let startTime = null;

    const STAGES = {{
        cpu:  ['Baseline', 'CPU encode', 'Done'],
        gpu:  ['Baseline', 'GPU encode', 'Done'],
        both: ['Baseline', 'CPU encode', 'Rest', 'Baseline 2', 'GPU encode', 'Done'],
    }};

    const STAGE_ICONS = {{
        done:    '✓',
        active:  '▶',
        pending: '·',
    }};

    function selectPreset(key) {{
        selectedPreset = key;
        ['cpu','gpu','both'].forEach(k => {{
            document.getElementById('preset-' + k).classList.toggle('selected', k === key);
        }});
    }}

    function formatElapsed(ms) {{
        const s = Math.floor(ms / 1000);
        const m = Math.floor(s / 60);
        return m > 0 ? `${{m}}m ${{s % 60}}s` : `${{s}}s`;
    }}

    function estimateStage(elapsedS, mode) {{
        // Rough stage boundaries in seconds
        if (mode === 'both') {{
            if (elapsedS < 10)  return 0; // baseline
            if (elapsedS < 80)  return 1; // cpu encode (est ~60s)
            if (elapsedS < 90)  return 2; // rest
            if (elapsedS < 100) return 3; // baseline 2
            if (elapsedS < 180) return 4; // gpu encode
            return 5;
        }} else {{
            if (elapsedS < 10) return 0;
            if (elapsedS < 120) return 1;
            return 2;
        }}
    }}

    function renderProgress(jobId, mode) {{
        const stages = STAGES[mode];
        startTime = Date.now();

        function update() {{
            const elapsed = Date.now() - startTime;
            const elapsedS = elapsed / 1000;
            const currentStage = estimateStage(elapsedS, mode);

            const stageHTML = stages.map((label, i) => {{
                let state = i < currentStage ? 'done' : i === currentStage ? 'active' : 'pending';
                let icon = state === 'done' ? '✓' : state === 'active' ? '▶' : '·';
                let iconColor = state === 'done' ? '#00ff99' : state === 'active' ? '#ffaa00' : '#333';
                return `<div class="stage ${{state}}">
                    <span class="stage-icon" style="color:${{iconColor}}">${{icon}}</span>
                    <span class="stage-label">${{label}}</span>
                </div>`;
            }}).join('');

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

        update();
        progressTimer = setInterval(update, 500);
    }}

    function stopProgress() {{
        if (progressTimer) {{ clearInterval(progressTimer); progressTimer = null; }}
    }}

    async function uploadAndRun() {{
        const file = document.getElementById('fileInput').files[0];
        if (!file) {{ alert('Please select a file first'); return; }}
        if (file.size > 1024 * 1024 * 1024) {{ alert('File too large (max 1GB)'); return; }}

        const btn = document.getElementById('runBtn');
        btn.disabled = true;
        document.getElementById('status').innerHTML =
            '<div style="color:#ffaa00">Uploading ' + file.name + '...</div>';

        const form = new FormData();
        form.append('file', file);
        form.append('preset', selectedPreset);

        try {{
            const resp = await fetch('/video/upload', {{ method: 'POST', body: form }});
            const data = await resp.json();
            if (data.job_id) {{
                renderProgress(data.job_id, selectedPreset);
                pollJob(data.job_id);
            }} else {{
                document.getElementById('status').innerHTML =
                    '<div style="color:#ff4400">Error: ' + JSON.stringify(data) + '</div>';
                btn.disabled = false;
            }}
        }} catch(e) {{
            document.getElementById('status').innerHTML =
                '<div style="color:#ff4400">Upload failed: ' + e + '</div>';
            btn.disabled = false;
        }}
    }}

    async function pollJob(jobId) {{
        try {{
            const resp = await fetch('/video/job/' + jobId);
            const data = await resp.json();
            if (data.status === 'done') {{
                stopProgress();
                renderResult(data.result);
                document.getElementById('runBtn').disabled = false;
            }} else if (data.status === 'error') {{
                stopProgress();
                document.getElementById('status').innerHTML =
                    '<div style="color:#ff4400">Error: ' + data.error + '</div>';
                document.getElementById('runBtn').disabled = false;
            }} else {{
                setTimeout(() => pollJob(jobId), 5000);
            }}
        }} catch(e) {{
            setTimeout(() => pollJob(jobId), 5000);
        }}
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

    function renderResult(r) {{
        const el = document.getElementById('status');
        const elapsed = startTime ? formatElapsed(Date.now() - startTime) : '';
        const elapsedNote = elapsed ? `<div style="color:#444;font-size:0.78rem;margin-bottom:1rem">
            Total elapsed: ${{elapsed}}</div>` : '';
        if (r.mode === 'both') {{
            el.innerHTML = elapsedNote + renderBoth(r);
        }} else {{
            el.innerHTML = elapsedNote + renderSingle(r.result);
        }}
    }}
    </script>
</body>
</html>"""

# --- Job runner ---

async def run_job(job_id: str, input_path: Path, preset: str):
    try:
        jobs[job_id] = {"status": "running"}
        if preset == "both":
            result = await run_both_measurement(input_path, job_id)
        else:
            result = await run_video_measurement(input_path, job_id, preset)
        jobs[job_id] = {"status": "done", "result": result}
    except Exception as e:
        jobs[job_id] = {"status": "error", "error": str(e)}
    finally:
        input_path.unlink(missing_ok=True)

@app.post("/video/upload")
async def upload_video(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    preset: str = Form("both")
):
    if preset not in ("cpu", "gpu", "both"):
        return JSONResponse({"error": "Invalid preset"}, status_code=400)
    if LOCK_FILE.exists():
        return JSONResponse(
            {"error": "GoS1 is busy with another measurement"}, status_code=409)

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

    background_tasks.add_task(run_job, job_id, input_path, preset)
    return {"job_id": job_id}

@app.get("/video/job/{job_id}")
async def job_status(job_id: str):
    return jobs.get(job_id, {"status": "not_found"})
