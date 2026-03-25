"""
Align video luminance stats with device/server power measurements.

This script builds a correlation-ready dataset by:
  1) extracting per-frame luma means from the video (if needed),
  2) resampling luma to the power sampling interval,
  3) finding the best lag via cross-correlation, and
  4) joining power samples with aligned video stats.
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def extract_video_luma(
    video_path: Path,
    output_path: Path,
    frame_step: int,
    resize_w: int,
    resize_h: int,
    texture_scale: int,
    edge_thresh: float,
    target_fps: float,
):
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("cv2 is required to compute video stats") from exc

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if target_fps and target_fps > 0:
        frame_step = max(1, int(round(fps / target_fps)))
    rows = []
    idx = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % frame_step == 0:
            if resize_w > 0 and resize_h > 0:
                frame = cv2.resize(frame, (resize_w, resize_h), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            mean_luma = float(gray.mean())
            time_s = idx / fps

            g = gray
            s = int(texture_scale) if int(texture_scale) >= 1 else 1
            if s > 1:
                g = g[::s, ::s]

            gy, gx = np.gradient(g.astype(float))
            grad = np.sqrt(gx * gx + gy * gy)
            grad_mean = float(np.mean(grad))
            grad_std = float(np.std(grad))

            lap = (-4.0 * g
                   + np.roll(g, 1, axis=0) + np.roll(g, -1, axis=0)
                   + np.roll(g, 1, axis=1) + np.roll(g, -1, axis=1))
            lap_var = float(np.var(lap))
            edge_density = float(np.mean(grad > edge_thresh))

            rows.append([idx, time_s, mean_luma, grad_mean, grad_std, lap_var, edge_density])
        idx += 1

    cap.release()

    df = pd.DataFrame(
        rows,
        columns=[
            "frame_idx",
            "time_s",
            "luma_mean",
            "grad_mean",
            "grad_std",
            "lap_var",
            "edge_density",
        ],
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(output_path, index=False, sheet_name="luma_per_frame")
    print(f"Wrote video stats to {output_path}")
    return df


def get_video_meta(video_path: Path):
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("cv2 is required to read video metadata") from exc

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration_s = total_frames / fps if fps > 0 else 0.0
    cap.release()
    return fps, total_frames, duration_s


def load_video_stats(video_stats_path: Path, fps: float, video_duration_s: float):
    df = pd.read_excel(video_stats_path, sheet_name=0)
    if "time_s" not in df.columns:
        if "frame_idx" in df.columns:
            df["time_s"] = df["frame_idx"] / float(fps)
        else:
            df["time_s"] = np.arange(len(df)) / float(fps)
            df["frame_idx"] = np.arange(len(df))
    if "luma_mean" not in df.columns:
        if "Y_mean_10b" in df.columns:
            df["luma_mean"] = df["Y_mean_10b"].astype(float)
        elif "Y_mean" in df.columns:
            df["luma_mean"] = df["Y_mean"].astype(float)

    if video_duration_s > 0:
        stats_duration = float(df["time_s"].max())
        if stats_duration < 0.9 * video_duration_s:
            print(
                "Warning: video stats cover %.1fs of %.1fs (%.1f%%). "
                "Recompute stats to cover full duration."
                % (stats_duration, video_duration_s, 100.0 * stats_duration / video_duration_s)
            )
    return df


def resample_video_to_dt(video_df: pd.DataFrame, dt_s: float):
    bins = (video_df["time_s"] / dt_s).astype(int)
    numeric_cols = video_df.select_dtypes(include=[np.number]).columns.tolist()
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
    )
    resampled = resampled.rename(columns={"time_s": "video_time_s"})
    resampled["bin_start_s"] = resampled["bin_idx"] * dt_s
    return resampled


def best_lag(power, video, max_lag_samples):
    power = np.asarray(power, dtype=float)
    video = np.asarray(video, dtype=float)
    power = (power - power.mean()) / (power.std() + 1e-9)
    video = (video - video.mean()) / (video.std() + 1e-9)

    best_corr = -np.inf
    best_lag = 0

    for lag in range(-max_lag_samples, max_lag_samples + 1):
        if lag < 0:
            p = power[-lag:]
            v = video[:len(p)]
        elif lag > 0:
            p = power[:-lag]
            v = video[lag:]
        else:
            p = power
            v = video

        min_len = min(len(p), len(v))
        if min_len < 3:
            continue
        p = p[:min_len]
        v = v[:min_len]
        corr = np.corrcoef(p, v)[0, 1]
        if corr > best_corr:
            best_corr = corr
            best_lag = lag

    return best_lag, best_corr


def align_power_with_video(power_df, power_col, time_col, video_resampled, max_lag_s):
    dt_s = power_df[time_col].diff().dropna().median()
    if pd.isna(dt_s) or dt_s <= 0:
        raise ValueError("Could not determine power sampling interval.")
    max_lag_samples = int(round(max_lag_s / dt_s))

    power_vals = power_df[power_col].values
    video_vals = video_resampled["luma_mean"].values

    if max_lag_samples <= 0:
        lag_samples, corr = 0, np.nan
    else:
        lag_samples, corr = best_lag(power_vals, video_vals, max_lag_samples)
    video_idx = np.arange(len(power_vals)) + lag_samples
    mask = (video_idx >= 0) & (video_idx < len(video_resampled))

    aligned = power_df.loc[mask].copy()
    aligned["power_value_W"] = aligned[power_col]
    aligned["video_bin_idx"] = video_idx[mask].astype(int)
    aligned["lag_samples"] = lag_samples
    aligned["lag_seconds"] = lag_samples * dt_s
    aligned["corr_coeff"] = corr

    aligned = aligned.merge(
        video_resampled,
        left_on="video_bin_idx",
        right_on="bin_idx",
        how="left",
    ).drop(columns=["bin_idx"])

    return aligned


def main():
    ap = argparse.ArgumentParser(description="Align video luma stats with power data.")
    ap.add_argument("--video-path", default="media/full_video_1080p30.mov")
    ap.add_argument("--video-stats", default="data_analysis/video_stats/full_video_luma.xlsx")
    ap.add_argument("--compute-video-stats", action="store_true")
    ap.add_argument("--frame-step", type=int, default=1)
    ap.add_argument("--target-fps", type=float, default=2.0)
    ap.add_argument("--resize-w", type=int, default=160)
    ap.add_argument("--resize-h", type=int, default=90)
    ap.add_argument("--texture-scale", type=int, default=2)
    ap.add_argument("--edge-thresh", type=float, default=10.0)
    ap.add_argument("--max-lag-s", type=float, default=180.0)
    ap.add_argument("--server-max-lag-s", type=float, default=0.0)
    ap.add_argument("--sources", choices=["device", "server", "both"], default="both")
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--out-path", default="data_analysis/video_power_aligned.xlsx")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    video_path = root / args.video_path
    video_stats_path = root / args.video_stats
    fps, total_frames, duration_s = get_video_meta(video_path)
    print(f"Video meta: fps={fps:.3f}, frames={total_frames}, duration={duration_s:.1f}s")

    if args.compute_video_stats or not video_stats_path.exists():
        video_df = extract_video_luma(
            video_path,
            video_stats_path,
            frame_step=args.frame_step,
            resize_w=args.resize_w,
            resize_h=args.resize_h,
            texture_scale=args.texture_scale,
            edge_thresh=args.edge_thresh,
            target_fps=args.target_fps,
        )
    else:
        video_df = load_video_stats(video_stats_path, fps=fps, video_duration_s=duration_s)

    aligned_outputs = []

    if args.sources in {"device", "both"}:
        device_path = root / "data/devicePower20251103_tidy.xlsx"
        device_df = pd.read_excel(device_path)
        for (device, cond), sub in device_df.groupby(["device_label", "condition_label"]):
            sub = sub.sort_values("t_rel_s")
            dt_s = sub["t_rel_s"].diff().dropna().median()
            video_resampled = resample_video_to_dt(video_df, dt_s)
            aligned = align_power_with_video(
                sub, "power_W", "t_rel_s", video_resampled, args.max_lag_s
            )
            aligned["source"] = "device"
            aligned["device_label"] = device
            aligned["condition_label"] = cond
            aligned_outputs.append(aligned)

    if args.sources in {"server", "both"}:
        server_path = root / "data/serverPower20251103_tidy.xlsx"
        server_df = pd.read_excel(server_path)
        server_df["total_power_W"] = server_df["power_enc_W"] + server_df["power_pck_W"]
        for cond, sub in server_df.groupby("condition_label"):
            sub = sub.sort_values("t_rel_s")
            dt_s = sub["t_rel_s"].diff().dropna().median()
            video_resampled = resample_video_to_dt(video_df, dt_s)
            aligned = align_power_with_video(
                sub, "total_power_W", "t_rel_s", video_resampled, args.server_max_lag_s
            )
            aligned["source"] = "server"
            aligned["device_label"] = "server"
            aligned["condition_label"] = cond
            aligned_outputs.append(aligned)

    if not aligned_outputs:
        print("No aligned outputs produced.")
        return

    out_df = pd.concat(aligned_outputs, ignore_index=True)
    out_path = root / args.out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_excel(out_path, index=False, sheet_name="aligned")
    print(f"Wrote aligned dataset to {out_path}")

    summary = (
        out_df
        .groupby(["source", "device_label", "condition_label"])
        .agg(
            corr_coeff=("corr_coeff", "mean"),
            lag_seconds=("lag_seconds", "mean"),
            n_samples=("power_value_W", "count"),
        )
        .reset_index()
    )
    print("\nAlignment summary:")
    print(summary)


if __name__ == "__main__":
    main()
