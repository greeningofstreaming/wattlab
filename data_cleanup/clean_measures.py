#!/usr/bin/env python3
"""Plot normalized power measurements from one CSV file or a directory of CSV files."""

from __future__ import annotations

import argparse
import csv
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, median, pstdev


@dataclass(frozen=True)
class SeriesData:
    label: str
    elapsed_seconds: list[float]
    native_sampling_seconds: float
    raw_values: list[float]
    normalized_values: list[float]
    gradient_values: list[float]


@dataclass(frozen=True)
class AlignmentStageResult:
    scale_name: str
    best_offset_seconds: float
    best_score: float
    best_gradient_correlation: float
    best_normalized_correlation: float


@dataclass(frozen=True)
class AlignmentResult:
    label: str
    offset_seconds: float
    score: float
    gradient_correlation: float
    normalized_correlation: float
    stages: list[AlignmentStageResult]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_path", type=Path, help="CSV file or directory of CSV files")
    parser.add_argument(
        "--pattern",
        default="*.csv",
        help="Glob pattern used when input_path is a directory",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data_cleanup/energy_measurements_plot.png"),
        help="Path where the combined plot image will be written",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory where all outputs will be written",
    )
    parser.add_argument(
        "--output-basename",
        default="energy_measurements_plot",
        help="Base filename used with --output-dir",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Display the plot interactively after saving it",
    )
    parser.add_argument(
        "--no-plots",
        action="store_true",
        help="Skip writing plot images and only export alignment outputs",
    )
    parser.add_argument(
        "--window-start-seconds",
        type=float,
        default=0.0,
        help="Start offset in seconds, relative to the beginning of each file",
    )
    parser.add_argument(
        "--window-duration-seconds",
        type=float,
        help="Optional duration in seconds for the portion of each file to plot",
    )
    parser.add_argument(
        "--coarse-resample-seconds",
        type=float,
        default=10.0,
        help="Coarsest resampling interval in seconds",
    )
    parser.add_argument(
        "--middle-resample-seconds",
        type=float,
        default=5.0,
        help="Middle resampling interval in seconds",
    )
    parser.add_argument(
        "--reference-label",
        help="Optional CSV filename to use as the alignment reference; defaults to the first series",
    )
    parser.add_argument(
        "--coarse-search-range-seconds",
        type=float,
        default=120.0,
        help="Half-width of the coarse offset search window in seconds",
    )
    parser.add_argument(
        "--middle-search-radius-seconds",
        type=float,
        default=20.0,
        help="Refinement radius around the best coarse offset in seconds",
    )
    parser.add_argument(
        "--fine-search-radius-seconds",
        type=float,
        default=6.0,
        help="Refinement radius around the best middle offset in seconds",
    )
    parser.add_argument(
        "--minimum-overlap-seconds",
        type=float,
        default=20.0,
        help="Minimum overlap duration required to score an offset",
    )
    return parser.parse_args()


def discover_input_files(input_path: Path, pattern: str) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if input_path.is_dir():
        return sorted(path for path in input_path.glob(pattern) if path.is_file())
    raise FileNotFoundError(f"Input path not found: {input_path}")


def resolve_output_path(
    output: Path,
    output_dir: Path | None,
    output_basename: str,
) -> Path:
    if output_dir is None:
        return output
    return output_dir / f"{output_basename}.png"


def read_measurements(path: Path) -> tuple[list[float], list[float]]:
    elapsed_seconds: list[float] = []
    power_values: list[float] = []

    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"Measurement CSV is empty: {path}")
        if "timestamp" not in reader.fieldnames or "power" not in reader.fieldnames:
            raise ValueError(f"CSV must contain timestamp and power columns: {path}")

        start_timestamp: datetime | None = None
        for row in reader:
            timestamp = datetime.fromisoformat(row["timestamp"].strip())
            if start_timestamp is None:
                start_timestamp = timestamp
            elapsed_seconds.append((timestamp - start_timestamp).total_seconds())
            power_values.append(float(row["power"]))

    if not elapsed_seconds:
        raise ValueError(f"Measurement CSV contains no samples: {path}")

    return elapsed_seconds, power_values


