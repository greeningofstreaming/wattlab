"""
Analysis and plotting for device-side power data.

Requires the tidy file produced by clean_device_power.py
(`devicePower20251103_tidy.xlsx`). If missing, it will be regenerated
from the raw Excel before plotting.
"""

import os
from pathlib import Path
import subprocess

import matplotlib

# Headless backend avoids GUI pop-ups/blocks
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import statsmodels.api as sm
from statsmodels.formula.api import ols

from clean_device_power import tidy_device_power_sheet


def load_or_build_tidy(root_path: Path) -> pd.DataFrame:
    tidy_path = root_path / "devicePower20251103_tidy.xlsx"
    raw_excel = root_path / "Device Power Consumption-2025-11-03 Tests.xlsx"

    if tidy_path.exists():
        return pd.read_excel(tidy_path)

    tidy_df = tidy_device_power_sheet(
        str(raw_excel),
        sheet_name="All Tests",
        experiment_id="devicePower20251103",
    )
    tidy_df.to_excel(tidy_path, index=False)
    return tidy_df


def write_pandoc_outputs(report_path: Path, pdf_path: Path, tex_path: Path) -> bool:
    try:
        subprocess.run(
            [
                "pandoc",
                str(report_path),
                "-s",
                "-o",
                str(pdf_path),
                "--pdf-engine=pdflatex",
                "--resource-path",
                str(report_path.parent),
            ],
            check=True,
        )
        subprocess.run(
            [
                "pandoc",
                str(report_path),
                "-s",
                "-o",
                str(tex_path),
                "--resource-path",
                str(report_path.parent),
            ],
            check=True,
        )
    except FileNotFoundError:
        print("pandoc not found; skipping PDF/Tex generation.")
        return False
    except subprocess.CalledProcessError as exc:
        print(f"pandoc failed: {exc}")
        return False
    return True


def trim_and_smooth(tidy_df: pd.DataFrame) -> pd.DataFrame:
    """
    For each device, align conditions to the minimal sample count,
    reset relative time per condition, and add smoothed power.
    """
    trimmed_blocks = []

    for device in tidy_df["device_label"].unique():
        sub = tidy_df[tidy_df["device_label"] == device].copy()

        target_len = sub.groupby("condition_label")["sample_idx"].count().min()
        trimmed = (
            sub.groupby("condition_label", group_keys=False)
            .tail(target_len)
            .copy()
        )
        trimmed["t_rel_s"] = (
            trimmed.groupby("condition_label")["t_rel_s"]
            .transform(lambda s: s - s.min())
        )
        trimmed["sample_idx_aligned"] = (
            trimmed.groupby("condition_label").cumcount()
        )
        trimmed_blocks.append(trimmed)

    power_df = pd.concat(trimmed_blocks, ignore_index=True)
    power_df = power_df.sort_values(
        ["device_label", "condition_label", "t_rel_s"]
    )

    SMOOTH_WINDOW = 7
    power_df["power_smooth_W"] = (
        power_df
        .groupby(["device_label", "condition_label"])["power_W"]
        .transform(
            lambda s: s.rolling(
                window=SMOOTH_WINDOW, center=True, min_periods=1
            ).mean()
        )
    )
    # Normalize within device
    baseline_label = "1080p30_6Mbps"
    baseline_means = (
        power_df[power_df["condition_label"] == baseline_label]
        .groupby("device_label")["power_W"]
        .mean()
    )
    power_df["baseline_mean_W"] = power_df["device_label"].map(baseline_means)

    power_df["device_mean_W"] = power_df.groupby("device_label")["power_W"].transform("mean")
    power_df["device_std_W"] = power_df.groupby("device_label")["power_W"].transform("std")

    power_df["power_pct_vs_baseline"] = (
        (power_df["power_W"] - power_df["baseline_mean_W"]) / power_df["baseline_mean_W"]
    )
    power_df["power_z"] = (power_df["power_W"] - power_df["device_mean_W"]) / power_df["device_std_W"]

    return power_df


