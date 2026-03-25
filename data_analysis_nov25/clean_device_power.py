"""
Tidy the device-side power measurements from the "Device Power Consumption-2025-11-03 Tests.xlsx"
workbook so they line up with the same condition ordering used in clean_data.py.

Usage:
    python data_analysis/clean_device_power.py
"""

import os
import re
from pathlib import Path
from typing import Dict, Tuple

import numpy as np
import pandas as pd

# Keep the same ordering/labels as clean_data.py
CONDITION_ORDER = [
    "1080p30_6Mbps",
    "1080p30_12Mbps",
    "1080p30_1Mbps",
    "270p30_6Mbps",
    "540p30_6Mbps",
]

CONDITION_DEFS = {
    "1080p30_6Mbps": {"resolution": "1080p", "framerate": 30, "bitrate_Mbps": 6},
    "1080p30_12Mbps": {"resolution": "1080p", "framerate": 30, "bitrate_Mbps": 12},
    "1080p30_1Mbps": {"resolution": "1080p", "framerate": 30, "bitrate_Mbps": 1},
    "270p30_6Mbps": {"resolution": "270p", "framerate": 30, "bitrate_Mbps": 6},
    "540p30_6Mbps": {"resolution": "540p", "framerate": 30, "bitrate_Mbps": 6},
}


def _parse_device_column(col: str) -> Tuple[str, str, int]:
    """
    Extract (device_label, condition_label, condition_index) from a column name.

    The columns are like "Ben-T1", "S-T3", "Arian-Y4", etc. The trailing digit
    (1–5) selects the condition using CONDITION_ORDER. Some columns have an
    extra letter before the digit ("Y4"); we ignore that letter.
    """
    col = str(col).strip()
    m = re.match(r"^(?P<device>.+?)-[A-Za-z]*?(?P<idx>[1-5])$", col)
    if not m:
        raise ValueError(f"Could not parse device/condition from column '{col}'")

    device_label = m.group("device")
    cond_idx = int(m.group("idx")) - 1
    if cond_idx < 0 or cond_idx >= len(CONDITION_ORDER):
        raise ValueError(f"Test index out of range for column '{col}'")

    condition_label = CONDITION_ORDER[cond_idx]
    return device_label, condition_label, cond_idx + 1


def tidy_device_power_sheet(
    excel_path: str,
    sheet_name: str = "All Tests",
    experiment_id: str = "devicePower20251103",
) -> pd.DataFrame:
    """
    Reshape the device-side power sheet into tidy form.

    Returns a DataFrame with columns:
      - timestamp (datetime64)
      - t_rel_s (seconds from first timestamp)
      - sample_idx (int)
      - device_label (e.g., "Ben", "S", "Arian", "Plazma")
      - condition_label (same ordering as clean_data.CONDITION_ORDER)
      - condition_number (1..5)
      - power_W (numeric)
      - resolution / framerate / bitrate_Mbps (from CONDITION_DEFS)
      - experiment_id, sheet
    """
    raw = pd.read_excel(excel_path, sheet_name=sheet_name)

    # Identify measurement columns (everything except time/sample helpers)
    time_col_candidates = [c for c in raw.columns if str(c).lower().startswith("time")]
    if not time_col_candidates:
        raise ValueError("No timestamp column found")
    time_col = time_col_candidates[0]

    sample_col_candidates = [c for c in raw.columns if "unnamed" in str(c).lower()]
    sample_col = sample_col_candidates[0] if sample_col_candidates else None

    measurement_cols = [
        c
        for c in raw.columns
        if c not in {time_col, sample_col}
    ]

    tidy_blocks = []

    # Normalize timestamps and sample indices once
    base = raw.copy()
    base = base.rename(columns={time_col: "timestamp"})
    base["timestamp"] = pd.to_datetime(base["timestamp"], errors="coerce")
    if base["timestamp"].notna().any():
        t0 = base["timestamp"].min(skipna=True)
        base["t_rel_s"] = (base["timestamp"] - t0).dt.total_seconds()
    else:
        base["t_rel_s"] = np.nan

    if sample_col:
        base = base.rename(columns={sample_col: "sample_idx"})
    else:
        base["sample_idx"] = np.arange(1, len(base) + 1)

    for col in measurement_cols:
        device_label, condition_label, condition_number = _parse_device_column(col)

        block = base[["timestamp", "t_rel_s", "sample_idx", col]].copy()
        block = block.rename(columns={col: "power_W"})
        block["power_W"] = pd.to_numeric(block["power_W"], errors="coerce")
        block = block.dropna(subset=["power_W"])

        block["device_label"] = device_label
        block["condition_label"] = condition_label
        block["condition_number"] = condition_number
        block["experiment_id"] = experiment_id
        block["sheet"] = sheet_name

        # Attach condition metadata
        meta: Dict[str, object] = CONDITION_DEFS[condition_label]
        for k, v in meta.items():
            block[k] = v

        tidy_blocks.append(block)

    tidy = pd.concat(tidy_blocks, ignore_index=True)

    tidy["condition_label"] = tidy["condition_label"].astype("category")
    tidy["device_label"] = tidy["device_label"].astype("category")
    tidy["resolution"] = tidy["resolution"].astype("category")
    tidy["bitrate_Mbps"] = tidy["bitrate_Mbps"].astype("int64")
    tidy["framerate"] = tidy["framerate"].astype("int64")

    tidy = tidy.sort_values(
        ["device_label", "condition_number", "t_rel_s", "sample_idx"]
    ).reset_index(drop=True)

    return tidy


def save_tidy_and_summary(
    tidy_df: pd.DataFrame,
    output_path: str,
) -> None:
    """
    Save the tidy data plus a simple per-device/per-condition summary.
    """
    summary = (
        tidy_df
        .groupby(["device_label", "condition_label"])
        .agg(
            n_samples=("power_W", "count"),
            mean_power_W=("power_W", "mean"),
            std_power_W=("power_W", "std"),
        )
        .reset_index()
        .sort_values(["device_label", "condition_label"])
    )

    with pd.ExcelWriter(output_path) as writer:
        tidy_df.to_excel(writer, index=False, sheet_name="tidy")
        summary.to_excel(writer, index=False, sheet_name="summary_per_device")


if __name__ == "__main__":
    root_path = Path(__file__).resolve().parent.parent / "data"
    excel_name = "Device Power Consumption-2025-11-03 Tests.xlsx"
    excel_path = root_path / excel_name

    tidy_df = tidy_device_power_sheet(
        str(excel_path),
        sheet_name="All Tests",
        experiment_id="devicePower20251103",
    )

    output_name = "devicePower20251103_tidy.xlsx"
    output_path = root_path / output_name
    save_tidy_and_summary(tidy_df, str(output_path))

    print(f"Tidy rows: {len(tidy_df)} written to {output_path}")
    print("Columns:", list(tidy_df.columns))
