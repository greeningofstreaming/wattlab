"""
power.py — pluggable power measurement interface for WattLab.

Currently backed by a Tapo P110 smart plug polled via local Wi-Fi API.

To swap in a different power source (PDU, IPMI, another smart plug brand):
  replace get_power_watts() — keep the same signature and return type.
  Everything else (polling loops, baseline measurement, energy maths)
  lives in the individual measurement modules and needs no changes.
"""

import asyncio
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
