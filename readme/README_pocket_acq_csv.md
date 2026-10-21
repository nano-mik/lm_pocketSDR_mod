# PocketSDR Kasami Acquisition — CSV Export (Current Implementation)

## Scope

This README documents `pocket_acq_custKasami_patched.py` and the minimal-diff patch `pocket_acq_csv_minimal.patch`, based on the supplied `pocket_acq_custKasami.py`. It describes **implemented behavior**, not planned batch processing or 3D matrix export.

The script uses existing PocketSDR correlation routines to acquire GPS PRN signals and, in **single-PRN mode**, optionally evaluate a Kasami code at the acquired PRN's Doppler/code-offset bin. The patch adds CSV output; it does not introduce a new acquisition or detection algorithm.

## Requirements

- Python 3 with NumPy and Matplotlib
- The existing PocketSDR modules `sdr_func` and `sdr_code` and their normal dependencies
- A supported IQ/IF recording readable by `sdr_func.read_data()`

Keep the script in an environment where the PocketSDR modules can be imported. No changes to those modules are included in this patch.

## Installation

Apply the patch to the corresponding source revision from the repository root (where `pocket_acq_custKasami.py` resides):

```bash
git apply pocket_acq_csv_minimal.patch
```

Alternatively, use the supplied `pocket_acq_custKasami_patched.py` as a standalone replacement after checking its dependencies and file paths.

## Usage

```bash
python3 pocket_acq_custKasami.py -prn 1 -wm -np -toff 0 -tint 10 -csv results.csv recording.bin
```

For several PRNs:

```bash
python3 pocket_acq_custKasami.py -prn 1-32 -np -toff 0 -tint 10 -csv results.csv recording.bin
```

**Important:** `-wm` currently evaluates Kasami **only in single-PRN mode**. In the multi-PRN example, the output contains the selected PRNs but **no Kasami row**, even if `-wm` is supplied. The current implementation does not produce 33 signals in one invocation.

### Relevant arguments

| Argument | Meaning |
| --- | --- |
| `-prn 1` or `-prn 1-32` | One or more PRNs |
| `-wm` | Evaluate Kasami at the reference PRN's detected bin (single-PRN mode only) |
| `-np` | Suppress plots; CSV export still works |
| `-csv path.csv` | Write one sectioned CSV for this invocation; overwrites existing file |
| `-toff value` | Offset from start of recording, **milliseconds** (argument parsing fixed by patch) |
| `-tint value` | Requested integration time, **milliseconds** |
| `-f value` | Sampling rate, MHz |
| `-fi value` | IF frequency, MHz |
| `-IQ` | IQ input mode |
| `-d value` | Maximum Doppler search, Hz |
| `-sdr value` | Reader/SDR format selector used by existing code |
| `-p`, `-l`, `-3d` | Existing plotting modes; `-3d` displays a surface plot but does **not** export its matrix |

The script's built-in defaults and input decoding behavior are inherited from the original version. `-tint` may be raised to at least one signal code period by the existing code.

## CSV format

Each invocation with `-csv` writes **one file**, comprising metadata lines followed by three named sections. This is a **sectioned CSV**, not a single conventional rectangular table. The later batch layer must parse the section markers.

### Metadata header

```text
# schema_version: 1
# recording_file: recording.bin
# sampling_hz: ...
# if_hz: ...
# time_offset_origin: recording_start
# score: dimensionless_z_score
```

The current exporter does **not** write a batch ID, reference-PRN list, or complete CLI configuration. Offset and integration time appear in the summary rows.

### `[SUMMARY]`

One row per evaluated signal, with columns:

```text
job_id,code_type,code_id,time_offset_ms,integration_time_ms,correlation_power,z_score,background_mean,background_std,code_offset_ms,doppler_hz,doppler_bin_hz
```

- `code_type` is `PRN` or `KASAMI`.
- `code_id` is the PRN number supplied to the acquisition/Kasami code selection.
- `correlation_power` is the matrix value at the evaluated bin.
- `z_score` is **dimensionless**, not C/N₀ in dB-Hz, despite some inherited console/plot labels.
- `background_mean` and `background_std` come from `noise_stats(P, 2, 32)`; the exclusion window is centered on the matrix's own maximum.
- `code_offset_ms` is the evaluated code-offset bin; `doppler_bin_hz` is its discrete Doppler bin; `doppler_hz` is the PRN's refined Doppler estimate.
- `job_id` is currently always `1` because each invocation is one job.

### `[CODE_OFFSET_CORRELATIONS]`

```text
job_id,code_type,code_id,doppler_ref_hz,code_offset_ms,correlation_power
```

One row per code-offset sample, taken at the evaluated Doppler bin. For PRNs this is their acquired peak bin; for Kasami it is the reference PRN's bin.

### `[DOPPLER_CORRELATIONS]`

```text
job_id,code_type,code_id,code_offset_ref_ms,doppler_hz,correlation_power
```

One row per Doppler bin, taken at the evaluated code-offset bin. Again, Kasami uses the reference PRN's bin.

## Interpretation

The two 1D correlation curves are slices of the full code-offset × Doppler correlation surface; they cross at the **evaluated** bin. PRN evaluation uses each PRN's own detected maximum. Kasami evaluation uses the reference PRN's maximum and **does not independently search for Kasami's best bin**.

The Kasami flag `-wm` controls whether to run this check; it is **not** a statement that a watermark was actually present in the recording. For negative-control recordings, the check can still be run using `-wm`.

## Current limitations and next steps

- **No `.npz` matrix export yet.** The proposed `-matrix` option is **not implemented**. The existing `-3d` switch is display-only.
- **No batch consolidation.** Reusing the same `-csv` filename overwrites the previous job's file. A future batch runner will combine offsets/integration times into one CSV per recording and assign unique job IDs.
- **No Kasami evaluation in multi-PRN mode.** To capture Kasami in the current version, run a single reference PRN with `-wm`.
- **No automatic insufficient-data skip status.** That belongs to the future batch runner; the current script does not produce `job_status.csv`.
- **No Excel graphs/statistical workbook yet.** That is a separate planned analysis stage.
- The patch has been syntax-checked, but end-to-end validation with a real recording and its PocketSDR dependencies has not been completed.

## Minimal-diff changes

The patch adds a CSV export function, `-csv` CLI handling, and collection of existing PRN/Kasami correlation arrays; corrects `-toff` parsing; removes an unsupported `ms=` argument from the multi-PRN acquisition call; and ensures single-PRN Kasami results can be exported even with `-np`. It does not modify the existing correlation computation modules.