def plot_time_series(power_df: pd.DataFrame, root_path: Path) -> None:
    """
    Create bitrate and resolution sweeps per device.
    """
    bitrate_order = [1, 6, 12]
    colors = {1: "tab:blue", 6: "tab:orange", 12: "tab:green"}
    res_order = ["1080p", "540p", "270p"]
    colors_res = {"1080p": "tab:blue", "540p": "tab:orange", "270p": "tab:green"}

    for device in power_df["device_label"].unique():
        sub_dev = power_df[power_df["device_label"] == device]

        # 1080p, vary bitrate
        mask_1080 = sub_dev["resolution"] == "1080p"
        df_1080 = sub_dev[mask_1080]

        plt.figure(figsize=(10, 6))
        for br in bitrate_order:
            sub = df_1080[df_1080["bitrate_Mbps"] == br]
            if sub.empty:
                continue
            plt.plot(
                sub["t_rel_s"],
                sub["power_W"],
                linestyle=":",
                alpha=0.3,
                color=colors[br],
                label=f"{br} Mbps raw",
            )
            plt.plot(
                sub["t_rel_s"],
                sub["power_smooth_W"],
                linestyle="-",
                color=colors[br],
                label=f"{br} Mbps smoothed",
            )

        plt.xlabel("Time (s)")
        plt.ylabel("Power (W)")
        plt.title(f"{device} power vs time – 1080p30, varying bitrate")
        plt.legend()
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(os.path.join(root_path, f"{device}_1080p_bitrate.pdf"))
        plt.close()

        # 6 Mbps, vary resolution
        mask_6Mbps = sub_dev["bitrate_Mbps"] == 6
        df_6Mbps = sub_dev[mask_6Mbps]

        plt.figure(figsize=(10, 6))
        for res in res_order:
            sub = df_6Mbps[df_6Mbps["resolution"] == res]
            if sub.empty:
                continue
            plt.plot(
                sub["t_rel_s"],
                sub["power_W"],
                linestyle=":",
                alpha=0.3,
                color=colors_res[res],
                label=f"{res} raw",
            )
            plt.plot(
                sub["t_rel_s"],
                sub["power_smooth_W"],
                linestyle="-",
                color=colors_res[res],
                label=f"{res} smoothed",
            )

        plt.xlabel("Time (s)")
        plt.ylabel("Power (W)")
        plt.title(f"{device} power vs time – 6 Mbps, varying resolution")
        plt.legend()
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(os.path.join(root_path, f"{device}_6mbps_resolution.pdf"))
        plt.close()


def plot_means(power_df: pd.DataFrame, root_path: Path) -> None:
    """
    Bar plot: mean power per condition for each device.
    """
    stats = (
        power_df
        .groupby(["device_label", "condition_label"])["power_W"]
        .agg(["mean", "std"])
        .reset_index()
    )

    devices = stats["device_label"].unique()
    conditions = stats["condition_label"].unique()

    x = np.arange(len(conditions))
    width = 0.18

    plt.figure(figsize=(12, 6))
    for i, device in enumerate(devices):
        sub = stats[stats["device_label"] == device]
        means = sub["mean"].values
        stds = sub["std"].values
        plt.bar(
            x + i * width,
            means,
            width=width,
            yerr=stds,
            capsize=3,
            label=device,
        )

    plt.xticks(x + width * (len(devices) - 1) / 2, conditions, rotation=30, ha="right")
    plt.ylabel("Average power (W)")
    plt.title("Average device power per condition (± std)")
    plt.grid(axis="y", alpha=0.3)
    plt.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(root_path, "device_avg_power_per_condition.pdf"))
    plt.close()


def plot_aggregated_normalized(agg_norm: pd.DataFrame, root_path: Path) -> None:
    """
    Bar charts for aggregated normalized stats across devices.
    """
    x = np.arange(len(agg_norm))
    labels = agg_norm["condition_label"].tolist()

    plt.figure(figsize=(10, 6))
    plt.bar(
        x,
        agg_norm["mean_pct_vs_baseline"],
        yerr=agg_norm["std_pct_vs_baseline"],
        capsize=5,
    )
    plt.xticks(x, labels, rotation=30, ha="right")
    plt.ylabel("Mean % change vs 1080p30_6Mbps")
    plt.title("Aggregated device power (percent change vs baseline)")
    plt.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(os.path.join(root_path, "device_norm_pct_vs_baseline.pdf"))
    plt.close()

    plt.figure(figsize=(10, 6))
    plt.bar(
        x,
        agg_norm["mean_z"],
        yerr=agg_norm["std_z"],
        capsize=5,
    )
    plt.xticks(x, labels, rotation=30, ha="right")
    plt.ylabel("Mean z-score (per device)")
    plt.title("Aggregated device power (z-score normalization)")
    plt.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(os.path.join(root_path, "device_norm_zscore.pdf"))
    plt.close()


