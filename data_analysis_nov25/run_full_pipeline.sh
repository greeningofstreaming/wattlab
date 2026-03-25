#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TARGET_FPS=2
DO_CLEAN=false

usage() {
  cat <<EOF
Usage: $(basename "$0") [--target-fps N] [--clean]

Options:
  --target-fps N   Video stats sampling FPS (default: 2)
  --clean          Run clean_outputs.sh before pipeline
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target-fps)
      TARGET_FPS="$2"
      shift 2
      ;;
    --clean)
      DO_CLEAN=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
done

echo "Running full pipeline in: ${ROOT_DIR}"
echo "Target FPS: ${TARGET_FPS}"

if ${DO_CLEAN}; then
  bash "${ROOT_DIR}/data_analysis/clean_outputs.sh"
fi

python "${ROOT_DIR}/data_analysis/clean_data.py"
python "${ROOT_DIR}/data_analysis/clean_device_power.py"

MPLCONFIGDIR=/tmp/matplotlib python "${ROOT_DIR}/data_analysis/analyze_server_power.py"
MPLCONFIGDIR=/tmp/matplotlib python "${ROOT_DIR}/data_analysis/analyze_device_power.py"

python "${ROOT_DIR}/data_analysis/align_video_power.py" --compute-video-stats --target-fps "${TARGET_FPS}"

python "${ROOT_DIR}/data_analysis/analyze_video_power_correlation.py"

python "${ROOT_DIR}/data_analysis/plot_device_luma_power_time.py"
python "${ROOT_DIR}/data_analysis/plot_server_luma_power_time.py"

echo "Pipeline complete."
