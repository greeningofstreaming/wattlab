# Clean Measures Readme
## `clean_measures.py`
For comments/bugs tania@vividmanta.com

This script accompanies the dual_measure_tool developed by Simon Jones (https://github.com/stj2022/dual_measure_tool), allowing to locally capture Tapo plug measurements. 

The script loads Tapo-style measurement CSV files, trims them to a chosen time window, normalizes them, computes gradients, performs multiscale correlation-based alignment, and exports both plots and aligned data tables. Each measured condition should be in its own CSV file, containing the timestamps and corresponding energy measures. 

It is intended for hackathon or experiment folders that contain several CSV files with a schema like:

```csv
timestamp,power
2026-03-15T19:48:55.212116,202323.0
2026-03-15T19:48:57.247158,202136.0
```

Non-measurement CSV files in the same folder are skipped automatically.

## What The Script Does

For each valid CSV file, the script:

1. Reads `timestamp` and `power`.
2. Converts timestamps into relative elapsed seconds from the beginning of that file.
3. Optionally keeps only a selected time window.
4. Normalizes each kept series with its own mean and standard deviation.
5. Computes the gradient of the normalized series.
6. Builds three resampled versions:
   - coarse scale
   - middle scale
   - fine scale
7. Aligns all series to a reference series using correlation-based multiscale offset search.
8. Exports plots, an alignment summary CSV, and an aligned Excel workbook.

## Alignment Logic

Alignment is done in three passes:

1. Coarse pass:
   - uses the coarse resampling interval, default `10s`
   - searches a wide offset range
2. Middle pass:
   - uses the middle resampling interval, default `5s`
   - refines around the best coarse offset
3. Fine pass:
   - uses the inferred native cadence of each series
   - refines around the best middle offset

The offset score is:

```text
0.7 * gradient correlation + 0.3 * normalized-series correlation
```

This means the search emphasizes transitions, while still using the normalized signal shape as a secondary check.

## Basic Usage

Run on a folder of CSV files:

```bash
python data_cleanup/clean_measures.py /path/to/measurements
```

Run on a single CSV file:

```bash
python data_cleanup/clean_measures.py /path/to/file.csv
```

Write all outputs into a directory:

```bash
python data_cleanup/clean_measures.py /path/to/measurements \
  --output-dir /path/to/results \
  --output-basename run1
```

Skip plots and export only alignment tables:

```bash
python data_cleanup/clean_measures.py /path/to/measurements \
  --output-dir /path/to/results \
  --output-basename run1 \
  --no-plots
```

## Main Options

### Input Selection

- `input_path`
  - CSV file or directory of CSV files.
- `--pattern`
  - Glob pattern when `input_path` is a directory.
  - Default: `*.csv`

### Output Control

- `--output`
  - Full output filename for the main plot.
  - Other outputs are derived from the same stem.
  - Default: `data_cleanup/energy_measurements_plot.png`
- `--output-dir`
  - Treat this path as a directory and place all outputs inside it.
- `--output-basename`
  - Base filename used together with `--output-dir`.
  - Default: `energy_measurements_plot`
- `--no-plots`
  - Do not create PNG plots.
  - Still writes the alignment summary CSV and aligned Excel workbook.
- `--show`
  - Displays plots interactively after saving them.

### Windowing
If some measurement series were left running after the end of the sequence, you might want to precise a window where the useful data is.

- `--window-start-seconds`
  - Start offset relative to the beginning of each file.
  - Default: `0`
- `--window-duration-seconds`
  - Duration of the segment to keep from each file.
  - If omitted, keeps data from `window-start-seconds` to the end.

This window is applied before normalization and alignment. Data outside the window are basically ignored and not included in the final excel file.

### Resampling

- `--coarse-resample-seconds`
  - Coarsest interval used for the first alignment pass.
  - Default: `10`
- `--middle-resample-seconds`
  - Middle interval used for the second alignment pass.
  - Default: `5`

The fine pass uses the inferred native cadence of the data after windowing. This will need to be adjusted depending on the sampling rate you have used for your measures. It is assumed that all measurement series use the same sampling rate. 

### Alignment Search

- `--reference-label`
  - CSV filename to use as the reference series.
  - If omitted, the first valid series is used.
- `--coarse-search-range-seconds`
  - Half-width of the coarse offset search window.
  - Default: `120`
- `--middle-search-radius-seconds`
  - Search radius around the best coarse offset.
  - Default: `20`
- `--fine-search-radius-seconds`
  - Search radius around the best middle offset.
  - Default: `6`
- `--minimum-overlap-seconds`
  - Minimum overlap duration required to score an offset.
  - Default: `20`

## Outputs

When plots are enabled, the script writes:

- Main normalized plot:
  - `<basename>.png`
- Gradient plot:
  - `<basename>_gradient.png`
- Coarse resampled normalized plot:
  - `<basename>_resampled_coarse.png`
- Middle resampled normalized plot:
  - `<basename>_resampled_middle.png`
- Fine resampled normalized plot:
  - `<basename>_resampled_fine.png`
- Aligned normalized plot - this is the most useful one and probably good to verify before doing further analysis to ensure that the alignment looks sane:
  - `<basename>_aligned.png`

It always writes:

- Alignment summary CSV:
  - `<basename>_alignment_summary.csv`
- Aligned measures Excel workbook:
  - `<basename>_aligned_measures.xlsx`

## Alignment Summary CSV

The alignment summary CSV contains one row per series with:

- final offset in seconds
- final combined score
- final gradient correlation
- final normalized correlation
- best coarse offset and score
- best middle offset and score
- best fine offset and score

## Aligned Excel Workbook

The Excel workbook contains one sheet named `aligned_measures`.

Columns:

- `relative_time_seconds`
- one column per input series

Details:

- time is relative to the reference series start
- each series is shifted by its estimated alignment offset
- exported values are raw power measurements
- values are interpolated on the fine-scale reference time grid
- cells are blank where a shifted series has no overlap (start and end)

## Notes

- The script aligns based on shape similarity, not explicit cue detection.
- Correlation quality depends heavily on the chosen time window.
- Long useless tails should usually be removed with `--window-start-seconds` and `--window-duration-seconds`.
- If your folder already contains analysis CSV files, they are ignored unless they also have `timestamp` and `power` columns.
- The CSV files themselves are (normally) not modified.
