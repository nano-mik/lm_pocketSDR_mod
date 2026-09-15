# C64 NAV Verifier

`c64_nav_verify.py` recovers a 50 bit/s BPSK NAV stream from a GNU Radio
complex64 recording spread by a manually selected PRN. It searches Doppler and
code phase, tracks the code once per code period, removes residual carrier
rotation with a BPSK-insensitive estimator, finds the NAV bit boundary, and can
compare the recovered stream with an expected message.

## Requirements

- Python 3.10 or newer
- NumPy

Install NumPy if needed:

```bash
python3 -m pip install numpy
```

On Ubuntu, install Tkinter once if the GUI is unavailable:

```bash
sudo apt install python3-tk
```

## GUI

Open the graphical interface by running the script without arguments:

```bash
python3 c64_nav_verify.py
```

Alternatively, use `python3 c64_nav_verify.py --gui`. The GUI provides browse
buttons for the IQ, PRN, expected NAV, and output paths. All receiver parameters
can be changed before pressing **Run verification**. Processing runs separately
so the window remains responsive, and the result or error is shown in its log.

The visual bit checker uses these colors:

- Green: matched bit
- Black: missing or extra bit
- Red: isolated wrong bit
- Yellow: possible polarity-flip run
- Blue: normal-polarity preamble
- Purple: inverted-polarity preamble
- Dark blue: five-bit SF ID following a normal preamble
- Dark purple: five-bit SF ID following an inverted preamble

By default, a window of eight bits with at least 60% opposite bits is marked as
a possible flip. This tolerates a gap inside an otherwise inverted region.
Change **Possible flip run (bits)** to make this classification more or less
sensitive. **Bits per visual row** controls the line width and defaults to 30
bits—one NAV word. Each row shows its corresponding aligned-bit range.

Enable **Align columns to detected subframe/word boundary** to place confirmed
preambles at column 1 while leaving them on their natural later rows. The
recording is not trimmed or moved to row 1. Black `-` placeholders pad the
leading and trailing partial words. This option is disabled by default.

The editable preamble defaults to `10100111` (`0xA7`). It accepts compact form
or spaced form (`1 0 1 0 0 1 1 1`). The JSON report lists every exact recovered-bit location
under `preamble_positions_zero_based`.

With an 8-bit preamble, the verifier also parses each complete 300-bit candidate
as ten 30-bit words:

- Word 1: 8-bit preamble, 5-bit SF ID, 4-bit word ID, 7-bit data, 6-bit CRC
- Words 2–10: 13-bit data, 4-bit word ID, 7-bit data, 6-bit CRC

CRC fields are displayed but intentionally not validated. Because the message
repeats and the recording may begin mid-subframe, candidates are found by their
preamble rather than by assuming the recording starts at SF1. Normal and
inverted preambles are searched independently.

## Typical command

The defaults match the PocketSDR parameters: 16.368 Msps, 4.013 MHz IF,
1.023 Mcps, a 1023-chip/1 ms PRN period, and 50 bit/s NAV data.

```bash
python3 c64_nav_verify.py recording.c64 \
  --prn custom_PRN.txt \
  --prn-row 1 \
  --expected-nav navmsg.bin \
  --preamble 10001011 \
  --output-prefix result/nav_recovery
```

For `GPSL1CA32.txt`, `--prn-row 1` selects the first row and
`--prn-row 32` selects the last. Rows are numbered from 1. A metadata/header
line in a custom text file is ignored.

## Input formats

- IQ: GNU Radio complex64 (`complex<float>`), normally little-endian I then Q.
- PRN text: one code per row, either compact (`11001...`) or space-separated
  (`1 1 0 0 1 ...`).
- PRN binary: byte-per-bit when every byte is 0 or 1; otherwise packed MSB-first.
- NAV text: compact or whitespace-separated bits.
- NAV binary: detected automatically. A file such as the supplied 375-byte
  `navmsg.bin` is interpreted as 3000 packed, MSB-first bits. At 50 bit/s,
  those 3000 bits are exactly 60 seconds (one minute) of NAV data.

Use `--nav-packed no` only if a binary NAV file stores one 0/1 byte per bit.

## Doppler options

The default search is -10 kHz to +10 kHz in 250 Hz steps:

```bash
--doppler-min -10000 --doppler-max 10000 --doppler-step 250
```

The report contains the coarse acquired Doppler, residual estimate, and their
sum. If the signal is weak, first narrow the range if the approximate Doppler
is known, then reduce `--doppler-step` for a finer search.

## Outputs

For the default prefix, the program writes:

- `nav_recovery.txt`: recovered `0`/`1` bit string
- `nav_recovery_spaced.txt`: recovered bits as whitespace-separated `0` and `1`
  values, MSB first, with 50 bits per line
- `nav_recovery.bin`: recovered bits packed MSB-first
- `nav_recovery.json`: acquisition, tracking, confidence, and comparison results
- `nav_recovery_aligned.txt`: expected and recovered streams with `-` marking
  missing/extra bits (written when `--expected-nav` is used)
- `nav_recovery_expected_spaced.txt`: the expected NAV converted to the same
  whitespace-separated, MSB-first text format
- `nav_recovery_aligned_spaced.txt`: expected and recovered streams in labeled
  50-bit blocks, including `-` gap markers for ordinary text comparison
- `nav_recovery_subframes.txt`: decoded candidate subframes and word fields;
  received CRC bits are labeled `unchecked`

The JSON report includes PRN code delay modulo one code period in samples,
chips, microseconds, and range-equivalent meters.

The decoded BPSK polarity is ambiguous without a known framing pattern. When
`--expected-nav` is supplied, the program tests both polarities and applies the
one with the higher match rate.

## Useful options

Process only part of a long recording:

```bash
--skip-seconds 2 --duration 5
```

Override the PocketSDR defaults:

```bash
--fs 16368000 --if-hz 4013000 --chip-rate 1023000 \
--code-ms 1 --nav-rate 50
```

Code phase is rechecked every 100 ms. Increase `--track-search` if it can move
by more than eight samples in that interval, or reduce `--track-batch-ms`.

The full code phase is also re-estimated at each batch boundary, allowing the
tracker to relock after a sample discontinuity caused by an SDR underrun.

## Interpretation

- `acquisition_peak_ratio`: peak correlation power divided by the mean
  out-of-peak correlation power; larger is better.
- `bit_confidence`: mean fraction of each integrated NAV symbol lying on the
  recovered BPSK real axis; values near 1 are cleaner.
- `match_fraction`: recovered-versus-expected bit agreement after selecting the
  best cyclic expected-message offset and BPSK polarity.
- `expected_message_coverage`: fraction of the one-minute expected message
  covered by the recovered recording.
- `dropped_bits`, `extra_or_repeated_bits`, and `substituted_bits`: results of
  edit-distance alignment. A dropped bit does not shift all later comparisons.
- `aligned_match_fraction`: bit agreement after accounting for those gaps.

The default alignment permits cumulative slips of up to 200 bits. Increase
`--max-bit-slip` if the JSON report says that limit was exceeded.

The verifier recovers the transmitted bitstream. It does not yet parse GPS word
fields or validate GPS parity because the supplied NAV message is custom. Those
checks can be added once its precise word/parity construction is specified.
