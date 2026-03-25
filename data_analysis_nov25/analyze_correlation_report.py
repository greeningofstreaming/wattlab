"""
Generate a correlation report (device + server) for video stats vs power.

Outputs:
  - data/correlation_report.md
  - data/correlation_report.pdf
  - data/correlation_report.tex
"""

from pathlib import Path
import subprocess

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
    return sm.OLS(y, X).fit()


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


def summarize_model(label, model, n, x_col, y_col):
    return {
        "label": label,
        "x_col": x_col,
        "y_col": y_col,
        "n_samples": n,
        "intercept": model.params[0],
        "slope": model.params[1],
        "r2": model.rsquared,
        "p_value": model.pvalues[1],
    }


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

    # Device normalization
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

    device_summaries = []
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
                device_summaries.append(summarize_model(label, model, len(dev_df), x_col, y_col))

    device_summary_df = pd.DataFrame(device_summaries)

    # Server correlations (enc/pck/total)
    srv_df = df[df["source"] == "server"].copy()
    server_summaries = []
    if not srv_df.empty:
        server_targets = [
            ("power_enc_W", "enc"),
            ("power_pck_W", "pck"),
            ("power_value_W", "total"),
        ]
        for y_col, y_label in server_targets:
            if y_col not in srv_df.columns:
                continue
            for x_col in metrics:
                label = f"server_all_{y_label}_{x_col}"
                out_path = out_dir / f"{label}.pdf"
                model = scatter_with_fit(
                    srv_df, x_col, y_col,
                    title=f"Server {y_label} power vs {x_col}",
                    out_path=out_path,
                )
                if model is not None:
                    server_summaries.append(summarize_model(label, model, len(srv_df), x_col, y_col))

    server_summary_df = pd.DataFrame(server_summaries)

    # Report
    report_lines = []
    report_lines.append("---")
    report_lines.append("header-includes:")
    report_lines.append("- \\usepackage{graphicx}")
    report_lines.append("- \\usepackage{float}")
    report_lines.append("- \\usepackage[margin=1in]{geometry}")
    report_lines.append(f"- \\graphicspath{{{{./}}{{{(root / 'data').as_posix()}/}}}}")
    report_lines.append("---")
    report_lines.append("# Video Stats vs Power Correlation Report")
    report_lines.append("")
    report_lines.append("## Overview")
    report_lines.append(
        "This report summarizes correlations between video content metrics "
        "(luma + texture/edge metrics) and power measurements."
    )

    report_lines.append("\n## Device (Normalized) Correlations")
    if device_summary_df.empty:
        report_lines.append("No device correlation results available.")
    else:
        report_lines.append(device_summary_df.to_markdown(index=False))

    report_lines.append("\n## Server Correlations")
    report_lines.append(
        "Server correlations are generally weak; encoding/packaging are not guaranteed to be real-time, "
        "and any alignment is approximate."
    )
    if server_summary_df.empty:
        report_lines.append("No server correlation results available.")
    else:
        report_lines.append(server_summary_df.to_markdown(index=False))

    report_lines.append("\n## Selected Plots")
    report_lines.append("### Device normalized: luma vs power_z")
    report_lines.append(
        "\\begin{figure}[H]\\centering\\includegraphics[width=0.85\\linewidth]{correlation_plots/device_all_zscore_luma_mean.pdf}\\end{figure}"
    )
    report_lines.append("### Device normalized: grad_mean vs power_z")
    report_lines.append(
        "\\begin{figure}[H]\\centering\\includegraphics[width=0.85\\linewidth]{correlation_plots/device_all_zscore_grad_mean.pdf}\\end{figure}"
    )
    report_lines.append("### Server encoding: luma vs power_enc_W")
    report_lines.append(
        "\\begin{figure}[H]\\centering\\includegraphics[width=0.85\\linewidth]{correlation_plots/server_all_enc_luma_mean.pdf}\\end{figure}"
    )
    report_lines.append("### Server packaging: luma vs power_pck_W")
    report_lines.append(
        "\\begin{figure}[H]\\centering\\includegraphics[width=0.85\\linewidth]{correlation_plots/server_all_pck_luma_mean.pdf}\\end{figure}"
    )

    report_path = root / "data/correlation_report.md"
    report_path.write_text("\n".join(report_lines))
    print(f"Wrote markdown report to {report_path}")

    pdf_report_path = root / "data/correlation_report.pdf"
    tex_report_path = root / "data/correlation_report.tex"
    ok = write_pandoc_outputs(report_path, pdf_report_path, tex_report_path)
    if ok:
        print(f"Wrote PDF report to {pdf_report_path}")
        print(f"Wrote TeX report to {tex_report_path}")


if __name__ == "__main__":
    main()