def is_measurement_csv(path: Path) -> bool:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return False
        return "timestamp" in reader.fieldnames and "power" in reader.fieldnames


def normalize_series(values: list[float]) -> list[float]:
    series_mean = mean(values)
    series_std = pstdev(values)
    if series_std == 0:
        return [0.0 for _ in values]
    return [(value - series_mean) / series_std for value in values]


def infer_sampling_interval(elapsed_seconds: list[float]) -> float:
    if len(elapsed_seconds) < 2:
        return 1.0
    deltas = [
        elapsed_seconds[index] - elapsed_seconds[index - 1]
        for index in range(1, len(elapsed_seconds))
        if elapsed_seconds[index] > elapsed_seconds[index - 1]
    ]
    if not deltas:
        return 1.0
    return median(deltas)


def compute_gradient(elapsed_seconds: list[float], values: list[float]) -> list[float]:
    if len(values) < 2:
        return [0.0 for _ in values]

    gradients: list[float] = []
    for index, value in enumerate(values):
        if index == 0:
            delta_time = elapsed_seconds[1] - elapsed_seconds[0]
            gradient = 0.0 if delta_time == 0 else (values[1] - values[0]) / delta_time
        elif index == len(values) - 1:
            delta_time = elapsed_seconds[-1] - elapsed_seconds[-2]
            gradient = 0.0 if delta_time == 0 else (values[-1] - values[-2]) / delta_time
        else:
            delta_time = elapsed_seconds[index + 1] - elapsed_seconds[index - 1]
            gradient = 0.0 if delta_time == 0 else (values[index + 1] - values[index - 1]) / delta_time
        gradients.append(gradient)
    return gradients


def interpolate_at(elapsed_seconds: list[float], values: list[float], target_second: float) -> float:
    if target_second <= elapsed_seconds[0]:
        return values[0]
    if target_second >= elapsed_seconds[-1]:
        return values[-1]

    for index in range(1, len(elapsed_seconds)):
        left_second = elapsed_seconds[index - 1]
        right_second = elapsed_seconds[index]
        if target_second > right_second:
            continue
        if right_second == left_second:
            return values[index]
        ratio = (target_second - left_second) / (right_second - left_second)
        return values[index - 1] + ratio * (values[index] - values[index - 1])

    return values[-1]


def resample_series(
    elapsed_seconds: list[float],
    values: list[float],
    resample_seconds: float,
) -> tuple[list[float], list[float]]:
    if resample_seconds <= 0:
        raise ValueError("Resampling interval must be positive.")
    if not elapsed_seconds:
        raise ValueError("Cannot resample an empty series.")
    if len(elapsed_seconds) == 1:
        return [0.0], [values[0]]

    end_second = elapsed_seconds[-1]
    resampled_elapsed: list[float] = []
    resampled_values: list[float] = []

    current_second = 0.0
    while current_second <= end_second:
        resampled_elapsed.append(current_second)
        resampled_values.append(interpolate_at(elapsed_seconds, values, current_second))
        current_second += resample_seconds

    if resampled_elapsed[-1] < end_second:
        resampled_elapsed.append(end_second)
        resampled_values.append(interpolate_at(elapsed_seconds, values, end_second))

    return resampled_elapsed, resampled_values


def pearson_correlation(values_a: list[float], values_b: list[float]) -> float:
    if len(values_a) != len(values_b):
        raise ValueError("Series must have the same length to compute correlation.")
    if len(values_a) < 2:
        return 0.0

    mean_a = mean(values_a)
    mean_b = mean(values_b)
    centered_a = [value - mean_a for value in values_a]
    centered_b = [value - mean_b for value in values_b]
    numerator = sum(value_a * value_b for value_a, value_b in zip(centered_a, centered_b))
    denominator_a = math.sqrt(sum(value * value for value in centered_a))
    denominator_b = math.sqrt(sum(value * value for value in centered_b))
    if denominator_a == 0 or denominator_b == 0:
        return 0.0
    return numerator / (denominator_a * denominator_b)


