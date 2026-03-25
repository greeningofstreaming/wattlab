"""
Simple regressions and plots for video luma vs power.

Uses the aligned dataset from align_video_power.py:
  data_analysis/video_power_aligned.xlsx
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import statsmodels.api as sm


def fit_linear(df: pd.DataFrame, x_col: str, y_col: str):
    x = df[x_col].astype(float).values
    y = df[y_col].astype(float).values
    if len(x) < 3:
        return None
    X = sm.add_constant(x)
    model = sm.OLS(y, X).fit()
    return model


def scatter_with_fit(df, x_col, y_col, title, out_path):
    model = fit_linear(df, x_col, y_col)
    if model is None:
        return None

    x = df[x_col].astype(float).values
    y = df[y_col].astype(float).values
    x_line = np.linspace(x.min(), x.max(), 100)
    y_line = model.params[0] + model.params[1] * x_line

    plt.figure(figsize=(8, 6))
    plt.scatter(x, y, alpha=0.4, s=12)
    plt.plot(x_line, y_line, color="tab:red")
    plt.xlabel(x_col)
    plt.ylabel(y_col)
    plt.title(title)
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_path)
    plt.close()

    return model


def summarize_model(label, model, n):
    return {
        "label": label,
        "n_samples": n,
        "intercept": model.params[0],
        "slope": model.params[1],
        "r2": model.rsquared,
        "p_value": model.pvalues[1],
    }


def main():
    root = Path(__file__).resolve().parent.parent
    aligned_path = root / "data_analysis/video_power_aligned.xlsx"
    out_dir = root / "data/correlation_plots"
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_excel(aligned_path, sheet_name="aligned")

    metrics = [c for c in ["luma_mean", "grad_mean", "grad_std", "lap_var", "edge_density"] if c in df.columns]
    if not metrics:
        print("No video metrics found in aligned dataset.")
        return

    summaries_device = []

    # Device side: normalize per device, then aggregate
    dev_df = df[df["source"] == "device"].copy()
    if dev_df.empty:
        print("No device rows found in aligned dataset.")
        return

    baseline_label = "1080p30_6Mbps"
    baseline_means = (
        dev_df[dev_df["condition_label"] == baseline_label]
        .groupby("device_label")["power_value_W"]
        .mean()
    )
    dev_df["baseline_mean_W"] = dev_df["device_label"].map(baseline_means)
    dev_df["device_mean_W"] = dev_df.groupby("device_label")["power_value_W"].transform("mean")
    dev_df["device_std_W"] = dev_df.groupby("device_label")["power_value_W"].transform("std")

    dev_df["power_pct_vs_baseline"] = (
        (dev_df["power_value_W"] - dev_df["baseline_mean_W"]) / dev_df["baseline_mean_W"]
    )
    dev_df["power_z"] = (
        (dev_df["power_value_W"] - dev_df["device_mean_W"]) / dev_df["device_std_W"]
    )

    for y_col, label_suffix in [
        ("power_pct_vs_baseline", "pct_vs_baseline"),
        ("power_z", "zscore"),
    ]:
        for x_col in metrics:
            label = f"device_all_{label_suffix}_{x_col}"
            out_path = out_dir / f"{label}.pdf"
            model = scatter_with_fit(
                dev_df, x_col, y_col,
                title=f"Devices (normalized): {y_col} vs {x_col}",
                out_path=out_path,
            )
            if model is not None:
                summary = summarize_model(label, model, len(dev_df))
                summary["x_col"] = x_col
                summary["y_col"] = y_col
                summaries_device.append(summary)

    summary_df_device = pd.DataFrame(summaries_device)
    summary_path = root / "data/correlation_summary.xlsx"
    with pd.ExcelWriter(summary_path) as writer:
        summary_df_device.to_excel(writer, index=False, sheet_name="device_norm")
    print(f"Wrote regression summary to {summary_path}")
    print(f"Wrote plots to {out_dir}")


if __name__ == "__main__":
    main()
