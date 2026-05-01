"""
Unit tests for `carbon.py`.

This is the first automated test suite in the repo. Establishes the pattern
that the access-spine modules (audience.py / capabilities.py / queue_control.py)
will follow when they land:
  - Test the module's pure logic, not its HTTP integrations
  - Always include a *regression test* for any bug we've already shipped a fix
    for, named after the bug, so future readers see the lesson
  - Reset module-level state between cases (fixtures) to avoid test pollution

What's covered:
  - Static fallback when no live data is available (the always-works floor)
  - The grams-rounding regression that displayed "0 g" for tiny LLM tasks
  - Live cache freshness — both `fetched_at` (cache age) and `source_datetime`
    (upstream publication age)
  - `walk_and_enrich` on the actual nested result shapes from every job mode
    (single, both, all_codecs, batch, all, all_both, rag, rag_compare)
  - Idempotency: re-enriching an already-enriched tree doesn't double-wrap
  - `comparison_table` shape and the home-vs-comparison distinction
  - `compute_intensity_from_mix` — the IPCC AR6 fallback math when
    `taux_co2` is absent from an Eco2mix record

What's NOT covered (deliberately out of scope for this warm-up):
  - Live HTTP fetches against Eco2mix or ElectricityMaps. They're integration
    tests against external services; valuable but heavier to set up. Mock
    them when the spine refactor introduces an httpx-injection seam.
"""
import time
from datetime import datetime, timezone, timedelta

import pytest

import carbon


# --- Fixtures ---------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_live_cache():
    """Clear the module-level live cache before and after every test so
    tests can't pollute each other."""
    carbon._LIVE.clear()
    yield
    carbon._LIVE.clear()


# --- Static fallback (the always-works floor) -------------------------------

def test_intensity_static_for_known_zone():
    """No live cache → returns the Ember 2024 annual mean for the zone."""
    out = carbon.intensity("FR")
    assert out["source"] == "static"
    assert out["zone"] == "FR"
    assert out["g_per_kwh"] == carbon.STATIC_INTENSITY["FR"]["g_per_kwh"]
    assert out["year"] == 2024


def test_intensity_static_for_unknown_zone_falls_back_to_world():
    """Unknown zone → world average (never raise)."""
    out = carbon.intensity("XX")
    assert out["source"] == "static"
    assert out["g_per_kwh"] == carbon.STATIC_INTENSITY["WORLD"]["g_per_kwh"]


def test_intensity_zone_string_is_case_insensitive():
    assert carbon.intensity("fr")["zone"] == "FR"
    assert carbon.intensity("Fr")["zone"] == "FR"


# --- The "0 g" regression (CR-002 root cause we already shipped a fix for) -

def test_wh_to_co2e_preserves_microgram_precision():
    """REGRESSION: Before 2026-05-01, `wh_to_co2e` rounded grams to 3 decimal
    places. For a tiny LLM task (~0.001 Wh) on the French grid (~53 g/kWh
    static, ~11 g/kWh live), the resulting grams value (5.3e-5) truncated
    to 0.0, which the UI then rendered as '0 g'. The fix rounds at 9 decimal
    places (nanogram precision, well below P110 measurement floor).
    """
    out = carbon.wh_to_co2e(0.001, "FR")
    # FR static = 53 g/kWh → 0.001 Wh × 53 / 1000 = 5.3e-5 g
    assert out is not None
    assert out["grams"] > 0
    assert out["grams"] == pytest.approx(5.3e-5, rel=1e-6)


def test_wh_to_co2e_handles_none_and_invalid():
    assert carbon.wh_to_co2e(None) is None
    assert carbon.wh_to_co2e("not a number") is None
    assert carbon.wh_to_co2e(0) is not None  # 0 Wh is valid; means 0 grams
    assert carbon.wh_to_co2e(0)["grams"] == 0


# --- Live cache freshness ---------------------------------------------------

def test_intensity_uses_live_when_cache_fresh():
    carbon._LIVE["FR"] = {
        "g_per_kwh": 11.0,
        "fetched_at": time.time(),
        "ok": True,
        "provider": "Eco2mix (RTE/Etalab)",
    }
    out = carbon.intensity("FR")
    assert out["source"] == "live"
    assert out["g_per_kwh"] == 11.0
    assert out["provider"] == "Eco2mix (RTE/Etalab)"