def score_offset(
    reference_series: SeriesData,
    candidate_series: SeriesData,
    offset_seconds: float,
    minimum_overlap_seconds: float,
) -> tuple[float, float, float] | None:
    overlap_start = max(reference_series.elapsed_seconds[0], candidate_series.elapsed_seconds[0] + offset_seconds)
    overlap_end = min(reference_series.elapsed_seconds[-1], candidate_series.elapsed_seconds[-1] + offset_seconds)
    if overlap_end - overlap_start < minimum_overlap_seconds:
        return None

    common_times = [
        elapsed_second
        for elapsed_second in reference_series.elapsed_seconds
        if overlap_start <= elapsed_second <= overlap_end
    ]
    if len(common_times) < 2:
        return None

    shifted_candidate_times = [elapsed_second - offset_seconds for elapsed_second in common_times]
    reference_normalized = [
        interpolate_at(reference_series.elapsed_seconds, reference_series.normalized_values, elapsed_second)
        for elapsed_second in common_times
    ]
    candidate_normalized = [
        interpolate_at(candidate_series.elapsed_seconds, candidate_series.normalized_values, elapsed_second)
        for elapsed_second in shifted_candidate_times
    ]
    reference_gradient = [
        interpolate_at(reference_series.elapsed_seconds, reference_series.gradient_values, elapsed_second)
        for elapsed_second in common_times
    ]
    candidate_gradient = [
        interpolate_at(candidate_series.elapsed_seconds, candidate_series.gradient_values, elapsed_second)
        for elapsed_second in shifted_candidate_times
    ]

    normalized_correlation = pearson_correlation(reference_normalized, candidate_normalized)
    gradient_correlation = pearson_correlation(reference_gradient, candidate_gradient)
    score = (0.7 * gradient_correlation) + (0.3 * normalized_correlation)
    return score, gradient_correlation, normalized_correlation


def generate_offset_candidates(center_seconds: float, radius_seconds: float, step_seconds: float) -> list[float]:
    if step_seconds <= 0:
        raise ValueError("Offset step must be positive.")
    candidate_count = int(round((2 * radius_seconds) / step_seconds))
    offsets = [
        center_seconds - radius_seconds + (index * step_seconds)
        for index in range(candidate_count + 1)
    ]
    if offsets[-1] != center_seconds + radius_seconds:
        offsets.append(center_seconds + radius_seconds)
    return offsets


def search_best_offset(
    reference_series: SeriesData,
    candidate_series: SeriesData,
    center_seconds: float,
    radius_seconds: float,
    step_seconds: float,
    minimum_overlap_seconds: float,
    scale_name: str,
) -> AlignmentStageResult:
    best_result: tuple[float, float, float, float] | None = None

    for offset_seconds in generate_offset_candidates(center_seconds, radius_seconds, step_seconds):
        scored = score_offset(
            reference_series,
            candidate_series,
            offset_seconds=offset_seconds,
            minimum_overlap_seconds=minimum_overlap_seconds,
        )
        if scored is None:
            continue
        score, gradient_correlation, normalized_correlation = scored
        if best_result is None or score > best_result[1]:
            best_result = (
                offset_seconds,
                score,
                gradient_correlation,
                normalized_correlation,
            )

    if best_result is None:
        return AlignmentStageResult(
            scale_name=scale_name,
            best_offset_seconds=center_seconds,
            best_score=float("-inf"),
            best_gradient_correlation=0.0,
            best_normalized_correlation=0.0,
        )

    return AlignmentStageResult(
        scale_name=scale_name,
        best_offset_seconds=best_result[0],
        best_score=best_result[1],
        best_gradient_correlation=best_result[2],
        best_normalized_correlation=best_result[3],
    )


