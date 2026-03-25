import pandas as pd
import numpy as np
import re
from pathlib import Path
# -------------------------------------------------------------------
# 1. Define condition metadata
# -------------------------------------------------------------------

# Order of conditions as they appear in the Excel columns
CONDITION_ORDER = [
    "1080p30_6Mbps",
    "1080p30_12Mbps",
    "1080p30_1Mbps",
    "270p30_6Mbps",
    "540p30_6Mbps",    
]

# Mapping from label -> experimental factors
CONDITION_DEFS = {
    "1080p30_6Mbps":  {"resolution": "1080p", "framerate": 30, "bitrate_Mbps": 6},
    "1080p30_12Mbps": {"resolution": "1080p", "framerate": 30, "bitrate_Mbps": 12},
    "1080p30_1Mbps":  {"resolution": "1080p", "framerate": 30, "bitrate_Mbps": 1},
    "270p30_6Mbps":   {"resolution": "270p",  "framerate": 30, "bitrate_Mbps": 6},
    "540p30_6Mbps":   {"resolution": "540p",  "framerate": 30, "bitrate_Mbps": 6},

}

# The metrics that repeat for each condition in that sheet
METRIC_ORDER = ["Sample", "Time", "Voltage cV", "En cA", "Pck cA", "~"]


# -------------------------------------------------------------------
# 2. Helper to normalize column names
#    e.g. " Voltage cV.2" -> "Voltage cV"
# -------------------------------------------------------------------
def _norm_metric(col: str) -> str:
    """
    Remove the .1, .2 suffixes and trim whitespace.
    """
    c = str(col)
    c = re.sub(r"\.\d+$", "", c)  # drop .1, .2, ...
    c = c.strip()
    return c


# -------------------------------------------------------------------
# 3. Main tidy-up function
# -------------------------------------------------------------------
def tidy_server_power_sheet(
    excel_path: str,
    sheet_name: str = "serverPower20251103-1659",
    experiment_id: str = "serverPower20251103",
    device_role: str = "server",  # e.g. "encoder", "packager", etc.
) -> pd.DataFrame:
    """
    Load the given sheet and reshape it into a tidy DataFrame.

    One row = one measurement for one condition at one time.

    Adds:
      - power_enc_W = voltage_cV * en_cA  / 10000
      - power_pck_W = voltage_cV * pck_cA / 10000
    """

    raw = pd.read_excel(excel_path, sheet_name=sheet_name)

    # 3.1. Find where each condition block starts:
    #      every time we see a "Sample" column (Sample, Sample .1, Sample .2, ...)
    sample_indices = [
        idx for idx, c in enumerate(raw.columns) if _norm_metric(c) == "Sample"
    ]

    # Sanity check: we expect exactly 5 conditions in this sheet
    if len(sample_indices) != len(CONDITION_ORDER):
        raise ValueError(
            f"Expected {len(CONDITION_ORDER)} condition blocks, "
            f"found {len(sample_indices)} at indices {sample_indices}"
        )

    tidy_blocks = []

    # 3.2. Loop over each condition block
    for cond_idx, start_idx in enumerate(sample_indices):
        cond_label = CONDITION_ORDER[cond_idx]

        # Columns for this condition: Sample, Time, Voltage cV, En cA, Pck cA, ~
        cols = list(raw.columns[start_idx : start_idx + len(METRIC_ORDER)])
        block = raw[cols].copy()

        # Normalize the column names (remove suffixes, trim spaces)
        rename_map = {c: _norm_metric(c) for c in cols}
        block = block.rename(columns=rename_map)

        # Drop rows where there is no sample index for this condition
        block = block.dropna(subset=["Sample"])

        # Standardize to nice snake_case names
        block = block.rename(
            columns={
                "Sample": "sample_idx",
                "Time": "timestamp",
                "Voltage cV": "voltage_cV",
                "En cA": "en_cA",
                "Pck cA": "pck_cA",
                "~": "misc",
            }
        )

        # --- Make sure these are numeric before computing power ---
        for col in ["voltage_cV", "en_cA", "pck_cA"]:
            block[col] = pd.to_numeric(block[col], errors="coerce")

        # Parse time-of-day, stripping inner whitespace like "  16:59:01 "
        block["timestamp"] = pd.to_datetime(
            block["timestamp"].astype(str).str.strip(),
            format="%H:%M:%S",
            errors="coerce",
        )

        # Relative time (seconds from first non-NaT timestamp in this condition)
        if block["timestamp"].notna().any():
            t0 = block["timestamp"].min(skipna=True)
            block["t_rel_s"] = (block["timestamp"] - t0).dt.total_seconds()
        else:
            block["t_rel_s"] = np.nan

        # --- Compute power for encoder and packager ---
        # encoder power: V * I_enc / 10000
        block["power_enc_W"] = block["voltage_cV"] * block["en_cA"] / 10000.0

        # packager power: V * I_pck / 10000
        block["power_pck_W"] = block["voltage_cV"] * block["pck_cA"] / 10000.0

        # Attach condition label and experiment metadata
        block["condition_label"] = cond_label
        block["experiment_id"] = experiment_id
        block["sheet"] = sheet_name
        block["device_role"] = device_role

        # Attach resolution / bitrate / framerate
        meta = CONDITION_DEFS[cond_label]
        for k, v in meta.items():
            block[k] = v

        tidy_blocks.append(block)

    # 3.3. Concatenate all conditions
    tidy = pd.concat(tidy_blocks, ignore_index=True)

    # 3.4. Set dtypes and sort nicely
    tidy["condition_label"] = tidy["condition_label"].astype("category")
    tidy["resolution"] = tidy["resolution"].astype("category")
    tidy["bitrate_Mbps"] = tidy["bitrate_Mbps"].astype("int64")
    tidy["framerate"] = tidy["framerate"].astype("int64")

    tidy = tidy.sort_values(
        ["condition_label", "t_rel_s", "sample_idx"]
    ).reset_index(drop=True)

    return tidy


if __name__ == "__main__":
    # Adjust the path to where your Excel file actually lives
    root_path = Path(__file__).resolve().parent.parent / "data"
    excel_path = root_path / "serverPower20251103 Analysis.xlsx"

    tidy_df = tidy_server_power_sheet(
        str(excel_path),
        sheet_name="serverPower20251103-1659",
        experiment_id="serverPower20251103",
        device_role="server",
    )

    output_path = root_path / "serverPower20251103_tidy.xlsx"
    tidy_df.to_excel(output_path, index=False)

    print(f"Wrote tidy server power data to {output_path}")
    print(tidy_df.head())
