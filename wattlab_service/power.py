"""
power.py — pluggable power + telemetry interface for WattLab.

Currently backed by a Tapo P110 smart plug polled via local Wi-Fi API
(wall power) plus lm-sensors (CPU + GPU temperatures and GPU PPT).

To swap in a different power source (PDU, IPMI, another smart plug brand):
  replace get_power_watts() — keep the same signature and return type.
  Everything else (polling loops, baseline measurement, energy maths)
  lives in the individual measurement modules and needs no changes.
"""

import asyncio
import json
import subprocess
from dotenv import dotenv_values
from tapo import ApiClient

_config = dotenv_values("/home/gos/wattlab/.env")


async def get_power_watts() -> float:
    """Return current system power draw in watts. Retries 3× on transient errors."""
    for attempt in range(3):
        try:
            client = ApiClient(_config["TAPO_EMAIL"], _config["TAPO_PASSWORD"])
            device = await client.p110(_config["TAPO_P110_IP"])
            result = await device.get_energy_usage()
            return result.current_power / 1000
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(1)


def read_sensors_dict() -> dict:
    """One-shot read of lm-sensors: CPU Tctl, GPU junction temp, GPU PPT (W).
    Returns None for any value that can't be parsed. Safe to call frequently
    (subprocess is ~10ms). Used by both live UI telemetry and per-measurement
    modules (which have their own read_sensors wrappers for historical reasons).
    """
    try:
        result = subprocess.run(['sensors', '-j'], capture_output=True, text=True)
        data = json.loads(result.stdout)
        cpu = data.get('k10temp-pci-00c3', {}).get('Tctl', {}).get('temp1_input')
        gpu_junc = data.get('amdgpu-pci-0300', {}).get('junction', {}).get('temp2_input')
        gpu_ppt = data.get('amdgpu-pci-0300', {}).get('PPT', {}).get('power1_average')
        return {
            "cpu_tctl": cpu,
            "gpu_junction": gpu_junc,
            "gpu_ppt_w": gpu_ppt,
        }
    except Exception:
        return {"cpu_tctl": None, "gpu_junction": None, "gpu_ppt_w": None}
