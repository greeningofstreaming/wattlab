#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "Cleaning derived outputs under: ${ROOT_DIR}"

rm -f \
  "${ROOT_DIR}/data/serverPower20251103_tidy.xlsx" \
  "${ROOT_DIR}/data/devicePower20251103_tidy.xlsx" \
  "${ROOT_DIR}/data/server_power_report.md" \
  "${ROOT_DIR}/data/server_power_report.pdf" \
  "${ROOT_DIR}/data/server_power_report.tex" \
  "${ROOT_DIR}/data/device_power_report.md" \
  "${ROOT_DIR}/data/device_power_report.pdf" \
  "${ROOT_DIR}/data/device_power_report.tex" \
  "${ROOT_DIR}/data/enc_power_1080p_bitrate.pdf" \
  "${ROOT_DIR}/data/enc_power_6mbps_resolution.pdf" \
  "${ROOT_DIR}/data/pkg_power_1080p_bitrate.pdf" \
  "${ROOT_DIR}/data/pkg_power_6mbps_resolution.pdf" \
  "${ROOT_DIR}/data/avg_enc_power_per_condition.pdf" \
  "${ROOT_DIR}/data/avg_pck_power_per_condition.pdf" \
  "${ROOT_DIR}/data/device_avg_power_per_condition.pdf" \
  "${ROOT_DIR}/data/device_norm_pct_vs_baseline.pdf" \
  "${ROOT_DIR}/data/device_norm_zscore.pdf" \
  "${ROOT_DIR}/data/correlation_summary.xlsx"

rm -f \
  "${ROOT_DIR}/data_analysis/video_power_aligned.xlsx" \
  "${ROOT_DIR}/data_analysis/video_stats/full_video_luma.xlsx"

rm -rf "${ROOT_DIR}/data/correlation_plots"

echo "Done."