def test_intensity_falls_back_to_static_when_cache_stale_by_clock():
    """fetched_at older than LIVE_TTL_S → fall back to static."""
    carbon._LIVE["FR"] = {
        "g_per_kwh": 11.0,
        "fetched_at": time.time() - (carbon.LIVE_TTL_S + 60),
        "ok": True,
        "provider": "Eco2mix (RTE/Etalab)",
    }
    out = carbon.intensity("FR")
    assert out["source"] == "static"


def test_intensity_falls_back_when_source_datetime_old():
    """fetched_at recent BUT upstream source_datetime old (e.g. RTE stopped
    publishing) → still fall back to static. Belt and braces."""
    old_iso = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    carbon._LIVE["FR"] = {
        "g_per_kwh": 11.0,
        "fetched_at": time.time(),  # we just polled
        "ok": True,
        "provider": "Eco2mix (RTE/Etalab)",
        "source_datetime": old_iso,  # but RTE's data is 5h old
    }
    out = carbon.intensity("FR")
    assert out["source"] == "static"


def test_intensity_ignores_failed_fetch_attempts():
    carbon._LIVE["FR"] = {"ok": False, "g_per_kwh": None}
    out = carbon.intensity("FR")
    assert out["source"] == "static"


# --- enrich_energy and walk_and_enrich --------------------------------------

def test_enrich_energy_adds_co2e_block():
    energy = {"delta_e_wh": 0.376, "w_base": 51.5}
    carbon.enrich_energy(energy, "FR")
    assert "co2e" in energy
    assert energy["co2e"]["grams"] > 0
    assert energy["co2e"]["intensity"]["zone"] == "FR"


def test_enrich_energy_handles_missing_delta_e_wh():
    """No delta_e_wh → no co2e block injected (don't fabricate)."""
    energy = {"w_base": 51.5}
    carbon.enrich_energy(energy, "FR")
    assert "co2e" not in energy


def test_walk_and_enrich_handles_video_single_shape():
    result = {
        "mode": "single",
        "result": {
            "energy": {"delta_e_wh": 0.376},
            "thermals": {"cpu_base": 50},
        },
    }
    carbon.walk_and_enrich(result)
    assert "co2e" in result["result"]["energy"]


def test_walk_and_enrich_handles_video_both_shape():
    result = {
        "mode": "both",
        "cpu": {"energy": {"delta_e_wh": 0.83}},
        "gpu": {"energy": {"delta_e_wh": 0.37}},
    }
    carbon.walk_and_enrich(result)
    assert result["cpu"]["energy"]["co2e"]["grams"] > 0
    assert result["gpu"]["energy"]["co2e"]["grams"] > 0


def test_walk_and_enrich_handles_all_codecs_shape():
    result = {
        "mode": "all_codecs",
        "codecs": {
            "h264": {
                "cpu": {"energy": {"delta_e_wh": 0.83}},
                "gpu": {"energy": {"delta_e_wh": 0.37}},
            },
            "h265": {
                "cpu": {"energy": {"delta_e_wh": 1.58}},
                "gpu": {"energy": {"delta_e_wh": 0.29}},
            },
            "av1": {
                "cpu": {"energy": {"delta_e_wh": 0.65}},
                "gpu": {"energy": {"delta_e_wh": 0.30}},
            },
        },
    }
    carbon.walk_and_enrich(result)
    for codec in ("h264", "h265", "av1"):
        for dev in ("cpu", "gpu"):
            assert "co2e" in result["codecs"][codec][dev]["energy"], (
                f"missing co2e on {codec}/{dev}"
            )


def test_walk_and_enrich_handles_llm_all_shape():
    result = {
        "mode": "all",
        "tasks": {
            "T1": {"energy": {"delta_e_wh": 0.001}},
            "T2": {"energy": {"delta_e_wh": 0.012}},
            "T3": {"energy": {"delta_e_wh": 0.080}},
        },
    }
    carbon.walk_and_enrich(result)
    for tk in ("T1", "T2", "T3"):
        assert "co2e" in result["tasks"][tk]["energy"]


def test_walk_and_enrich_handles_rag_compare_shape():
    result = {
        "mode": "rag_compare",
        "results": {
            "baseline":  {"energy": {"delta_e_wh": 0.020}},
            "rag":       {"energy": {"delta_e_wh": 0.034}},
            "rag_large": {"energy": {"delta_e_wh": 0.072}},
        },
    }
    carbon.walk_and_enrich(result)
    for mode in ("baseline", "rag", "rag_large"):
        assert "co2e" in result["results"][mode]["energy"]