def apply_time_window(
    elapsed_seconds: list[float],
    values: list[float],
    window_start_seconds: float,
    window_duration_seconds: float | None,
) -> tuple[list[float], list[float]]:
    if window_duration_seconds is None:
        window_end_seconds = None
    else:
        window_end_seconds = window_start_seconds + window_duration_seconds

    filtered_pairs = [
        (elapsed_second, value)
        for elapsed_second, value in zip(elapsed_seconds, values)
        if elapsed_second >= window_start_seconds
        and (window_end_seconds is None or elapsed_second <= window_end_seconds)
    ]

    if not filtered_pairs:
        raise ValueError(
            "No samples remain after applying the requested time window."
        )

    filtered_elapsed = [elapsed_second - window_start_seconds for elapsed_second, _ in filtered_pairs]
    filtered_values = [value for _, value in filtered_pairs]
    return filtered_elapsed, filtered_values


def prepare_series_data(
    input_files: list[Path],
    window_start_seconds: float,
    window_duration_seconds: float | None,
) -> list[SeriesData]:
    series_data: list[SeriesData] = []
    for csv_path in input_files:
        if not is_measurement_csv(csv_path):
            print(f"Skipping non-measurement CSV: {csv_path}")
            continue
        elapsed_seconds, power_values = read_measurements(csv_path)
        elapsed_seconds, power_values = apply_time_window(
            elapsed_seconds,
            power_values,
            window_start_seconds=window_start_seconds,
            window_duration_seconds=window_duration_seconds,
        )
        normalized_values = normalize_series(power_values)
        gradient_values = compute_gradient(elapsed_seconds, normalized_values)
        series_data.append(
            SeriesData(
                label=csv_path.name,
                elapsed_seconds=elapsed_seconds,
                native_sampling_seconds=infer_sampling_interval(elapsed_seconds),
                raw_values=power_values,
                normalized_values=normalized_values,
                gradient_values=gradient_values,
            )
        )
    return series_data


def build_gradient_output_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}_gradient{output_path.suffix}")


def build_resampled_output_path(output_path: Path, scale_name: str) -> Path:
    return output_path.with_name(f"{output_path.stem}_resampled_{scale_name}{output_path.suffix}")


def build_aligned_output_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}_aligned{output_path.suffix}")


def build_alignment_summary_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}_alignment_summary.csv")


def build_alignment_workbook_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}_aligned_measures.xlsx")


def plot_series(
    plt,
    series_data: list[SeriesData],
    value_getter: str,
    title: str,
    y_label: str,
    output_path: Path,
) -> None:
    plt.figure(figsize=(12, 7))
    for series in series_data:
        plt.plot(
            series.elapsed_seconds,
            getattr(series, value_getter),
            linewidth=1.5,
            label=series.label,
        )

    plt.title(title)
    plt.xlabel("Elapsed Time Since Window Start (s)")
    plt.ylabel(y_label)
    plt.grid(True, alpha=0.3)
    plt.legend()
    plt.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, dpi=150)
    plt.close()


def plot_aligned_series(
    plt,
    series_data: list[SeriesData],
    alignment_results: dict[str, AlignmentResult],
    reference_label: str,
    output_path: Path,
) -> None:
    plt.figure(figsize=(12, 7))
    for series in series_data:
        offset_seconds = 0.0 if series.label == reference_label else alignment_results[series.label].offset_seconds
        shifted_elapsed = [elapsed_second + offset_seconds for elapsed_second in series.elapsed_seconds]
        plt.plot(
            shifted_elapsed,
            series.normalized_values,
            linewidth=1.5,
            label=f"{series.label} (offset {offset_seconds:+.1f}s)",
        )

    plt.title("Aligned Normalized Energy Measurements")
    plt.xlabel("Aligned Time (s)")
    plt.ylabel("Normalized Power (z-score)")
    plt.grid(True, alpha=0.3)
    plt.legend()
    plt.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, dpi=150)
    plt.close()