def plot_aggregated_normalized_time(power_df: pd.DataFrame, root_path: Path) -> None:
    """
    Plot aggregated normalized device power vs time per condition.
    Shows raw and smoothed curves (mean across devices).
    """
    SMOOTH_WINDOW = 7

    agg = (
        power_df
        .groupby(["condition_label", "t_rel_s"])
        .agg(
            mean_pct=("power_pct_vs_baseline", "mean"),
        )
        .reset_index()
        .sort_values(["condition_label", "t_rel_s"])
    )

    agg["mean_pct_smooth"] = (
        agg
        .groupby("condition_label")["mean_pct"]
        .transform(
            lambda s: s.rolling(
                window=SMOOTH_WINDOW, center=True, min_periods=1
            ).mean()
        )
    )

    bitrate_conditions = ["1080p30_1Mbps", "1080p30_6Mbps", "1080p30_12Mbps"]
    resolution_conditions = ["1080p30_6Mbps", "540p30_6Mbps", "270p30_6Mbps"]

    colors_bitrate = {
        "1080p30_1Mbps": "tab:blue",
        "1080p30_6Mbps": "tab:orange",
        "1080p30_12Mbps": "tab:green",
    }
    colors_res = {
        "1080p30_6Mbps": "tab:blue",
        "540p30_6Mbps": "tab:orange",
        "270p30_6Mbps": "tab:green",
    }

    # Bitrate sweep at 1080p
    plt.figure(figsize=(10, 6))
    for cond in bitrate_conditions:
        sub = agg[agg["condition_label"] == cond]
        if sub.empty:
            continue
        plt.plot(
            sub["t_rel_s"],
            sub["mean_pct"],
            linestyle=":",
            alpha=0.3,
            color=colors_bitrate.get(cond, None),
            label=f"{cond} raw",
        )
        plt.plot(
            sub["t_rel_s"],
            sub["mean_pct_smooth"],
            linestyle="-",
            color=colors_bitrate.get(cond, None),
            label=f"{cond} smoothed",
        )

    plt.xlabel("Time (s)")
    plt.ylabel("Mean % change vs baseline")
    plt.title("Aggregated device power vs time – 1080p, varying bitrate")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(os.path.join(root_path, "device_norm_time_bitrate.pdf"))
    plt.close()

    # Resolution sweep at 6 Mbps
    plt.figure(figsize=(10, 6))
    for cond in resolution_conditions:
        sub = agg[agg["condition_label"] == cond]
        if sub.empty:
            continue
        plt.plot(
            sub["t_rel_s"],
            sub["mean_pct"],
            linestyle=":",
            alpha=0.3,
            color=colors_res.get(cond, None),
            label=f"{cond} raw",
        )
        plt.plot(
            sub["t_rel_s"],
            sub["mean_pct_smooth"],
            linestyle="-",
            color=colors_res.get(cond, None),
            label=f"{cond} smoothed",
        )

    plt.xlabel("Time (s)")
    plt.ylabel("Mean % change vs baseline")
    plt.title("Aggregated device power vs time – 6 Mbps, varying resolution")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(os.path.join(root_path, "device_norm_time_resolution.pdf"))
    plt.close()


def compute_anovas(power_df: pd.DataFrame):
    """
    Compute simple one-way ANOVAs per device:
      - Resolution effect at 6 Mbps
      - Bitrate effect at 1080p
    Returns list of dicts with tables and eta squared values.
    """
    results = []
    for device in sorted(power_df["device_label"].unique()):
        dev_res = {}
        sub = power_df[power_df["device_label"] == device]

        df_res = sub[sub["bitrate_Mbps"] == 6]
        if df_res["resolution"].nunique() >= 2:
            model = ols("power_W ~ C(resolution)", data=df_res).fit()
            table = sm.stats.anova_lm(model, typ=2)
            ss_effect = table.loc["C(resolution)", "sum_sq"]
            ss_resid = table.loc["Residual", "sum_sq"]
            eta_sq = ss_effect / (ss_effect + ss_resid)
            p_value = table.loc["C(resolution)", "PR(>F)"]
            dev_res["resolution"] = (table, eta_sq, p_value)

        df_br = sub[sub["resolution"] == "1080p"]
        if df_br["bitrate_Mbps"].nunique() >= 2:
            model_br = ols("power_W ~ C(bitrate_Mbps)", data=df_br).fit()
            table_br = sm.stats.anova_lm(model_br, typ=2)
            ss_effect = table_br.loc["C(bitrate_Mbps)", "sum_sq"]
            ss_resid = table_br.loc["Residual", "sum_sq"]
            eta_sq_br = ss_effect / (ss_effect + ss_resid)
            p_value_br = table_br.loc["C(bitrate_Mbps)", "PR(>F)"]
            dev_res["bitrate"] = (table_br, eta_sq_br, p_value_br)

        if dev_res:
            results.append((device, dev_res))
    return results


