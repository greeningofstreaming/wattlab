"""
Plot aligned server power vs video luma over time.

Creates per-condition plots with dual y-axes:
  - luma_mean vs encoding power
  - luma_mean vs packaging power
"""

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


def plot_dual_axis(df, time_col, luma_col, power_col, title, out_path):
    fig, ax1 = plt.subplots(figsize=(10, 5))
    ax2 = ax1.twinx()

    ax1.plot(df[time_col], df[luma_col], color="tab:blue", label="luma_mean")
    ax2.plot(df[time_col], df[power_col], color="tab:orange", label=power_col)

    ax1.set_xlabel("Time (s)")
    ax1.set_ylabel("Luma (mean)")
    ax2.set_ylabel(f"{power_col} (W)")

    ax1.set_title(title)
    ax1.grid(True, alpha=0.3)

    lines, labels = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines + lines2, labels + labels2, loc="upper right")

    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def resample_video_to_dt(video_df: pd.DataFrame, dt_s: float):
    bins = (video_df["time_s"] / dt_s).astype(int)
    numeric_cols = video_df.select_dtypes(include=["number"]).columns.tolist()
    drop_cols = {"frame_idx", "time_s", "bin_idx"}
    metric_cols = [c for c in numeric_cols if c not in drop_cols]
    agg_map = {c: "mean" for c in metric_cols}
    agg_map["time_s"] = "min"

    resampled = (
        video_df
        .assign(bin_idx=bins)
        .groupby("bin_idx")
        .agg(agg_map)
        .reset_index()
        .rename(columns={"time_s": "video_time_s"})
    )
    resampled["bin_start_s"] = resampled["bin_idx"] * dt_s
    return resampled


def load_video_stats(video_stats_path: Path, fps: float):
    df = pd.read_excel(video_stats_path, sheet_name=0)
    if "time_s" not in df.columns:
        if "frame_idx" in df.columns:
            df["time_s"] = df["frame_idx"] / float(fps)
        else:
            df["time_s"] = pd.RangeIndex(len(df)) / float(fps)
            df["frame_idx"] = pd.RangeIndex(len(df))
    if "luma_mean" not in df.columns and "Y_mean_10b" in df.columns:
        df["luma_mean"] = df["Y_mean_10b"].astype(float)
    return df


def main():
    ap = argparse.ArgumentParser(description="Plot luma vs server power over time.")
    ap.add_argument("--unaligned", action="store_true")
    ap.add_argument("--video-stats", default="data_analysis/video_stats/full_video_luma.xlsx")
    ap.add_argument("--fps", type=float, default=30.0)
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    out_dir = root / "data/correlation_plots"
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.unaligned:
        video_df = load_video_stats(root / args.video_stats, fps=args.fps)
        server_df = pd.read_excel(root / "data/serverPower20251103_tidy.xlsx")
        server_df = server_df.sort_values(["condition_label", "t_rel_s"])
        srv = server_df.copy()
    else:
        aligned_path = root / "data_analysis/video_power_aligned.xlsx"
        df = pd.read_excel(aligned_path, sheet_name="aligned")
        srv = df[df["source"] == "server"].copy()

    if srv.empty:
        print("No server rows found in aligned dataset.")
        return

    for cond, sub in srv.groupby("condition_label"):
        sub = sub.sort_values("t_rel_s")
        if args.unaligned:
            dt_s = sub["t_rel_s"].diff().dropna().median()
            video_resampled = resample_video_to_dt(video_df, dt_s)
            sub = sub.copy()
            sub["bin_idx"] = (sub["t_rel_s"] / dt_s).round().astype(int)
            sub = sub.merge(
                video_resampled,
                left_on="bin_idx",
                right_on="bin_idx",
                how="left",
            )

        if "luma_mean" not in sub.columns:
            print("Missing luma_mean in dataset.")
            return

        if "power_enc_W" in sub.columns:
            out_path = out_dir / f"server_{cond}_enc_luma_time.pdf"
            plot_dual_axis(
                sub,
                time_col="t_rel_s",
                luma_col="luma_mean",
                power_col="power_enc_W",
                title=f"{cond}: Encoding power vs luma",
                out_path=out_path,
            )

        if "power_pck_W" in sub.columns:
            out_path = out_dir / f"server_{cond}_pck_luma_time.pdf"
            plot_dual_axis(
                sub,
                time_col="t_rel_s",
                luma_col="luma_mean",
                power_col="power_pck_W",
                title=f"{cond}: Packaging power vs luma",
                out_path=out_path,
            )

    print(f"Wrote plots to {out_dir}")


if __name__ == "__main__":
    main()