def build_resampled_series_data(
    series_data: list[SeriesData],
    resample_seconds: float | None,
) -> list[SeriesData]:
    resampled_data: list[SeriesData] = []
    for series in series_data:
        target_step = series.native_sampling_seconds if resample_seconds is None else resample_seconds
        resampled_elapsed, resampled_values = resample_series(
            series.elapsed_seconds,
            series.normalized_values,
            resample_seconds=target_step,
        )
        resampled_data.append(
            SeriesData(
                label=series.label,
                elapsed_seconds=resampled_elapsed,
                native_sampling_seconds=target_step,
                raw_values=[interpolate_at(series.elapsed_seconds, series.raw_values, second) for second in resampled_elapsed],
                normalized_values=resampled_values,
                gradient_values=compute_gradient(resampled_elapsed, resampled_values),
            )
        )
    return resampled_data


def select_reference_label(series_data: list[SeriesData], requested_label: str | None) -> str:
    available_labels = {series.label for series in series_data}
    if requested_label is None:
        return series_data[0].label
    if requested_label not in available_labels:
        raise ValueError(
            f"Reference label {requested_label!r} not found. Available labels: {sorted(available_labels)}"
        )
    return requested_label


def index_series_by_label(series_data: list[SeriesData]) -> dict[str, SeriesData]:
    return {series.label: series for series in series_data}


def align_series_multiscale(
    fine_series_data: list[SeriesData],
    middle_series_data: list[SeriesData],
    coarse_series_data: list[SeriesData],
    reference_label: str,
    coarse_search_range_seconds: float,
    middle_search_radius_seconds: float,
    fine_search_radius_seconds: float,
    middle_step_seconds: float,
    coarse_step_seconds: float,
    minimum_overlap_seconds: float,
) -> dict[str, AlignmentResult]:
    fine_by_label = index_series_by_label(fine_series_data)
    middle_by_label = index_series_by_label(middle_series_data)
    coarse_by_label = index_series_by_label(coarse_series_data)
    results: dict[str, AlignmentResult] = {}

    reference_fine = fine_by_label[reference_label]
    reference_middle = middle_by_label[reference_label]
    reference_coarse = coarse_by_label[reference_label]

    for label, candidate_fine in fine_by_label.items():
        if label == reference_label:
            results[label] = AlignmentResult(
                label=label,
                offset_seconds=0.0,
                score=1.0,
                gradient_correlation=1.0,
                normalized_correlation=1.0,
                stages=[
                    AlignmentStageResult("coarse", 0.0, 1.0, 1.0, 1.0),
                    AlignmentStageResult("middle", 0.0, 1.0, 1.0, 1.0),
                    AlignmentStageResult("fine", 0.0, 1.0, 1.0, 1.0),
                ],
            )
            continue

        coarse_stage = search_best_offset(
            reference_coarse,
            coarse_by_label[label],
            center_seconds=0.0,
            radius_seconds=coarse_search_range_seconds,
            step_seconds=coarse_step_seconds,
            minimum_overlap_seconds=minimum_overlap_seconds,
            scale_name="coarse",
        )
        middle_stage = search_best_offset(
            reference_middle,
            middle_by_label[label],
            center_seconds=coarse_stage.best_offset_seconds,
            radius_seconds=middle_search_radius_seconds,
            step_seconds=middle_step_seconds,
            minimum_overlap_seconds=minimum_overlap_seconds,
            scale_name="middle",
        )
        fine_step_seconds = max(
            candidate_fine.native_sampling_seconds,
            reference_fine.native_sampling_seconds,
        )
        fine_stage = search_best_offset(
            reference_fine,
            candidate_fine,
            center_seconds=middle_stage.best_offset_seconds,
            radius_seconds=fine_search_radius_seconds,
            step_seconds=fine_step_seconds,
            minimum_overlap_seconds=minimum_overlap_seconds,
            scale_name="fine",
        )
        results[label] = AlignmentResult(
            label=label,
            offset_seconds=fine_stage.best_offset_seconds,
            score=fine_stage.best_score,
            gradient_correlation=fine_stage.best_gradient_correlation,
            normalized_correlation=fine_stage.best_normalized_correlation,
            stages=[coarse_stage, middle_stage, fine_stage],
        )

    return results


