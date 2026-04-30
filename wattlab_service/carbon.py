"""
carbon.py — Energy → CO2e conversion.

Single home for everything carbon-related. The rest of the service treats
Wh × g/kWh as a black box: it calls `walk_and_enrich(result_dict)` at save
time, and the resulting JSON carries a `co2e` block on every `energy`
sub-dict so audit trail + UI rendering both have the data they need.

Fallback ladder (no exception path; always returns a usable number):
  1. Live — ElectricityMaps API value, fresh (< LIVE_TTL_S)   → source="live"
  2. Static — annual mean for the zone (Ember 2024)            → source="static"

Live vs estimated is an explicit field in the returned dict — the UI shows
a badge based on this so visitors know which they're looking at.

The home zone (where the GoS1 server lives) gets a background poller; the
comparison zones use the static table only, so their numbers don't drift
between page loads.
"""
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Read API token from .env at module import (siblings do the same).
try:
    from dotenv import dotenv_values
    _ENV = dotenv_values("/home/gos/wattlab/.env")
except Exception:
    _ENV = {}


# --- Config ---

# The zone the server actually runs in. Live polling targets this zone only.
HOME_ZONE = "FR"

# Cities shown in the comparison strip (in display order).
COMPARISON_ZONES = ["FR", "GB", "DE", "PL", "ES", "US", "CN"]

# Live cache freshness: values older than this are treated as stale and the
# fallback to the static annual mean kicks in. Applied to BOTH our cache hit
# time and the upstream source's own published timestamp (data_age_s).
LIVE_TTL_S = 30 * 60          # 30 minutes
POLL_INTERVAL_S = 5 * 60      # poll every 5 minutes
HTTP_TIMEOUT_S = 8.0

# Sources, in priority order for FR:
#   1. Eco2mix — RTE/Etalab official French TSO real-time data (no auth).
#      Already includes a precomputed `taux_co2` field, so we don't have to
#      compute carbon intensity from the production mix ourselves.
#   2. ElectricityMaps — third-party aggregator, requires token. Used as a
#      backup if Eco2mix is unreachable.
ECO2MIX_URL = (
    "https://odre.opendatasoft.com/api/explore/v2.1/catalog/datasets/"
    "eco2mix-national-tr/records"
)
ELECTRICITYMAPS_URL = "https://api.electricitymap.org/v3/carbon-intensity/latest"

# IPCC AR6 WGIII (2022) lifecycle median emission factors, gCO2eq/kWh.
# Used only as a sanity-check or fallback if `taux_co2` is missing from a
# given Eco2mix record. Eco2mix's own number is the authoritative output.
EMISSION_FACTORS = {
    "nucleaire":     12,
    "eolien":        11,
    "solaire":       45,
    "hydraulique":   24,
    "bioenergies":  230,
    "gaz":          490,
    "charbon":      820,
    "fioul":        650,
    "pompage":       24,   # pumped hydro storage — proxied with hydro factor
}
EMISSION_FACTORS_SOURCE = "IPCC AR6 WGIII (2022) lifecycle medians"


