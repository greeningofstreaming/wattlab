"""
Analysis and plotting for server power data.

This script expects the cleaned tidy data produced by clean_data.py
(`serverPower20251103_tidy.xlsx`). If that file is missing, it will
regenerate it from the raw Excel before running the plots/ANOVA.
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

from clean_data import tidy_server_power_sheet


def load_or_build_tidy(root_path: Path) -> pd.DataFrame:
    tidy_path = root_path / "serverPower20251103_tidy.xlsx"
    raw_excel = root_path / "serverPower20251103 Analysis.xlsx"

    if tidy_path.exists():
        return pd.read_excel(tidy_path)

    tidy_df = tidy_server_power_sheet(
        str(raw_excel),
        sheet_name="serverPower20251103-1659",
        experiment_id="serverPower20251103",
        device_role="server",
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


def main() -> None:
    root_path = Path(__file__).resolve().parent.parent / "data"
    tidy_df = load_or_build_tidy(root_path)

    # Basic stats: average power & number of measurements per condition
    stats_per_condition = (
        tidy_df
        .groupby("condition_label")
        .agg(
            n_samples=("sample_idx", "count"),
            mean_power_enc_W=("power_enc_W", "mean"),
            mean_power_pck_W=("power_pck_W", "mean"),
            std_power_enc_W=("power_enc_W", "std"),
            std_power_pck_W=("power_pck_W", "std"),
        )
        .sort_index()
    )
    print("\n=== Stats per condition ===")
    print(stats_per_condition)

    # Build a power-only structure, aligned to minimal samples per condition
    tidy_df = tidy_df.copy()

    tidy_df = tidy_df.sort_values(
        ["condition_label", "t_rel_s", "sample_idx"]
    )

    target_len = (
        tidy_df.groupby("condition_label")["sample_idx"]
        .count()
        .min()
    )
    print(f"Target length per condition: {target_len}")

    trimmed = (
        tidy_df
        .groupby("condition_label", group_keys=False)
        .tail(target_len)
    ).copy()

    trimmed["t_rel_s"] = (
        trimmed.groupby("condition_label")["t_rel_s"]
        .transform(lambda s: s - s.min())
    )
    trimmed["sample_idx_aligned"] = (
        trimmed.groupby("condition_label").cumcount()
    )

    print(
        "Samples per condition after trimming:\n",
        trimmed["condition_label"].value_counts()
    )

    power_df = trimmed[
        [
            "condition_label",
            "resolution",
            "bitrate_Mbps",
            "framerate",
            "t_rel_s",
            "power_enc_W",
            "power_pck_W",
        ]
    ].reset_index(drop=True)

    # Add smoothed versions (moving average) per condition
    SMOOTH_WINDOW = 7
    power_df = power_df.sort_values(["condition_label", "t_rel_s"])

    power_df["power_enc_smooth_W"] = (
        power_df
        .groupby("condition_label")["power_enc_W"]
        .transform(
            lambda s: s.rolling(
                window=SMOOTH_WINDOW, center=True, min_periods=1
            ).mean()
        )
    )
    power_df["power_pck_smooth_W"] = (
        power_df
        .groupby("condition_label")["power_pck_W"]
        .transform(
            lambda s: s.rolling(
                window=SMOOTH_WINDOW, center=True, min_periods=1
            ).mean()
        )
    )

    # Plot 1: encoding power – 1080p, varying bitrate
    bitrate_order = [1, 6, 12]  # Mbps
    colors = {1: "tab:blue", 6: "tab:orange", 12: "tab:green"}

    mask_1080 = power_df["resolution"] == "1080p"
    df_1080 = power_df[mask_1080]

    plt.figure(figsize=(10, 6))
    for br in bitrate_order:
        sub = df_1080[df_1080["bitrate_Mbps"] == br]
        if sub.empty:
            continue

        plt.plot(
            sub["t_rel_s"],
            sub["power_enc_W"],
            linestyle=":",
            alpha=0.3,
            color=colors[br],
            label=f"{br} Mbps raw"
        )
        plt.plot(
            sub["t_rel_s"],
            sub["power_enc_smooth_W"],
            linestyle="-",
            color=colors[br],
            label=f"{br} Mbps smoothed",
        )

    plt.xlabel("Time (s)")
    plt.ylabel("Encoding power (W)")
    plt.title("Encoding power vs time – 1080p30, varying bitrate")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.savefig(os.path.join(root_path, "enc_power_1080p_bitrate.pdf"))
    plt.tight_layout()
    plt.close()

    # Plot 2: encoding power – 6 Mbps, varying resolution
    res_order = ["1080p", "540p", "270p"]
    colors_res = {"1080p": "tab:blue", "540p": "tab:orange", "270p": "tab:green"}

    mask_6Mbps = power_df["bitrate_Mbps"] == 6
    df_6Mbps = power_df[mask_6Mbps]

    plt.figure(figsize=(10, 6))
    for res in res_order:
        sub = df_6Mbps[df_6Mbps["resolution"] == res]
        if sub.empty:
            continue

        plt.plot(
            sub["t_rel_s"],
            sub["power_enc_W"],
            linestyle=":",
            alpha=0.3,
            color=colors_res[res],
            label=f"{res} raw"
        )
        plt.plot(
            sub["t_rel_s"],
            sub["power_enc_smooth_W"],
            linestyle="-",
            color=colors_res[res],
            label=f"{res} smoothed",
        )

    plt.xlabel("Time (s)")
    plt.ylabel("Encoding power (W)")
    plt.title("Encoding power vs time – 6 Mbps, varying resolution")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.savefig(os.path.join(root_path, "enc_power_6mbps_resolution.pdf"))
    plt.tight_layout()
    plt.close()

    # Plot 3: packaging power – 1080p, varying bitrate
    mask_1080 = power_df["resolution"] == "1080p"
    df_1080 = power_df[mask_1080]

    plt.figure(figsize=(10, 6))
    for br in bitrate_order:
        sub = df_1080[df_1080["bitrate_Mbps"] == br]
        if sub.empty:
            continue

        plt.plot(
            sub["t_rel_s"],
            sub["power_pck_W"],
            linestyle=":",
            alpha=0.3,
            color=colors[br],
            label=f"{br} Mbps raw"
        )
        plt.plot(
            sub["t_rel_s"],
            sub["power_pck_smooth_W"],
            linestyle="-",
            color=colors[br],
            label=f"{br} Mbps smoothed",
        )

    plt.xlabel("Time (s)")
    plt.ylabel("Packaging power (W)")
    plt.title("Packaging power vs time – 1080p30, varying bitrate")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.savefig(os.path.join(root_path, "pkg_power_1080p_bitrate.pdf"))
    plt.tight_layout()
    plt.close()

    # Plot 4: packaging power – 6 Mbps, varying resolution
    mask_6Mbps = power_df["bitrate_Mbps"] == 6
    df_6Mbps = power_df[mask_6Mbps]

    plt.figure(figsize=(10, 6))
    for res in res_order:
        sub = df_6Mbps[df_6Mbps["resolution"] == res]
        if sub.empty:
            continue

        plt.plot(
            sub["t_rel_s"],
            sub["power_pck_W"],
            linestyle=":",
            alpha=0.3,
            color=colors_res[res],
            label=f"{res} raw"
        )
        plt.plot(
            sub["t_rel_s"],
            sub["power_pck_smooth_W"],
            linestyle="-",
            color=colors_res[res],
            label=f"{res} smoothed",
        )

    plt.xlabel("Time (s)")
    plt.ylabel("Packaging power (W)")
    plt.title("Packaging power vs time – 6 Mbps, varying resolution")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.savefig(os.path.join(root_path, "pkg_power_6mbps_resolution.pdf"))
    plt.tight_layout()
    plt.close()

    # Average encoding and packaging power per condition
    stats_enc = (
        power_df
        .groupby("condition_label")["power_enc_W"]
        .agg(["mean", "std"])
        .reset_index()
    )
    stats_pck = (
        power_df
        .groupby("condition_label")["power_pck_W"]
        .agg(["mean", "std"])
        .reset_index()
    )

    condition_order = [
        "1080p30_12Mbps",
        "1080p30_6Mbps",
        "1080p30_1Mbps",
        "540p30_6Mbps",
        "270p30_6Mbps",
    ]
    stats_enc = stats_enc.set_index("condition_label").loc[condition_order].reset_index()
    stats_pck = stats_pck.set_index("condition_label").loc[condition_order].reset_index()

    x = np.arange(len(stats_enc))
    labels = stats_enc["condition_label"].tolist()

    plt.figure(figsize=(10, 6))
    plt.bar(x, stats_enc["mean"].values, yerr=stats_enc["std"].values, capsize=5)
    plt.xticks(x, labels, rotation=30, ha="right")
    plt.ylabel("Average encoding power (W)")
    plt.title("Average encoding power per condition\n(with standard deviation)")
    plt.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(os.path.join(root_path, "avg_enc_power_per_condition.pdf"))
    plt.close()

    plt.figure(figsize=(10, 6))
    plt.bar(x, stats_pck["mean"].values, yerr=stats_pck["std"].values, capsize=5)
    plt.xticks(x, labels, rotation=30, ha="right")
    plt.ylabel("Average packaging power (W)")
    plt.title("Average packaging power per condition\n(with standard deviation)")
    plt.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(os.path.join(root_path, "avg_pck_power_per_condition.pdf"))
    plt.close()

    # ANOVA analysis
    df_res = power_df[power_df["bitrate_Mbps"] == 6].copy()
    model_enc_res = ols("power_enc_W ~ C(resolution)", data=df_res).fit()
    anova_enc_res = sm.stats.anova_lm(model_enc_res, typ=2)
    print("\nEncoding power ~ resolution (ANOVA):")
    print(anova_enc_res)

    ss_effect = anova_enc_res.loc["C(resolution)", "sum_sq"]
    ss_resid = anova_enc_res.loc["Residual", "sum_sq"]
    eta_sq_res = ss_effect / (ss_effect + ss_resid)
    print("eta² Encoding vs Resolution =", eta_sq_res)

    model_pck_res = ols("power_pck_W ~ C(resolution)", data=df_res).fit()
    anova_pck_res = sm.stats.anova_lm(model_pck_res, typ=2)
    print("\nPackaging power ~ resolution (ANOVA):")
    print(anova_pck_res)

    ss_effect = anova_pck_res.loc["C(resolution)", "sum_sq"]
    ss_resid = anova_pck_res.loc["Residual", "sum_sq"]
    eta_sq_pck_res = ss_effect / (ss_effect + ss_resid)
    print("eta² Packager vs Resolution =", eta_sq_pck_res)

    df_br = power_df[power_df["resolution"] == "1080p"].copy()
    model_enc_br = ols("power_enc_W ~ C(bitrate_Mbps)", data=df_br).fit()
    anova_enc_br = sm.stats.anova_lm(model_enc_br, typ=2)
    print(anova_enc_br)

    term = "C(bitrate_Mbps)"
    ss_effect_br = anova_enc_br.loc[term, "sum_sq"]
    ss_resid_br = anova_enc_br.loc["Residual", "sum_sq"]
    eta_sq_br = ss_effect_br / (ss_effect_br + ss_resid_br)
    print(f"eta² Encoding vs Bitrate =", eta_sq_br)

    model_pck_br = ols("power_pck_W ~ C(bitrate_Mbps)", data=df_br).fit()
    anova_pck_br = sm.stats.anova_lm(model_pck_br, typ=2)
    print(anova_pck_br)

    ss_effect_pck = anova_pck_br.loc[term, "sum_sq"]
    ss_resid_pck = anova_pck_br.loc["Residual", "sum_sq"]
    eta_sq_pck = ss_effect_pck / (ss_effect_pck + ss_resid_pck)
    print("eta² Packager vs Bitrate =", eta_sq_pck)

    # ------------------------------------------------------------------
    # Write markdown report
    # ------------------------------------------------------------------
    report_lines = []
    report_lines.append("---")
    report_lines.append("header-includes:")
    report_lines.append("- \\usepackage{graphicx}")
    report_lines.append("- \\usepackage{float}")
    report_lines.append("- \\usepackage[margin=1in]{geometry}")
    report_lines.append(f"- \\graphicspath{{{{./}}{{{root_path.as_posix()}/}}}}")
    report_lines.append("---")
    report_lines.append("# Server Power Analysis Report")
    report_lines.append("")
    report_lines.append("## Overview")
    report_lines.append(
        "Summary of server-side power measurements across five video conditions. "
        "Plots are saved alongside the data files."
    )

    report_lines.append("\n## Basic Stats per Condition")
    report_lines.append(stats_per_condition.to_markdown())
    report_lines.append("")

    report_lines.append("\n## Trimmed/Aggregated Stats – Encoding")
    report_lines.append(stats_enc.to_markdown(index=False))
    report_lines.append("")
    report_lines.append("\n## Trimmed/Aggregated Stats – Packaging")
    report_lines.append(stats_pck.to_markdown(index=False))
    report_lines.append("")

    report_lines.append("\n## ANOVA – Resolution effect at 6 Mbps")
    report_lines.append(anova_enc_res.to_markdown())
    report_lines.append(f"\n$\\eta^2$ Encoding vs Resolution = {eta_sq_res:.4f}")
    report_lines.append("")
    report_lines.append(anova_pck_res.to_markdown())
    report_lines.append(f"\n$\\eta^2$ Packager vs Resolution = {eta_sq_pck_res:.4f}")
    report_lines.append("")

    report_lines.append("\n## ANOVA – Bitrate effect at 1080p")
    report_lines.append(anova_enc_br.to_markdown())
    report_lines.append(f"\n$\\eta^2$ Encoding vs Bitrate = {eta_sq_br:.4f}")
    report_lines.append("")
    report_lines.append(anova_pck_br.to_markdown())
    report_lines.append(f"\n$\\eta^2$ Packager vs Bitrate = {eta_sq_pck:.4f}")
    report_lines.append("")

    report_lines.append("\n## Key Findings")
    max_abs_enc = stats_enc["mean"].sub(stats_enc["mean"].mean()).abs().max()
    max_abs_pck = stats_pck["mean"].sub(stats_pck["mean"].mean()).abs().max()
    report_lines.append(
        f"- Max deviation in encoding means from overall mean: {max_abs_enc:.2f} W"
    )
    report_lines.append(
        f"- Max deviation in packaging means from overall mean: {max_abs_pck:.2f} W"
    )
    report_lines.append(
        f"- Encoding resolution effect size ($\\eta^2$): {eta_sq_res:.3f}"
    )
    report_lines.append(
        f"- Packager resolution effect size ($\\eta^2$): {eta_sq_pck_res:.3f}"
    )
    report_lines.append(
        f"- Encoding bitrate effect size ($\\eta^2$): {eta_sq_br:.3f}"
    )
    report_lines.append(
        f"- Packager bitrate effect size ($\\eta^2$): {eta_sq_pck:.3f}"
    )

    report_lines.append("\n## Plots")
    report_lines.append(
        "- `enc_power_1080p_bitrate.pdf`: Encoding power vs time at 1080p, varying bitrate."
    )
    report_lines.append(
        "- `enc_power_6mbps_resolution.pdf`: Encoding power vs time at 6 Mbps, varying resolution."
    )
    report_lines.append(
        "- `pkg_power_1080p_bitrate.pdf`: Packager power vs time at 1080p, varying bitrate."
    )
    report_lines.append(
        "- `pkg_power_6mbps_resolution.pdf`: Packager power vs time at 6 Mbps, varying resolution."
    )
    report_lines.append(
        "- `avg_enc_power_per_condition.pdf`: Mean encoding power per condition with std bars."
    )
    report_lines.append(
        "- `avg_pck_power_per_condition.pdf`: Mean packaging power per condition with std bars."
    )

    report_lines.append("\n## Figures")
    report_lines.append("### Encoding power vs time – 1080p, varying bitrate")
    report_lines.append(
        "\\begin{figure}[H]\\centering\\includegraphics[width=0.9\\linewidth]{enc_power_1080p_bitrate.pdf}\\end{figure}"
    )
    report_lines.append("### Encoding power vs time – 6 Mbps, varying resolution")
    report_lines.append(
        "\\begin{figure}[H]\\centering\\includegraphics[width=0.9\\linewidth]{enc_power_6mbps_resolution.pdf}\\end{figure}"
    )
    report_lines.append("### Packager power vs time – 1080p, varying bitrate")
    report_lines.append(
        "\\begin{figure}[H]\\centering\\includegraphics[width=0.9\\linewidth]{pkg_power_1080p_bitrate.pdf}\\end{figure}"
    )
    report_lines.append("### Packager power vs time – 6 Mbps, varying resolution")
    report_lines.append(
        "\\begin{figure}[H]\\centering\\includegraphics[width=0.9\\linewidth]{pkg_power_6mbps_resolution.pdf}\\end{figure}"
    )
    report_lines.append("### Average encoding power per condition (mean ± std)")
    report_lines.append(
        "\\begin{figure}[H]\\centering\\includegraphics[width=0.9\\linewidth]{avg_enc_power_per_condition.pdf}\\end{figure}"
    )
    report_lines.append("### Average packaging power per condition (mean ± std)")
    report_lines.append(
        "\\begin{figure}[H]\\centering\\includegraphics[width=0.9\\linewidth]{avg_pck_power_per_condition.pdf}\\end{figure}"
    )

    report_path = root_path / "server_power_report.md"
    report_path.write_text("\n".join(report_lines))
    print(f"\nWrote markdown report to {report_path}")

    pdf_report_path = root_path / "server_power_report.pdf"
    tex_report_path = root_path / "server_power_report.tex"
    ok = write_pandoc_outputs(report_path, pdf_report_path, tex_report_path)
    if ok:
        print(f"Wrote PDF report to {pdf_report_path}")
        print(f"Wrote TeX report to {tex_report_path}")


if __name__ == "__main__":
    main()