def write_alignment_summary(
    output_path: Path,
    alignment_results: dict[str, AlignmentResult],
) -> None:
    fieldnames = [
        "label",
        "final_offset_seconds",
        "final_score",
        "final_gradient_correlation",
        "final_normalized_correlation",
        "coarse_offset_seconds",
        "coarse_score",
        "middle_offset_seconds",
        "middle_score",
        "fine_offset_seconds",
        "fine_score",
    ]
    with output_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for result in alignment_results.values():
            stages = {stage.scale_name: stage for stage in result.stages}
            writer.writerow(
                {
                    "label": result.label,
                    "final_offset_seconds": f"{result.offset_seconds:.6f}",
                    "final_score": f"{result.score:.6f}",
                    "final_gradient_correlation": f"{result.gradient_correlation:.6f}",
                    "final_normalized_correlation": f"{result.normalized_correlation:.6f}",
                    "coarse_offset_seconds": f"{stages['coarse'].best_offset_seconds:.6f}",
                    "coarse_score": f"{stages['coarse'].best_score:.6f}",
                    "middle_offset_seconds": f"{stages['middle'].best_offset_seconds:.6f}",
                    "middle_score": f"{stages['middle'].best_score:.6f}",
                    "fine_offset_seconds": f"{stages['fine'].best_offset_seconds:.6f}",
                    "fine_score": f"{stages['fine'].best_score:.6f}",
                }
            )


def interpolate_if_in_range(series: SeriesData, values: list[float], target_second: float) -> float | None:
    if target_second < series.elapsed_seconds[0] or target_second > series.elapsed_seconds[-1]:
        return None
    return interpolate_at(series.elapsed_seconds, values, target_second)


def write_aligned_measures_workbook(
    output_path: Path,
    fine_series_data: list[SeriesData],
    alignment_results: dict[str, AlignmentResult],
    reference_label: str,
) -> None:
    from openpyxl import Workbook

    series_by_label = index_series_by_label(fine_series_data)
    reference_series = series_by_label[reference_label]

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "aligned_measures"

    headers = ["relative_time_seconds"] + [series.label for series in fine_series_data]
    worksheet.append(headers)

    for reference_time in reference_series.elapsed_seconds:
        row: list[float | None] = [reference_time]
        for series in fine_series_data:
            offset_seconds = 0.0 if series.label == reference_label else alignment_results[series.label].offset_seconds
            source_time = reference_time - offset_seconds
            value = interpolate_if_in_range(series, series.raw_values, source_time)
            row.append(value)
        worksheet.append(row)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_path)