# Annual mean grid carbon intensity, gCO2eq/kWh.
# Source: Ember Yearly Electricity Data 2024 (ember-energy.org), lifecycle
# values where available, otherwise direct emissions. These are the fallback
# the UI shows as "estimated" and what comparison cities always use.
STATIC_INTENSITY = {
    "FR":    {"label": "Paris (France)",       "g_per_kwh": 53,  "year": 2024},
    "GB":    {"label": "London (UK)",          "g_per_kwh": 207, "year": 2024},
    "DE":    {"label": "Berlin (Germany)",     "g_per_kwh": 363, "year": 2024},
    "PL":    {"label": "Warsaw (Poland)",      "g_per_kwh": 597, "year": 2024},
    "ES":    {"label": "Madrid (Spain)",       "g_per_kwh": 154, "year": 2024},
    "NL":    {"label": "Amsterdam (NL)",       "g_per_kwh": 268, "year": 2024},
    "IE":    {"label": "Dublin (Ireland)",     "g_per_kwh": 287, "year": 2024},
    "IT":    {"label": "Rome (Italy)",         "g_per_kwh": 263, "year": 2024},
    "SE":    {"label": "Stockholm (Sweden)",   "g_per_kwh": 41,  "year": 2024},
    "NO":    {"label": "Oslo (Norway)",        "g_per_kwh": 30,  "year": 2024},
    "US":    {"label": "United States avg",    "g_per_kwh": 369, "year": 2024},
    "CN":    {"label": "China avg",            "g_per_kwh": 582, "year": 2024},
    "IN":    {"label": "India avg",            "g_per_kwh": 713, "year": 2024},
    "WORLD": {"label": "World average",        "g_per_kwh": 480, "year": 2024},
}
STATIC_SOURCE = "Ember 2024 annual mean"

# Live cache: {zone: {"g_per_kwh": float, "fetched_at": epoch_s, "ok": bool}}
_LIVE: dict = {}


def _token() -> Optional[str]:
    return _ENV.get("ELECTRICITYMAPS_TOKEN") or None


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def compute_intensity_from_mix(mix_mw: dict) -> Optional[float]:
    """Sanity-check / fallback path: derive gCO2/kWh from a {source: MW} dict
    using EMISSION_FACTORS. Returns None if total positive production is 0
    or nothing maps to a known factor."""
    total = 0.0
    weighted = 0.0
    for source, mw in mix_mw.items():
        if mw is None:
            continue
        try:
            mw = float(mw)
        except (TypeError, ValueError):
            continue
        if mw <= 0:
            continue
        ef = EMISSION_FACTORS.get(source)
        if ef is None:
            continue
        total += mw
        weighted += mw * ef
    if total <= 0:
        return None
    return weighted / total


# --- Live fetchers ---