def main() -> None:
    root_path = Path(__file__).resolve().parent.parent / "data"
    tidy_df = load_or_build_tidy(root_path)

    # Basic stats per device/condition
    stats_per_device = (
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
    print("\n=== Stats per device & condition ===")
    print(stats_per_device)

    power_df = trim_and_smooth(tidy_df)
    print("\nAligned samples per device/condition:")
    print(power_df.groupby(["device_label", "condition_label"]).size())

    plot_time_series(power_df, root_path)
    plot_means(power_df, root_path)

    anova_results = compute_anovas(power_df)

    # Aggregated stats across devices (normalized)
    agg_norm = (
        power_df
        .groupby("condition_label")
        .agg(
            mean_pct_vs_baseline=("power_pct_vs_baseline", "mean"),
            std_pct_vs_baseline=("power_pct_vs_baseline", "std"),
            mean_z=("power_z", "mean"),
            std_z=("power_z", "std"),
            n_samples=("power_W", "count"),
        )
        .reset_index()
    )

    plot_aggregated_normalized(agg_norm, root_path)
    plot_aggregated_normalized_time(power_df, root_path)

    # Markdown report
    report_lines = []
    report_lines.append("---")
    report_lines.append("header-includes:")
    report_lines.append("- \\usepackage{graphicx}")
    report_lines.append("- \\usepackage{float}")
    report_lines.append("- \\usepackage[margin=1in]{geometry}")
    report_lines.append(f"- \\graphicspath{{{{./}}{{{root_path.as_posix()}/}}}}")
    report_lines.append("---")
    report_lines.append("# Device Power Analysis Report")
    report_lines.append("")
    report_lines.append("## Overview")
    report_lines.append(
        "Summary of device-side power measurements across five video conditions. "
        "Plots are saved alongside the data files."
    )

    report_lines.append("\n## Basic Stats per Device/Condition")
    report_lines.append(stats_per_device.to_markdown(index=False))
    report_lines.append("")

    report_lines.append("\n## Aligned Sample Counts")
    aligned_counts = power_df.groupby(["device_label", "condition_label"]).size().unstack()
    report_lines.append(aligned_counts.to_markdown())
    report_lines.append("")

    report_lines.append("\n## Aggregated Normalized Stats (Across Devices)")
    report_lines.append(
        "Normalization is per-device. Percent change baseline is 1080p30_6Mbps."
    )
    report_lines.append("")
    report_lines.append(agg_norm.to_markdown(index=False))
    report_lines.append("")

    report_lines.append("\n## Key Findings")
    max_abs_pct = agg_norm["mean_pct_vs_baseline"].abs().max()
    max_abs_z = agg_norm["mean_z"].abs().max()
    report_lines.append(
        f"- Largest aggregated mean percent change vs baseline: {max_abs_pct:.2%}"
    )
    report_lines.append(
        f"- Largest aggregated mean z-score magnitude: {max_abs_z:.3f}"
    )

    sig_resolution = []
    sig_bitrate = []
    for device, res in anova_results:
        if "resolution" in res:
            _, eta, pval = res["resolution"]
            if pval < 0.05:
                sig_resolution.append((device, eta, pval))
        if "bitrate" in res:
            _, eta, pval = res["bitrate"]
            if pval < 0.05:
                sig_bitrate.append((device, eta, pval))

    if not sig_resolution and not sig_bitrate:
        report_lines.append("- No device shows a statistically significant effect at p < 0.05.")
    else:
        if sig_resolution:
            report_lines.append("- Resolution effects (p < 0.05):")
            for device, eta, pval in sig_resolution:
                report_lines.append(
                    f"  - {device}: $\\eta^2$={eta:.3f}, p={pval:.3g}"
                )
        if sig_bitrate:
            report_lines.append("- Bitrate effects (p < 0.05):")
            for device, eta, pval in sig_bitrate:
                report_lines.append(
                    f"  - {device}: $\\eta^2$={eta:.3f}, p={pval:.3g}"
                )

    report_lines.append("\n## ANOVA per Device")
    if not anova_results:
        report_lines.append("No ANOVA results (insufficient category coverage).")
    else:
        for device, res in anova_results:
            report_lines.append(f"\n### {device}")
            if "resolution" in res:
                table, eta, pval = res["resolution"]
                report_lines.append("\nResolution effect at 6 Mbps")
                report_lines.append("")
                report_lines.append(table.to_markdown())
                report_lines.append(f"\n$\\eta^2$ Resolution = {eta:.4f}, p = {pval:.3g}")
                report_lines.append("")
            if "bitrate" in res:
                table, eta, pval = res["bitrate"]
                report_lines.append("\nBitrate effect at 1080p")
                report_lines.append("")
                report_lines.append(table.to_markdown())
                report_lines.append(f"\n$\\eta^2$ Bitrate = {eta:.4f}, p = {pval:.3g}")
                report_lines.append("")

    report_lines.append("\n## Plots")
    report_lines.append(
        "- `<device>_1080p_bitrate.pdf`: Power vs time at 1080p, varying bitrate (per device)."
    )
    report_lines.append(
        "- `<device>_6mbps_resolution.pdf`: Power vs time at 6 Mbps, varying resolution (per device)."
    )
    report_lines.append(
        "- `device_avg_power_per_condition.pdf`: Mean power per condition with std bars across devices."
    )
    report_lines.append(
        "- `device_norm_pct_vs_baseline.pdf`: Aggregated percent-change vs baseline (per device normalization)."
    )
    report_lines.append(
        "- `device_norm_zscore.pdf`: Aggregated z-score per condition (per device normalization)."
    )
    report_lines.append(
        "- `device_norm_time_bitrate.pdf`: Aggregated normalized power vs time (1080p bitrate sweep)."
    )
    report_lines.append(
        "- `device_norm_time_resolution.pdf`: Aggregated normalized power vs time (6 Mbps resolution sweep)."
    )

    report_lines.append("\n## Figures")
    for device in sorted(power_df["device_label"].unique()):
        report_lines.append(f"### {device} – 1080p, varying bitrate")
        report_lines.append(
            f"\\begin{{figure}}[H]\\centering\\includegraphics[width=0.9\\linewidth]{{{device}_1080p_bitrate.pdf}}\\end{{figure}}"
        )
        report_lines.append(f"### {device} – 6 Mbps, varying resolution")
        report_lines.append(
            f"\\begin{{figure}}[H]\\centering\\includegraphics[width=0.9\\linewidth]{{{device}_6mbps_resolution.pdf}}\\end{{figure}}"
        )
    report_lines.append("### Mean power per condition (all devices)")
    report_lines.append(
        "\\begin{figure}[H]\\centering\\includegraphics[width=0.9\\linewidth]{device_avg_power_per_condition.pdf}\\end{figure}"
    )
    report_lines.append("### Aggregated percent change vs baseline")
    report_lines.append(
        "\\begin{figure}[H]\\centering\\includegraphics[width=0.9\\linewidth]{device_norm_pct_vs_baseline.pdf}\\end{figure}"
    )
    report_lines.append("### Aggregated z-score per condition")
    report_lines.append(
        "\\begin{figure}[H]\\centering\\includegraphics[width=0.9\\linewidth]{device_norm_zscore.pdf}\\end{figure}"
    )
    report_lines.append("### Aggregated normalized power vs time – 1080p bitrate sweep")
    report_lines.append(
        "\\begin{figure}[H]\\centering\\includegraphics[width=0.9\\linewidth]{device_norm_time_bitrate.pdf}\\end{figure}"
    )
    report_lines.append("### Aggregated normalized power vs time – 6 Mbps resolution sweep")
    report_lines.append(
        "\\begin{figure}[H]\\centering\\includegraphics[width=0.9\\linewidth]{device_norm_time_resolution.pdf}\\end{figure}"
    )

    report_path = root_path / "device_power_report.md"
    report_path.write_text("\n".join(report_lines))
    print(f"\nWrote markdown report to {report_path}")

    pdf_report_path = root_path / "device_power_report.pdf"
    tex_report_path = root_path / "device_power_report.tex"
    ok = write_pandoc_outputs(report_path, pdf_report_path, tex_report_path)
    if ok:
        print(f"Wrote PDF report to {pdf_report_path}")
        print(f"Wrote TeX report to {tex_report_path}")


if __name__ == "__main__":
    main()