def main() -> None:
    args = parse_args()
    input_files = discover_input_files(args.input_path, args.pattern)
    output_path = resolve_output_path(args.output, args.output_dir, args.output_basename)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not input_files:
        raise ValueError(f"No CSV files found in {args.input_path}")

    mpl_config_dir = Path(__file__).resolve().parent / ".mplconfig"
    mpl_config_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(mpl_config_dir))
    os.environ.setdefault("XDG_CACHE_HOME", str(mpl_config_dir))

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    series_data = prepare_series_data(
        input_files,
        window_start_seconds=args.window_start_seconds,
        window_duration_seconds=args.window_duration_seconds,
    )
    if not series_data:
        raise ValueError(f"No valid measurement CSV files found in {args.input_path}")

    gradient_output = build_gradient_output_path(output_path)
    coarse_resampled_data = build_resampled_series_data(
        series_data,
        resample_seconds=args.coarse_resample_seconds,
    )
    middle_resampled_data = build_resampled_series_data(
        series_data,
        resample_seconds=args.middle_resample_seconds,
    )
    fine_resampled_data = build_resampled_series_data(
        series_data,
        resample_seconds=None,
    )
    coarse_output = build_resampled_output_path(output_path, "coarse")
    middle_output = build_resampled_output_path(output_path, "middle")
    fine_output = build_resampled_output_path(output_path, "fine")
    reference_label = select_reference_label(series_data, args.reference_label)
    alignment_results = align_series_multiscale(
        fine_series_data=fine_resampled_data,
        middle_series_data=middle_resampled_data,
        coarse_series_data=coarse_resampled_data,
        reference_label=reference_label,
        coarse_search_range_seconds=args.coarse_search_range_seconds,
        middle_search_radius_seconds=args.middle_search_radius_seconds,
        fine_search_radius_seconds=args.fine_search_radius_seconds,
        middle_step_seconds=args.middle_resample_seconds,
        coarse_step_seconds=args.coarse_resample_seconds,
        minimum_overlap_seconds=args.minimum_overlap_seconds,
    )
    aligned_output = build_aligned_output_path(output_path)
    alignment_summary_output = build_alignment_summary_path(output_path)
    alignment_workbook_output = build_alignment_workbook_path(output_path)
    write_alignment_summary(alignment_summary_output, alignment_results)
    write_aligned_measures_workbook(
        alignment_workbook_output,
        fine_resampled_data,
        alignment_results=alignment_results,
        reference_label=reference_label,
    )
    if not args.no_plots:
        plot_series(
            plt,
            series_data,
            value_getter="normalized_values",
            title="Normalized Energy Measurements",
            y_label="Normalized Power (z-score)",
            output_path=output_path,
        )
        plot_series(
            plt,
            series_data,
            value_getter="gradient_values",
            title="Gradient of Normalized Energy Measurements",
            y_label="Gradient of Normalized Power",
            output_path=gradient_output,
        )
        plot_series(
            plt,
            coarse_resampled_data,
            value_getter="normalized_values",
            title=f"Normalized Energy Measurements Resampled at {args.coarse_resample_seconds:g}s",
            y_label="Normalized Power (z-score)",
            output_path=coarse_output,
        )
        plot_series(
            plt,
            middle_resampled_data,
            value_getter="normalized_values",
            title=f"Normalized Energy Measurements Resampled at {args.middle_resample_seconds:g}s",
            y_label="Normalized Power (z-score)",
            output_path=middle_output,
        )
        plot_series(
            plt,
            fine_resampled_data,
            value_getter="normalized_values",
            title="Normalized Energy Measurements Resampled at Native Cadence",
            y_label="Normalized Power (z-score)",
            output_path=fine_output,
        )
        plot_aligned_series(
            plt,
            fine_resampled_data,
            alignment_results=alignment_results,
            reference_label=reference_label,
            output_path=aligned_output,
        )
        print(f"Saved normalized plot to {output_path}")
        print(f"Saved gradient plot to {gradient_output}")
        print(f"Saved coarse resampled plot to {coarse_output}")
        print(f"Saved middle resampled plot to {middle_output}")
        print(f"Saved fine resampled plot to {fine_output}")
        print(f"Saved aligned plot to {aligned_output}")
    print(f"Saved alignment summary to {alignment_summary_output}")
    print(f"Saved aligned measures workbook to {alignment_workbook_output}")
    print(f"Reference series: {reference_label}")
    for label, result in alignment_results.items():
        print(
            f"{label}: offset={result.offset_seconds:+.1f}s "
            f"score={result.score:.3f} "
            f"gradient_corr={result.gradient_correlation:.3f} "
            f"normalized_corr={result.normalized_correlation:.3f}"
        )

    if args.show:
        plt.show()


if __name__ == "__main__":
    main()