async def _fetch_eco2mix(client) -> Optional[dict]:
    """RTE/Etalab Eco2mix real-time, gCO2eq/kWh + production mix in MW.

    Returns:
      {"g_per_kwh": float, "mix_mw": {...}, "source_datetime": str,
       "computed": bool}    — computed=True if `taux_co2` was missing and
                              we derived it from the mix.
    or None on any failure.

    Filters `where=taux_co2 IS NOT NULL` because the dataset pre-populates
    rows for upcoming intervals as NULL placeholders — the latest *real*
    record is the one we want.
    """
    try:
        r = await client.get(
            ECO2MIX_URL,
            params={
                "order_by": "date_heure desc",
                "where": "taux_co2 IS NOT NULL",
                "limit": 1,
            },
            timeout=HTTP_TIMEOUT_S,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        records = data.get("results") or []
        if not records:
            return None
        rec = records[0]
        mix = {k: rec.get(k) for k in EMISSION_FACTORS.keys() if rec.get(k) is not None}
        precomputed = rec.get("taux_co2")
        if isinstance(precomputed, (int, float)) and precomputed > 0:
            return {
                "g_per_kwh": float(precomputed),
                "mix_mw": mix,
                "source_datetime": rec.get("date_heure"),
                "computed": False,
            }
        derived = compute_intensity_from_mix(mix)
        if derived is None:
            return None
        return {
            "g_per_kwh": derived,
            "mix_mw": mix,
            "source_datetime": rec.get("date_heure"),
            "computed": True,
        }
    except Exception:
        return None


async def _fetch_electricitymaps(client, zone: str) -> Optional[float]:
    token = _token()
    if not token:
        return None
    try:
        r = await client.get(
            ELECTRICITYMAPS_URL,
            params={"zone": zone},
            headers={"auth-token": token},
            timeout=HTTP_TIMEOUT_S,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        v = data.get("carbonIntensity")
        if not isinstance(v, (int, float)):
            return None
        return float(v)
    except Exception:
        return None


async def poller(zones=(HOME_ZONE,), interval_s: int = POLL_INTERVAL_S):
    """Background task — refreshes _LIVE for the given zones every interval_s.

    Source priority for FR:
      1. Eco2mix (RTE/Etalab — authoritative French TSO data, no auth)
      2. ElectricityMaps (if token configured)

    Other zones fall straight through to ElectricityMaps. Failures keep the
    previous good value (or leave the entry absent); the request path
    never blocks on this — if no live value is available, the static table
    is used.
    """
    try:
        import httpx
    except ImportError:
        # No httpx available — everything resolves to static. Service still works.
        return
    while True:
        try:
            async with httpx.AsyncClient() as client:
                for z in zones:
                    fetched = None

                    # 1. Eco2mix — only meaningful for FR.
                    if z == "FR":
                        eco = await _fetch_eco2mix(client)
                        if eco is not None:
                            fetched = {
                                "g_per_kwh": eco["g_per_kwh"],
                                "fetched_at": time.time(),
                                "ok": True,
                                "provider": "Eco2mix (RTE/Etalab)",
                                "provider_url": "https://www.rte-france.com/eco2mix",
                                "mix_mw": eco["mix_mw"],
                                "computed": eco["computed"],
                                "source_datetime": eco["source_datetime"],
                            }

                    # 2. ElectricityMaps — backup for FR, primary for others.
                    if fetched is None:
                        v = await _fetch_electricitymaps(client, z)
                        if v is not None:
                            fetched = {
                                "g_per_kwh": v,
                                "fetched_at": time.time(),
                                "ok": True,
                                "provider": "ElectricityMaps",
                                "provider_url": "https://www.electricitymaps.com",
                            }

                    if fetched is not None:
                        _LIVE[z] = fetched
                    else:
                        # Mark unreachable but preserve any prior value.
                        existing = _LIVE.get(z, {})
                        _LIVE[z] = {**existing, "ok": False}
        except Exception:
            pass
        await asyncio.sleep(interval_s)


# --- Lookups ---

def intensity(zone: str = HOME_ZONE) -> dict:
    """Return current best-estimate intensity for `zone`. Always returns a
    usable value via the fallback ladder.

    For "live" results, `age_s` reflects the upstream source timestamp
    (`source_datetime`) when available, falling back to `fetched_at` —
    so the UI shows when RTE/ElectricityMaps actually published, not just
    when our cache was last refreshed."""
    z = zone.upper()
    live = _LIVE.get(z)
    if live and live.get("ok") and live.get("g_per_kwh") is not None:
        cache_age = time.time() - (live.get("fetched_at") or 0)
        # Compute data age from upstream timestamp if available.
        src_dt = _parse_iso(live.get("source_datetime"))
        if src_dt is not None:
            now = datetime.now(timezone.utc)
            data_age = (now - src_dt).total_seconds()
        else:
            data_age = cache_age
        # Fresh by both clocks.
        if cache_age < LIVE_TTL_S and data_age < LIVE_TTL_S:
            out = {
                "g_per_kwh": round(live["g_per_kwh"], 1),
                "source": "live",
                "fetched_at": live["fetched_at"],
                "age_s": int(data_age),
                "zone": z,
                "zone_label": STATIC_INTENSITY.get(z, {}).get("label", z),
                "provider": live.get("provider", "live"),
                "provider_url": live.get("provider_url"),
            }
            if live.get("mix_mw"):
                out["mix_mw"] = live["mix_mw"]
            if live.get("computed"):
                out["computed"] = True
            return out
    static = STATIC_INTENSITY.get(z) or STATIC_INTENSITY["WORLD"]
    return {
        "g_per_kwh": static["g_per_kwh"],
        "source": "static",
        "year": static.get("year"),
        "zone": z,
        "zone_label": static["label"],
        "provider": STATIC_SOURCE,
    }


def wh_to_co2e(wh: Optional[float], zone: str = HOME_ZONE) -> Optional[dict]:
    """Wh → gCO2e. Returns dict with `grams` and full intensity provenance."""
    if wh is None:
        return None
    try:
        wh_f = float(wh)
    except (TypeError, ValueError):
        return None
    i = intensity(zone)
    grams = (wh_f / 1000.0) * i["g_per_kwh"]
    # Round at nanogram precision — well below the P110 measurement floor.
    # Coarser rounding (3 decimals) silently truncates µg-scale values to 0,
    # which the UI then renders as "0 g". Display-layer formatting (fmtMass
    # in main.py) handles human-readable rounding from the full value.
    return {"grams": round(grams, 9), "intensity": i}


def enrich_energy(energy: dict, zone: str = HOME_ZONE) -> None:
    """Add a `co2e` block to an energy dict in place. Idempotent — overwrites
    any existing co2e block so re-enrichment picks up the latest intensity."""
    if not isinstance(energy, dict):
        return
    co2e = wh_to_co2e(energy.get("delta_e_wh"), zone)
    if co2e is not None:
        energy["co2e"] = co2e


def walk_and_enrich(obj, zone: str = HOME_ZONE) -> None:
    """Recursively walk a result dict, enriching every nested `energy` block.

    Single insertion point used by persist.save_result so every job type
    (video, llm, image, rag — single, both, all_codecs, all_both, batch,
    rag_compare, etc.) gets uniform CO2e enrichment without per-mode wiring.
    """
    if isinstance(obj, dict):
        if isinstance(obj.get("energy"), dict):
            enrich_energy(obj["energy"], zone)
        for v in obj.values():
            walk_and_enrich(v, zone)
    elif isinstance(obj, list):
        for v in obj:
            walk_and_enrich(v, zone)


def comparison_table(wh: Optional[float],
                     zones=COMPARISON_ZONES,
                     home_zone: str = HOME_ZONE) -> list:
    """Build comparison rows for a Wh figure. Home zone uses live (with
    static fallback); other zones always use static so values are stable
    across page loads."""
    if wh is None:
        return []
    rows = []
    for z in zones:
        if z == home_zone:
            i = intensity(z)  # may be live
        else:
            static = STATIC_INTENSITY.get(z) or STATIC_INTENSITY["WORLD"]
            i = {
                "g_per_kwh": static["g_per_kwh"],
                "source": "static",
                "year": static.get("year"),
                "zone": z,
                "zone_label": static["label"],
                "provider": STATIC_SOURCE,
            }
        grams = (float(wh) / 1000.0) * i["g_per_kwh"]
        row = {
            "zone": z,
            "label": i["zone_label"],
            "g_per_kwh": i["g_per_kwh"],
            "grams": round(grams, 9),
            "source": i["source"],
            "is_home": (z == home_zone),
        }
        if i.get("source") == "live":
            row["age_s"] = i.get("age_s")
        elif i.get("year"):
            row["year"] = i.get("year")
        rows.append(row)
    return rows


def status() -> dict:
    """Diagnostic snapshot for /carbon endpoint."""
    return {
        "home_zone": HOME_ZONE,
        "comparison_zones": COMPARISON_ZONES,
        "token_configured": bool(_token()),
        "home_intensity": intensity(HOME_ZONE),
        "live_cache": {
            z: {
                "g_per_kwh": v.get("g_per_kwh"),
                "ok": v.get("ok"),
                "age_s": int(time.time() - v["fetched_at"]) if v.get("fetched_at") else None,
            }
            for z, v in _LIVE.items()
        },
        "static_table": STATIC_INTENSITY,
        "static_source": STATIC_SOURCE,
        "live_ttl_s": LIVE_TTL_S,
        "poll_interval_s": POLL_INTERVAL_S,
    }