def test_walk_and_enrich_is_idempotent():
    """Re-enriching an already-enriched tree should overwrite, not nest.
    Important: enrich_energy is called when result JSONs are re-saved,
    e.g. for partial-result snapshots during long jobs."""
    result = {"energy": {"delta_e_wh": 0.376}}
    carbon.walk_and_enrich(result)
    first_co2e = result["energy"]["co2e"].copy()
    carbon.walk_and_enrich(result)
    second_co2e = result["energy"]["co2e"]
    # Same shape — not nested co2e-inside-co2e
    assert "co2e" not in second_co2e
    assert second_co2e["grams"] == first_co2e["grams"]


def test_walk_and_enrich_handles_lists_in_tree():
    """Some result shapes (e.g. LLM batch `runs`) contain lists of dicts
    with energy blocks inside. The walker must descend into lists too."""
    result = {
        "mode": "batch",
        "runs": [
            {"run": 1, "energy": {"delta_e_wh": 0.05}},
            {"run": 2, "energy": {"delta_e_wh": 0.06}},
            {"run": 3, "energy": {"delta_e_wh": 0.07}},
        ],
    }
    carbon.walk_and_enrich(result)
    for run in result["runs"]:
        assert "co2e" in run["energy"]


def test_walk_and_enrich_does_not_corrupt_non_dict_energy():
    """Defensive: if a future result shape uses 'energy' as a label string,
    walker must NOT try to enrich it (would crash or corrupt the tree)."""
    result = {"label_energy": "high", "energy": "label-not-a-dict"}
    carbon.walk_and_enrich(result)
    # The string-valued 'energy' field is left untouched; no exception raised
    assert result["energy"] == "label-not-a-dict"


# --- comparison_table -------------------------------------------------------

def test_comparison_table_has_one_row_per_zone():
    rows = carbon.comparison_table(0.5)
    zones = [r["zone"] for r in rows]
    assert zones == carbon.COMPARISON_ZONES


def test_comparison_table_marks_home_zone():
    rows = carbon.comparison_table(0.5, home_zone="FR")
    home_rows = [r for r in rows if r["is_home"]]
    assert len(home_rows) == 1
    assert home_rows[0]["zone"] == "FR"


def test_comparison_table_uses_static_for_non_home_zones_even_if_live_cached():
    """Comparison cities must always render static so values don't drift
    between page loads. Belt-and-braces: even if (somehow) a live value
    is in the cache for GB, the comparison table should render Ember static."""
    carbon._LIVE["GB"] = {
        "g_per_kwh": 999,  # nonsense value
        "fetched_at": time.time(),
        "ok": True,
        "provider": "fake",
    }
    rows = carbon.comparison_table(1.0, home_zone="FR")
    gb_row = next(r for r in rows if r["zone"] == "GB")
    assert gb_row["source"] == "static"
    assert gb_row["g_per_kwh"] == carbon.STATIC_INTENSITY["GB"]["g_per_kwh"]
    assert gb_row["g_per_kwh"] != 999


def test_comparison_table_with_zero_wh():
    rows = carbon.comparison_table(0)
    for r in rows:
        assert r["grams"] == 0


def test_comparison_table_with_none_wh_returns_empty_list():
    assert carbon.comparison_table(None) == []


# --- compute_intensity_from_mix (IPCC AR6 fallback when taux_co2 missing) --

def test_compute_intensity_from_mix_pure_nuclear():
    """100% nuclear → ~12 g/kWh (the IPCC AR6 lifecycle median we use)."""
    out = carbon.compute_intensity_from_mix({"nucleaire": 50000})
    assert out == pytest.approx(12.0)


def test_compute_intensity_from_mix_weighted_blend():
    """Half nuclear, half gas → weighted average of factors."""
    out = carbon.compute_intensity_from_mix({"nucleaire": 1000, "gaz": 1000})
    expected = (1000 * 12 + 1000 * 490) / 2000
    assert out == pytest.approx(expected)


def test_compute_intensity_from_mix_ignores_negative_and_zero():
    """Pumped-storage in discharge mode reports negative MW; sources at zero
    contribute nothing. Both should be ignored, not crash."""
    out = carbon.compute_intensity_from_mix({
        "nucleaire": 40000,
        "charbon": 0,        # idle
        "pompage": -500,     # discharging (negative)
    })
    assert out == pytest.approx(12.0)


def test_compute_intensity_from_mix_returns_none_when_no_production():
    assert carbon.compute_intensity_from_mix({}) is None
    assert carbon.compute_intensity_from_mix({"charbon": 0}) is None
