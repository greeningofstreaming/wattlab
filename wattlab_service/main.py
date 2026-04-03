import asyncio
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from dotenv import dotenv_values
from tapo import ApiClient

config = dotenv_values("/home/gos/wattlab/.env")
app = FastAPI()

async def get_power_watts() -> float:
    client = ApiClient(config["TAPO_EMAIL"], config["TAPO_PASSWORD"])
    device = await client.p110(config["TAPO_P110_IP"])
    result = await device.get_energy_usage()
    return result.current_power / 1000

@app.get("/", response_class=HTMLResponse)
async def index():
    watts = await get_power_watts()
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>WattLab — GoS1 Power</title>
        <meta http-equiv="refresh" content="10">
        <style>
            body {{ font-family: monospace; background: #0a0a0a; color: #e0e0e0; 
                   display: flex; flex-direction: column; align-items: center; 
                   justify-content: center; height: 100vh; margin: 0; }}
            .watts {{ font-size: 6rem; color: #00ff99; font-weight: bold; }}
            .label {{ font-size: 1.2rem; color: #888; margin-top: 1rem; }}
            .scope {{ font-size: 0.8rem; color: #555; margin-top: 2rem; }}
        </style>
    </head>
    <body>
        <div class="watts">{watts:.1f} W</div>
        <div class="label">GoS1 current power draw</div>
        <div class="scope">Device layer only · P110 · refreshes every 10s</div>
    </body>
    </html>
    """

@app.get("/power")
async def power_json():
    watts = await get_power_watts()
    return {{"watts": watts, "scope": "device_only", "source": "tapo_p110"}}
