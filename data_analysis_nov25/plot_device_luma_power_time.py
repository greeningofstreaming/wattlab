"""
Plot aligned device power vs video luma over time.

Creates per-device/condition plots with dual y-axes:
  - luma_mean vs device power.
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


def plot_triple_axis(df, time_col, luma_col, power_col, texture_col, title, out_path):
    fig, ax1 = plt.subplots(figsize=(10, 5))
    ax2 = ax1.twinx()
    ax3 = ax1.twinx()
    ax3.spines["right"].set_position(("axes", 1.12))

    ax1.plot(df[time_col], df[luma_col], color="tab:blue", label="luma_mean")
    ax2.plot(df[time_col], df[power_col], color="tab:orange", label=power_col)
    ax3.plot(df[time_col], df[texture_col], color="tab:green", label=texture_col)

    ax1.set_xlabel("Time (s)")
    ax1.set_ylabel("Luma (mean)")
    ax2.set_ylabel("Power (W)")
    ax3.set_ylabel(texture_col)

    ax1.set_title(title)
    ax1.grid(True, alpha=0.3)

    lines, labels = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    lines3, labels3 = ax3.get_legend_handles_labels()
    ax1.legend(lines + lines2 + lines3, labels + labels2 + labels3, loc="upper right")

    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_device_sweeps(dev_df, device_label, out_dir):
    """
    One figure per device:
      - left subplot: bitrate sweep at 1080p
      - right subplot: resolution sweep at 6 Mbps
    Uses normalized power (% vs baseline) with raw+smoothed curves.
    """
    SMOOTH_WINDOW = 7
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

    fig, axes = plt.subplots(1, 2, figsize=(12, 5), sharey=True)

    # Bitrate sweep (1080p)
    ax = axes[0]
    for cond in bitrate_conditions:
        sub = dev_df[dev_df["condition_label"] == cond].sort_values("t_rel_s")
        if sub.empty:
            continue
        smooth = (
            sub["power_pct_vs_baseline"]
            .rolling(window=SMOOTH_WINDOW, center=True, min_periods=1)
            .mean()
        )
        ax.plot(
            sub["t_rel_s"],
            sub["power_pct_vs_baseline"],
            linestyle=":",
            alpha=0.3,
            color=colors_bitrate.get(cond),
            label=f"{cond} raw",
        )
        ax.plot(
            sub["t_rel_s"],
            smooth,
            linestyle="-",
            color=colors_bitrate.get(cond),
            label=f"{cond} smoothed",
        )

    ax.set_title("1080p: varying bitrate")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Power % vs baseline")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)

    # Resolution sweep (6 Mbps)
    ax = axes[1]
    for cond in resolution_conditions:
        sub = dev_df[dev_df["condition_label"] == cond].sort_values("t_rel_s")
        if sub.empty:
            continue
        smooth = (
            sub["power_pct_vs_baseline"]
            .rolling(window=SMOOTH_WINDOW, center=True, min_periods=1)
            .mean()
        )
        ax.plot(
            sub["t_rel_s"],
            sub["power_pct_vs_baseline"],
            linestyle=":",
            alpha=0.3,
            color=colors_res.get(cond),
            label=f"{cond} raw",
        )
        ax.plot(
            sub["t_rel_s"],
            smooth,
            linestyle="-",
            color=colors_res.get(cond),
            label=f"{cond} smoothed",
        )

    ax.set_title("6 Mbps: varying resolution")
    ax.set_xlabel("Time (s)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)

    fig.suptitle(f"{device_label}: normalized power vs time")
    fig.tight_layout()
    out_path = out_dir / f"device_{device_label}_sweeps.pdf"
    fig.savefig(out_path)
    plt.close(fig)


def main():
    root = Path(__file__).resolve().parent.parent
    aligned_path = root / "data_analysis/video_power_aligned.xlsx"
    out_dir = root / "data/correlation_plots"
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_excel(aligned_path, sheet_name="aligned")
    dev = df[df["source"] == "device"].copy()

    if dev.empty:
        print("No device rows found in aligned dataset.")
        return

    if "luma_mean" not in dev.columns:
        print("Missing luma_mean in aligned dataset.")
        return

    baseline_label = "1080p30_6Mbps"
    baseline_means = (
        dev[dev["condition_label"] == baseline_label]
        .groupby("device_label")["power_value_W"]
        .mean()
    )
    dev["baseline_mean_W"] = dev["device_label"].map(baseline_means)
    dev["power_pct_vs_baseline"] = (
        (dev["power_value_W"] - dev["baseline_mean_W"]) / dev["baseline_mean_W"]
    )

    texture_col = "grad_mean" if "grad_mean" in dev.columns else "lap_var"
    if texture_col not in dev.columns:
        print("Missing texture metric in aligned dataset.")
        return

    for (device, cond), sub in dev.groupby(["device_label", "condition_label"]):
        sub = sub.sort_values("t_rel_s")
        out_path = out_dir / f"device_{device}_{cond}_luma_time.pdf"
        plot_triple_axis(
            sub,
            time_col="t_rel_s",
            luma_col="luma_mean",
            power_col="power_value_W",
            texture_col=texture_col,
            title=f"{device} {cond}: device power vs luma",
            out_path=out_path,
        )

    for device, sub in dev.groupby("device_label"):
        plot_device_sweeps(sub, device, out_dir)

    # Aggregate across devices (mean per time bin and condition)
    agg = (
        dev
        .groupby(["condition_label", "t_rel_s"])
        .agg(
            luma_mean=("luma_mean", "mean"),
            power_value_W=("power_value_W", "mean"),
            texture_val=(texture_col, "mean"),
        )
        .reset_index()
    )
    agg = agg.rename(columns={"texture_val": texture_col})
    for cond, sub in agg.groupby("condition_label"):
        sub = sub.sort_values("t_rel_s")
        out_path = out_dir / f"device_all_{cond}_luma_time.pdf"
        plot_triple_axis(
            sub,
            time_col="t_rel_s",
            luma_col="luma_mean",
            power_col="power_value_W",
            texture_col=texture_col,
            title=f"All devices {cond}: power vs luma vs {texture_col}",
            out_path=out_path,
        )

    # Aggregate across all devices and all conditions
    agg_all = (
        dev
        .groupby("t_rel_s")
        .agg(
            luma_mean=("luma_mean", "mean"),
            power_value_W=("power_value_W", "mean"),
            texture_val=(texture_col, "mean"),
        )
        .reset_index()
        .rename(columns={"texture_val": texture_col})
        .sort_values("t_rel_s")
    )
    out_path = out_dir / "device_all_conditions_luma_time.pdf"
    plot_triple_axis(
        agg_all,
        time_col="t_rel_s",
        luma_col="luma_mean",
        power_col="power_value_W",
        texture_col=texture_col,
        title=f"All devices, all conditions: power vs luma vs {texture_col}",
        out_path=out_path,
    )

    print(f"Wrote plots to {out_dir}")


if __name__ == "__main__":
    main()
