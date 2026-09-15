#!/usr/bin/env python3
"""Recover a 50 bit/s BPSK NAV stream from a PRN-spread complex64 recording."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np


def _text_bit_rows(path: Path) -> list[np.ndarray]:
    rows = []
    for line in path.read_text(errors="ignore").splitlines():
        s = line.strip()
        if not s:
            continue
        if re.fullmatch(r"[01]+", s):
            rows.append(np.fromiter((int(x) for x in s), dtype=np.uint8))
            continue
        tokens = re.findall(r"(?<![\w.])[01](?![\w.])", s)
        if len(tokens) > 1:  # ignores metadata such as "L1CA" and "Bits:4"
            rows.append(np.asarray(tokens, dtype=np.uint8))
    return rows


def load_bits(path: str, row: int = 1, packed: str = "auto") -> np.ndarray:
    """Load a selected 1-based row from text, or bits from a binary file."""
    p = Path(path)
    if p.suffix.lower() in {".txt", ".csv"}:
        rows = _text_bit_rows(p)
        if not rows:
            raise ValueError(f"no bit rows found in {p}")
        if not 1 <= row <= len(rows):
            raise ValueError(f"row must be 1..{len(rows)} for {p}")
        return rows[row - 1]

    raw = np.fromfile(p, dtype=np.uint8)
    if not raw.size:
        raise ValueError(f"empty file: {p}")
    use_packed = packed == "yes" or (packed == "auto" and not np.all(raw <= 1))
    return np.unpackbits(raw, bitorder="big") if use_packed else raw


def write_spaced_bits(path: Path, bits, bits_per_line: int = 50) -> None:
    """Write MSB-first 0/1 values separated by spaces, wrapping by bit count."""
    values = [str(x) for x in bits]
    lines = [" ".join(values[i:i + bits_per_line])
             for i in range(0, len(values), bits_per_line)]
    path.write_text("\n".join(lines) + "\n")


def parse_bit_string(value: str) -> np.ndarray:
    """Parse compact or whitespace/comma-separated 0/1 input."""
    cleaned = re.sub(r"[\s,_]", "", value.strip())
    if cleaned.lower().startswith("0b"):
        cleaned = cleaned[2:]
    if not cleaned or re.fullmatch(r"[01]+", cleaned) is None:
        raise ValueError("preamble must contain only 0 and 1 (spaces are allowed)")
    return np.fromiter((int(x) for x in cleaned), dtype=np.uint8)


def find_preamble(bits: np.ndarray, preamble: np.ndarray) -> list[int]:
    """Return zero-based exact-match locations in the recovered stream."""
    if len(preamble) > len(bits):
        return []
    return [i for i in range(len(bits) - len(preamble) + 1)
            if np.array_equal(bits[i:i + len(preamble)], preamble)]


def bits_to_int(bits) -> int:
    return int("".join(map(str, np.asarray(bits, dtype=np.uint8).tolist())), 2)


def parse_subframes(bits: np.ndarray, preamble: np.ndarray) -> list[dict]:
    """Parse complete 300-bit candidates beginning with normal/inverse preambles."""
    inverse = 1 - preamble
    candidates = [(p, False) for p in find_preamble(bits, preamble)]
    candidates += [(p, True) for p in find_preamble(bits, inverse)]
    subframes = []
    for position, inverted in sorted(candidates):
        if position + 300 > len(bits):
            continue
        sf = bits[position:position + 300].copy()
        if inverted:
            sf = 1 - sf
        words = []
        for word_index in range(10):
            word = sf[word_index * 30:(word_index + 1) * 30]
            if word_index == 0:
                item = {
                    "word": 1,
                    "preamble": "".join(map(str, word[0:8].tolist())),
                    "sf_id": bits_to_int(word[8:13]),
                    "word_id": bits_to_int(word[13:17]),
                    "data": "".join(map(str, word[17:24].tolist())),
                    "crc_received": "".join(map(str, word[24:30].tolist())),
                    "crc_status": "unchecked",
                }
            else:
                item = {
                    "word": word_index + 1,
                    "data_1": "".join(map(str, word[0:13].tolist())),
                    "word_id": bits_to_int(word[13:17]),
                    "data_2": "".join(map(str, word[17:24].tolist())),
                    "crc_received": "".join(map(str, word[24:30].tolist())),
                    "crc_status": "unchecked",
                }
            words.append(item)
        word_ids = [w["word_id"] for w in words]
        sequential = all(((word_ids[i + 1] - word_ids[i]) & 0xF) == 1
                         for i in range(len(word_ids) - 1))
        subframes.append({"start_bit_zero_based": position,
                          "received_preamble_polarity": "inverted" if inverted else "normal",
                          "sf_id": words[0]["sf_id"], "word_ids": word_ids,
                          "word_ids_sequential_mod16": sequential, "words": words})
    return subframes


def write_subframe_report(path: Path, subframes: list[dict]) -> None:
    lines = ["CRC values are displayed but not validated.", ""]
    for index, sf in enumerate(subframes, 1):
        lines.append(f"Subframe candidate {index}: start_bit={sf['start_bit_zero_based']} "
                     f"polarity={sf['received_preamble_polarity']} SF_ID={sf['sf_id']} "
                     f"word_IDs={sf['word_ids']} sequential={sf['word_ids_sequential_mod16']}")
        for word in sf["words"]:
            if word["word"] == 1:
                lines.append(f"  W1 preamble={word['preamble']} SF_ID={word['sf_id']} "
                             f"word_ID={word['word_id']} data={word['data']} "
                             f"CRC={word['crc_received']} (unchecked)")
            else:
                lines.append(f"  W{word['word']} data_1={word['data_1']} "
                             f"word_ID={word['word_id']} data_2={word['data_2']} "
                             f"CRC={word['crc_received']} (unchecked)")
        lines.append("")
    path.write_text("\n".join(lines) + "\n")


def possible_flip_ranges(reference: str, recovered: str, window: int) -> list[tuple[int, int]]:
    """Find merged windows having at least 60% opposite, non-gap bits."""
    if window <= 0 or len(reference) < window:
        return []
    wrong = np.fromiter((a != "-" and b != "-" and a != b
                         for a, b in zip(reference, recovered)), dtype=bool)
    threshold = math.ceil(0.60 * window)
    hits = []
    for start in range(len(wrong) - window + 1):
        if int(np.count_nonzero(wrong[start:start + window])) >= threshold:
            hits.append((start, start + window))
    merged = []
    for start, end in hits:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def sampled_code(prn_bits: np.ndarray, fs: float, chip_rate: float, count: int,
                 start_sample: float = 0.0) -> np.ndarray:
    chips = 1.0 - 2.0 * prn_bits.astype(np.float32)
    index = np.floor((start_sample + np.arange(count)) * chip_rate / fs).astype(np.int64)
    return chips[index % len(chips)]


def acquire(x: np.ndarray, prn: np.ndarray, fs: float, chip_rate: float,
            code_ms: float, if_hz: float, doppler_min: float,
            doppler_max: float, doppler_step: float) -> dict:
    ncode = int(round(fs * code_ms * 1e-3))
    if len(x) < ncode:
        raise ValueError("recording is shorter than one code period")
    block = x[:ncode].astype(np.complex128, copy=False)
    local = sampled_code(prn, fs, chip_rate, ncode)
    local_fft = np.fft.fft(local)
    t = np.arange(ncode) / fs
    dopplers = np.arange(doppler_min, doppler_max + 0.5 * doppler_step, doppler_step)
    best = (-1.0, 0.0, 0, None)
    for doppler in dopplers:
        wiped = block * np.exp(-2j * np.pi * (if_hz + doppler) * t)
        corr = np.fft.ifft(np.fft.fft(wiped) * np.conj(local_fft))
        power = np.abs(corr) ** 2
        phase = int(np.argmax(power))
        metric = float(power[phase])
        if metric > best[0]:
            best = (metric, float(doppler), phase, power)
    power = best[3]
    guard = max(1, int(round(fs / chip_rate)))
    mask = np.ones(ncode, dtype=bool)
    for k in range(-guard, guard + 1):
        mask[(best[2] + k) % ncode] = False
    peak_ratio = best[0] / max(float(np.mean(power[mask])), 1e-30)
    return {"doppler_hz": best[1], "code_phase_samples": best[2],
            "peak_ratio": peak_ratio}


def track_ms(x: np.ndarray, prn: np.ndarray, fs: float, chip_rate: float,
             code_ms: float, if_hz: float, doppler: float, phase: int,
             search_samples: int, batch_periods: int) -> tuple[np.ndarray, np.ndarray]:
    ncode = int(round(fs * code_ms * 1e-3))
    local = sampled_code(prn, fs, chip_rate, ncode)
    prompts, offsets = [], []
    predicted = int(phase)
    while predicted + ncode <= len(x):
        # A full circular code-phase check at every batch boundary allows the
        # tracker to relock after an SDR sample discontinuity/underrun.
        idx0 = predicted + np.arange(ncode)
        carrier0 = np.exp(-2j * np.pi * (if_hz + doppler) * idx0 / fs)
        probe = x[predicted:predicted + ncode] * carrier0
        corr = np.fft.ifft(np.fft.fft(probe) * np.conj(np.fft.fft(local)))
        lag = int(np.argmax(np.abs(corr) ** 2))
        if lag > ncode // 2:
            lag -= ncode
        predicted = max(0, predicted + lag)
        candidates = range(max(0, predicted - search_samples),
                           min(len(x) - ncode, predicted + search_samples) + 1)
        best = None
        for start in candidates:
            idx = start + np.arange(ncode)
            carrier = np.exp(-2j * np.pi * (if_hz + doppler) * idx / fs)
            value = np.vdot(local, x[start:start + ncode] * carrier)
            score = abs(value)
            if best is None or score > best[0]:
                best = (score, start, value)
        assert best is not None
        start = best[1]
        count = min(batch_periods, (len(x) - start) // ncode)
        idx = start + np.arange(count * ncode)
        carrier = np.exp(-2j * np.pi * (if_hz + doppler) * idx / fs)
        wiped = (x[start:start + count * ncode] * carrier).reshape(count, ncode)
        prompts.extend(wiped @ local)
        offsets.extend(start + np.arange(count) * ncode)
        predicted = start + count * ncode
    if not prompts:
        raise ValueError("no complete code periods available after acquisition phase")
    return np.asarray(prompts), np.asarray(offsets)


def costas_correct(prompt: np.ndarray) -> tuple[np.ndarray, float]:
    """Remove residual phase/frequency; squaring makes this insensitive to BPSK data."""
    if len(prompt) < 2:
        return prompt, 0.0
    z = prompt / np.maximum(np.abs(prompt), 1e-30)
    dphi = 0.5 * np.angle(np.sum((z[1:] * np.conj(z[:-1])) ** 2))
    n = np.arange(len(prompt))
    wiped = prompt * np.exp(-1j * dphi * n)
    phase = 0.5 * np.angle(np.sum(wiped ** 2))
    wiped *= np.exp(-1j * phase)
    return wiped, dphi / (2 * np.pi * 0.001)


def recover_bits(prompt: np.ndarray, periods_per_bit: int) -> dict:
    best = None
    for offset in range(periods_per_bit):
        usable = (len(prompt) - offset) // periods_per_bit
        if usable <= 0:
            continue
        symbols = prompt[offset:offset + usable * periods_per_bit].reshape(usable, periods_per_bit).sum(1)
        confidence = float(np.mean(np.abs(symbols.real) / np.maximum(np.abs(symbols), 1e-30)))
        if best is None or confidence > best[0]:
            best = (confidence, offset, symbols)
    if best is None:
        raise ValueError("recording is too short to recover one NAV bit")
    bits = (best[2].real < 0).astype(np.uint8)
    return {"bits": bits, "bit_offset_ms": best[1], "confidence": best[0],
            "symbols": best[2]}


def compare_expected(rx: np.ndarray, expected: np.ndarray) -> dict:
    """Find cyclic expected-message offset and either BPSK polarity."""
    if not len(rx) or not len(expected):
        return {}
    best = None
    # Use a short prefix to seed the offset. Comparing the entire unaligned
    # stream would choose the wrong offset after even one missing bit.
    n = min(len(rx), len(expected), 200)
    for shift in range(len(expected)):
        reference = np.resize(np.roll(expected, -shift), n)
        direct = float(np.mean(rx[:n] == reference))
        inverted = float(np.mean((1 - rx[:n]) == reference))
        candidate = (max(direct, inverted), shift, inverted > direct, n)
        if best is None or candidate[0] > best[0]:
            best = candidate
    return {"match_fraction": best[0], "expected_bit_offset": best[1],
            "polarity_inverted": best[2], "compared_bits": best[3]}


def align_bits(rx: np.ndarray, expected: np.ndarray, expected_offset: int,
               inverted: bool, max_slip: int, flip_run: int = 8) -> tuple[dict, str, str]:
    """Banded edit alignment; '-' in RX means a dropped transmitted bit."""
    received = (1 - rx if inverted else rx).astype(np.uint8)
    # For an approximately full reference interval, retain the reference's
    # length so missing/extra recovered bits remain visible. For a deliberately
    # short recording, compare only the available interval.
    reference_len = len(expected) if abs(len(expected) - len(received)) <= max_slip else len(received)
    reference = np.resize(np.roll(expected, -expected_offset), reference_len)
    n, m = len(reference), len(received)
    inf = n + m + 1
    prev = np.full(m + 1, inf, dtype=np.int32)
    prev[:min(m, max_slip) + 1] = 2 * np.arange(min(m, max_slip) + 1)
    direction = np.full((n + 1, 2 * max_slip + 1), 3, dtype=np.uint8)
    for i in range(1, n + 1):
        cur = np.full(m + 1, inf, dtype=np.int32)
        lo, hi = max(0, i - max_slip), min(m, i + max_slip)
        if lo == 0:
            cur[0] = 2 * i
        for j in range(max(1, lo), hi + 1):
            diag = prev[j - 1] + int(reference[i - 1] != received[j - 1])
            delete = prev[j] + 2       # reference bit absent from recording
            insert = cur[j - 1] + 2    # extra/repeated recorded bit
            choice = int(np.argmin((diag, delete, insert)))
            cur[j] = (diag, delete, insert)[choice]
            direction[i, j - i + max_slip] = choice
        prev = cur
    if prev[m] >= inf:
        raise ValueError("bit slip exceeds --max-bit-slip")
    a_ref, a_rx = [], []
    i, j = n, m
    substitutions = dropped = extra = 0
    while i or j:
        if i == 0:
            choice = 2
        elif j == 0:
            choice = 1
        else:
            k = j - i + max_slip
            choice = int(direction[i, k]) if 0 <= k < direction.shape[1] else 3
        if choice == 0:
            a_ref.append(str(int(reference[i - 1])))
            a_rx.append(str(int(received[j - 1])))
            substitutions += int(reference[i - 1] != received[j - 1])
            i -= 1; j -= 1
        elif choice == 1:
            a_ref.append(str(int(reference[i - 1]))); a_rx.append("-")
            dropped += 1; i -= 1
        elif choice == 2:
            a_ref.append("-"); a_rx.append(str(int(received[j - 1])))
            extra += 1; j -= 1
        else:
            raise ValueError("could not backtrack bit alignment; increase --max-bit-slip")
    a_ref = "".join(reversed(a_ref)); a_rx = "".join(reversed(a_rx))
    matches = sum(a == b for a, b in zip(a_ref, a_rx) if a != "-" and b != "-")
    flip_intervals = possible_flip_ranges(a_ref, a_rx, flip_run)
    flip_mask = np.zeros(len(a_ref), dtype=bool)
    for start, end in flip_intervals:
        for pos in range(start, end):
            flip_mask[pos] = (a_ref[pos] != "-" and a_rx[pos] != "-" and
                              a_ref[pos] != a_rx[pos])
    flip_ranges = [{"aligned_start": start, "aligned_end": end,
                    "opposite_bits": int(np.count_nonzero(flip_mask[start:end]))}
                   for start, end in flip_intervals]
    stats = {"aligned_matches": matches, "substituted_bits": substitutions,
             "dropped_bits": dropped, "extra_or_repeated_bits": extra,
             "edit_distance": substitutions + dropped + extra,
             "aligned_match_fraction": float(matches / max(matches + substitutions, 1)),
             "possible_flip_bits": int(np.count_nonzero(flip_mask)),
             "possible_flip_runs": flip_ranges}
    return stats, a_ref, a_rx


def launch_gui() -> None:
    """Launch a Tkinter front end; processing runs in a child process."""
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        from tkinter.scrolledtext import ScrolledText
    except ImportError as exc:
        raise SystemExit("Tkinter is required for the GUI (Ubuntu: sudo apt install python3-tk)") from exc

    root = tk.Tk()
    root.title("C64 NAV Verifier")
    root.minsize(820, 690)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(2, weight=1)

    files = ttk.LabelFrame(root, text="Files", padding=10)
    files.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
    files.columnconfigure(1, weight=1)
    variables: dict[str, tk.StringVar] = {}

    def file_row(row: int, key: str, label: str, pattern, save: bool = False) -> None:
        var = variables[key] = tk.StringVar()
        ttk.Label(files, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(files, textvariable=var).grid(row=row, column=1, sticky="ew", pady=3)
        def browse() -> None:
            if save:
                chosen = filedialog.asksaveasfilename(title=label, filetypes=pattern)
                if chosen:
                    for suffix in (".json", ".txt", ".bin"):
                        if chosen.endswith(suffix):
                            chosen = chosen[:-len(suffix)]
                            break
            else:
                chosen = filedialog.askopenfilename(title=label, filetypes=pattern)
            if chosen:
                var.set(chosen)
        ttk.Button(files, text="Browse…", command=browse).grid(row=row, column=2, padx=(8, 0), pady=3)

    file_row(0, "iq", "C64 IQ recording", [("Complex64 IQ", "*.c64 *.binC64"), ("All files", "*")])
    file_row(1, "prn", "PRN code file", [("PRN files", "*.txt *.bin"), ("All files", "*")])
    file_row(2, "nav", "Expected NAV (optional)", [("NAV files", "*.bin *.txt"), ("All files", "*")])
    file_row(3, "output", "Output prefix", [("Output prefix", "*"), ("All files", "*")], save=True)
    variables["output"].set("nav_recovery")

    params = ttk.LabelFrame(root, text="Parameters", padding=10)
    params.grid(row=1, column=0, sticky="ew", padx=10, pady=5)
    for col in (1, 3):
        params.columnconfigure(col, weight=1)
    defaults = [
        ("prn_row", "PRN row", "1"), ("nav_packed", "NAV binary packing", "auto"),
        ("fs", "Sample rate (Hz)", "16368000"), ("if_hz", "IF frequency (Hz)", "4013000"),
        ("chip_rate", "Chip rate (chip/s)", "1023000"), ("code_ms", "Code period (ms)", "1"),
        ("nav_rate", "NAV rate (bit/s)", "50"), ("doppler_min", "Doppler minimum (Hz)", "-10000"),
        ("doppler_max", "Doppler maximum (Hz)", "10000"), ("doppler_step", "Doppler step (Hz)", "250"),
        ("track_search", "Local search (samples)", "8"), ("track_batch_ms", "Full relock interval (ms)", "100"),
        ("max_bit_slip", "Maximum bit slip", "200"), ("skip_seconds", "Start offset (s)", "0"),
        ("flip_run", "Possible flip run (bits)", "8"),
        ("duration", "Duration (s, blank = all)", ""),
        ("preamble", "Custom preamble", "10100111"),
        ("display_bits_per_row", "Bits per visual row", "30"),
    ]
    for i, (key, label, value) in enumerate(defaults):
        row, side = divmod(i, 2)
        base = side * 2
        ttk.Label(params, text=label).grid(row=row, column=base, sticky="w", padx=(0, 6), pady=3)
        var = variables[key] = tk.StringVar(value=value)
        if key == "nav_packed":
            ttk.Combobox(params, textvariable=var, values=("auto", "yes", "no"),
                         state="readonly", width=14).grid(row=row, column=base + 1, sticky="ew", padx=(0, 14), pady=3)
        else:
            ttk.Entry(params, textvariable=var, width=18).grid(row=row, column=base + 1,
                                                               sticky="ew", padx=(0, 14), pady=3)
    align_columns = tk.BooleanVar(value=False)
    ttk.Checkbutton(params, text="Align columns to detected subframe/word boundary",
                    variable=align_columns).grid(row=(len(defaults) + 1) // 2,
                                                  column=0, columnspan=4,
                                                  sticky="w", pady=(6, 0))

    bottom = ttk.Frame(root, padding=(10, 5, 10, 10))
    bottom.grid(row=2, column=0, sticky="nsew")
    bottom.columnconfigure(0, weight=1)
    bottom.rowconfigure(1, weight=1)
    status = tk.StringVar(value="Ready")
    toolbar = ttk.Frame(bottom)
    toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 5))
    ttk.Label(toolbar, textvariable=status).pack(side="left")
    panes = ttk.Panedwindow(bottom, orient="vertical")
    panes.grid(row=1, column=0, sticky="nsew")
    log_frame = ttk.LabelFrame(panes, text="Processing log", padding=5)
    log = ScrolledText(log_frame, height=9, wrap="word", state="disabled")
    log.pack(fill="both", expand=True)
    panes.add(log_frame, weight=1)

    visual_frame = ttk.LabelFrame(panes, text="Visual bit checker", padding=5)
    legend = ttk.Frame(visual_frame)
    legend.pack(fill="x", pady=(0, 4))
    for label, bg, fg in (("Matched", "#d9f2d0", "#176b0b"),
                          ("Missing / extra", "#111111", "white"),
                          ("Wrong", "#d32f2f", "white"),
                          ("Possible polarity flip", "#ffd54f", "black"),
                          ("Preamble", "#80d8ff", "black"),
                          ("Inverted preamble", "#ce93d8", "black"),
                          ("SF ID", "#42a5f5", "white"),
                          ("Inverted SF ID", "#8e24aa", "white")):
        tk.Label(legend, text=f" {label} ", background=bg, foreground=fg).pack(side="left", padx=(0, 8))
    visual_summary = tk.StringVar(value="Run with an expected NAV file to display bit comparison.")
    ttk.Label(visual_frame, textvariable=visual_summary).pack(anchor="w", pady=(0, 3))
    bit_view = ScrolledText(visual_frame, height=12, wrap="none", font=("TkFixedFont", 11), state="disabled")
    bit_view.pack(fill="both", expand=True)
    bit_view.tag_configure("match", background="#d9f2d0", foreground="#176b0b")
    bit_view.tag_configure("missing", background="#111111", foreground="white")
    bit_view.tag_configure("wrong", background="#d32f2f", foreground="white")
    bit_view.tag_configure("flip", background="#ffd54f", foreground="black")
    bit_view.tag_configure("preamble", background="#80d8ff", foreground="black")
    bit_view.tag_configure("preamble_inverse", background="#ce93d8", foreground="black")
    bit_view.tag_configure("sfid", background="#42a5f5", foreground="white")
    bit_view.tag_configure("sfid_inverse", background="#8e24aa", foreground="white")
    panes.add(visual_frame, weight=2)

    def append(text: str) -> None:
        log.configure(state="normal")
        log.insert("end", text)
        log.see("end")
        log.configure(state="disabled")

    numeric_options = {
        "prn_row": "--prn-row", "fs": "--fs", "if_hz": "--if-hz",
        "chip_rate": "--chip-rate", "code_ms": "--code-ms", "nav_rate": "--nav-rate",
        "doppler_min": "--doppler-min", "doppler_max": "--doppler-max",
        "doppler_step": "--doppler-step", "track_search": "--track-search",
        "track_batch_ms": "--track-batch-ms", "max_bit_slip": "--max-bit-slip",
        "skip_seconds": "--skip-seconds", "flip_run": "--flip-run", "duration": "--duration",
    }

    def render_alignment(path: Path, flip_run: int, preamble_text: str,
                         global_inverted: bool, bits_per_row: int,
                         align_to_subframe: bool) -> None:
        bit_view.configure(state="normal")
        bit_view.delete("1.0", "end")
        if not path.exists():
            visual_summary.set("No aligned comparison was produced. Select an expected NAV file.")
            bit_view.configure(state="disabled")
            return
        lines = path.read_text().splitlines()
        if len(lines) < 2:
            visual_summary.set("Aligned comparison file is incomplete.")
            bit_view.configure(state="disabled")
            return
        reference = lines[0].removeprefix("EXPECTED  ")
        recovered = lines[1].removeprefix("RECOVERED ")
        status_tags = []
        for a, b in zip(reference, recovered):
            status_tags.append("missing" if "-" in (a, b) else "match" if a == b else "wrong")
        intervals = possible_flip_ranges(reference, recovered, flip_run)
        for start, end in intervals:
            for pos in range(start, end):
                if status_tags[pos] == "wrong":
                    status_tags[pos] = "flip"
        flip_runs = len(intervals)
        counts = {tag: status_tags.count(tag) for tag in ("match", "missing", "wrong", "flip")}
        structural_marks = {}
        subframe_starts = []
        if preamble_text.strip():
            preamble_bits = "".join(map(str, parse_bit_string(preamble_text).tolist()))
            inverse_bits = "".join("1" if bit == "0" else "0" for bit in preamble_bits)
            ref_plain_chars, plain_to_aligned = [], []
            for aligned_pos, bit in enumerate(reference):
                if bit != "-":
                    ref_plain_chars.append(bit)
                    plain_to_aligned.append(aligned_pos)
            ref_plain = "".join(ref_plain_chars)
            occurrences = []
            start = ref_plain.find(preamble_bits)
            while start >= 0:
                occurrences.append(start)
                start = ref_plain.find(preamble_bits, start + 1)
            if occurrences:
                phase_counts = {}
                for start in occurrences:
                    phase_counts[start % 300] = phase_counts.get(start % 300, 0) + 1
                selected_phase = max(phase_counts, key=phase_counts.get)
                occurrences = [start for start in occurrences if start % 300 == selected_phase]
            for plain_start in occurrences:
                if plain_start + len(preamble_bits) + 5 > len(plain_to_aligned):
                    continue
                positions = plain_to_aligned[plain_start:plain_start + len(preamble_bits)]
                sfid_positions = plain_to_aligned[plain_start + len(preamble_bits):
                                                  plain_start + len(preamble_bits) + 5]
                rx_preamble = "".join(recovered[pos] for pos in positions)
                corrected_inverse = (rx_preamble == inverse_bits)
                physical_inverse = corrected_inverse ^ global_inverted
                preamble_tag = "preamble_inverse" if physical_inverse else "preamble"
                sfid_tag = "sfid_inverse" if physical_inverse else "sfid"
                for pos in positions:
                    structural_marks[pos] = preamble_tag
                for pos in sfid_positions:
                    structural_marks[pos] = sfid_tag
                subframe_starts.append(positions[0])
        visual_summary.set(
            f"Aligned bits: {len(status_tags)}   Matched: {counts['match']}   "
            f"Missing/extra: {counts['missing']}   Wrong: {counts['wrong']}   "
            f"Possible flip: {counts['flip']} bits in {flip_runs} run(s)")
        source_indices = list(range(1, len(recovered) + 1))
        if align_to_subframe and subframe_starts:
            leading_pad = (-subframe_starts[0]) % bits_per_row
            if leading_pad:
                recovered = "-" * leading_pad + recovered
                status_tags = ["missing"] * leading_pad + status_tags
                source_indices = [None] * leading_pad + source_indices
                structural_marks = {pos + leading_pad: tag for pos, tag in structural_marks.items()}
                subframe_starts = [pos + leading_pad for pos in subframe_starts]
            trailing_pad = (-len(recovered)) % bits_per_row
            if trailing_pad:
                recovered += "-" * trailing_pad
                status_tags += ["missing"] * trailing_pad
                source_indices += [None] * trailing_pad
        for row_start in range(0, len(status_tags), bits_per_row):
            row_end = min(row_start + bits_per_row, len(status_tags))
            for pos in range(row_start, row_end):
                tags = ((status_tags[pos], structural_marks[pos])
                        if pos in structural_marks else (status_tags[pos],))
                bit_view.insert("end", recovered[pos], tags)
                column = pos - row_start + 1
                if column % 10 == 0 and pos + 1 < row_end:
                    bit_view.insert("end", " ")
            valid = [x for x in source_indices[row_start:row_end] if x is not None]
            label = f"{valid[0]:04d}-{valid[-1]:04d}" if valid else "padding"
            bit_view.insert("end", f"  {label}\n")
        bit_view.configure(state="disabled")

    def run_clicked() -> None:
        if not variables["iq"].get() or not variables["prn"].get():
            messagebox.showerror("Missing input", "Select both the C64 recording and PRN file.")
            return
        output_prefix = variables["output"].get()
        flip_run_value = int(variables["flip_run"].get())
        preamble_value = variables["preamble"].get()
        align_columns_value = bool(align_columns.get())
        try:
            bits_per_row_value = int(variables["display_bits_per_row"].get())
            if bits_per_row_value <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid parameter", "Bits per visual row must be a positive integer.")
            return
        cmd = [sys.executable, str(Path(__file__).resolve()), variables["iq"].get(),
               "--prn", variables["prn"].get(), "--output-prefix", output_prefix,
               "--nav-packed", variables["nav_packed"].get()]
        if variables["nav"].get():
            cmd += ["--expected-nav", variables["nav"].get()]
        if variables["preamble"].get().strip():
            cmd += ["--preamble", variables["preamble"].get().strip()]
        for key, option in numeric_options.items():
            if variables[key].get().strip():
                cmd += [option, variables[key].get().strip()]
        run_button.configure(state="disabled")
        status.set("Processing…")
        append("Starting verification…\n")

        def worker() -> None:
            try:
                proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, check=False)
                root.after(0, append, proc.stdout)
                root.after(0, status.set, "Finished" if proc.returncode == 0 else "Failed")
                if proc.returncode == 0:
                    aligned_path = Path(output_prefix).with_name(Path(output_prefix).name + "_aligned.txt")
                    report_path = Path(output_prefix).with_suffix(".json")
                    report_data = json.loads(report_path.read_text()) if report_path.exists() else {}
                    root.after(0, render_alignment, aligned_path, flip_run_value,
                               preamble_value,
                               bool(report_data.get("polarity_inverted", False)),
                               bits_per_row_value, align_columns_value)
                    root.after(0, messagebox.showinfo, "C64 NAV Verifier", "Verification finished successfully.")
                else:
                    root.after(0, messagebox.showerror, "C64 NAV Verifier",
                               "Verification failed. See the log for details.")
            except Exception as exc:
                root.after(0, append, f"ERROR: {exc}\n")
                root.after(0, status.set, "Failed")
            finally:
                root.after(0, run_button.configure, {"state": "normal"})
        threading.Thread(target=worker, daemon=True).start()

    run_button = ttk.Button(toolbar, text="Run verification", command=run_clicked)
    run_button.pack(side="right")
    root.mainloop()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("iq", help="GNU Radio complex64 recording")
    ap.add_argument("--prn", required=True, help="PRN .txt or .bin")
    ap.add_argument("--prn-row", type=int, default=1, help="1-based PRN row (default: 1)")
    ap.add_argument("--expected-nav", help="optional expected NAV .txt or .bin")
    ap.add_argument("--nav-packed", choices=["auto", "yes", "no"], default="auto")
    ap.add_argument("--fs", type=float, default=16.368e6)
    ap.add_argument("--if-hz", type=float, default=4.013e6)
    ap.add_argument("--chip-rate", type=float, default=1.023e6)
    ap.add_argument("--code-ms", type=float, default=1.0)
    ap.add_argument("--nav-rate", type=float, default=50.0)
    ap.add_argument("--doppler-min", type=float, default=-10000.0)
    ap.add_argument("--doppler-max", type=float, default=10000.0)
    ap.add_argument("--doppler-step", type=float, default=250.0)
    ap.add_argument("--track-search", type=int, default=8,
                    help="code tracking radius in samples (default: 8)")
    ap.add_argument("--track-batch-ms", type=float, default=100.0,
                    help="recheck code phase every N ms (default: 100)")
    ap.add_argument("--max-bit-slip", type=int, default=200,
                    help="maximum cumulative missing/extra bits in alignment")
    ap.add_argument("--flip-run", type=int, default=8,
                    help="consecutive wrong bits classified as a possible polarity flip")
    ap.add_argument("--preamble",
                    help="custom bit preamble, compact or whitespace-separated")
    ap.add_argument("--skip-seconds", type=float, default=0.0)
    ap.add_argument("--duration", type=float, help="seconds to process")
    ap.add_argument("--output-prefix", default="nav_recovery")
    args = ap.parse_args()

    prn = load_bits(args.prn, args.prn_row)
    expected = load_bits(args.expected_nav, packed=args.nav_packed) if args.expected_nav else None
    periods_per_bit = int(round(1000.0 / (args.nav_rate * args.code_ms)))
    if not np.isclose(periods_per_bit * args.code_ms * args.nav_rate, 1000.0):
        raise ValueError("NAV bit duration must contain an integer number of code periods")

    start = int(round(args.skip_seconds * args.fs))
    count = -1 if args.duration is None else int(round(args.duration * args.fs))
    x = np.memmap(args.iq, dtype=np.complex64, mode="r", offset=start * 8,
                  shape=None if count < 0 else (count,))
    acq = acquire(x, prn, args.fs, args.chip_rate, args.code_ms, args.if_hz,
                  args.doppler_min, args.doppler_max, args.doppler_step)
    prompt, code_offsets = track_ms(x, prn, args.fs, args.chip_rate, args.code_ms,
                                    args.if_hz, acq["doppler_hz"],
                                    acq["code_phase_samples"], args.track_search,
                                    max(1, int(round(args.track_batch_ms / args.code_ms))))
    prompt, residual = costas_correct(prompt)
    rec = recover_bits(prompt, periods_per_bit)
    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    comparison = compare_expected(rec["bits"], expected) if expected is not None else {}
    alignment = {}
    if expected is not None:
        alignment, aligned_ref, aligned_rx = align_bits(
            rec["bits"], expected, comparison["expected_bit_offset"],
            comparison["polarity_inverted"], args.max_bit_slip, args.flip_run)
        prefix.with_name(prefix.name + "_aligned.txt").write_text(
            "EXPECTED  " + aligned_ref + "\nRECOVERED " + aligned_rx + "\n")
        spaced_aligned = prefix.with_name(prefix.name + "_aligned_spaced.txt")
        with spaced_aligned.open("w") as stream:
            for start_bit in range(0, len(aligned_ref), 50):
                end_bit = min(start_bit + 50, len(aligned_ref))
                stream.write(f"BITS {start_bit + 1:04d}-{end_bit:04d}\n")
                stream.write("EXPECTED  " + " ".join(aligned_ref[start_bit:end_bit]) + "\n")
                stream.write("RECOVERED " + " ".join(aligned_rx[start_bit:end_bit]) + "\n\n")
    if comparison.get("polarity_inverted"):
        rec["bits"] = 1 - rec["bits"]
    preamble = parse_bit_string(args.preamble) if args.preamble else None
    preamble_positions = find_preamble(rec["bits"], preamble) if preamble is not None else []
    inverse_preamble_positions = (find_preamble(rec["bits"], 1 - preamble)
                                  if preamble is not None else [])
    subframes = parse_subframes(rec["bits"], preamble) if preamble is not None else []
    if preamble is not None:
        write_subframe_report(prefix.with_name(prefix.name + "_subframes.txt"), subframes)

    (prefix.with_suffix(".txt")).write_text("".join(map(str, rec["bits"].tolist())) + "\n")
    write_spaced_bits(prefix.with_name(prefix.name + "_spaced.txt"), rec["bits"].tolist())
    np.packbits(rec["bits"], bitorder="big").tofile(prefix.with_suffix(".bin"))
    if expected is not None:
        write_spaced_bits(prefix.with_name(prefix.name + "_expected_spaced.txt"),
                          expected.tolist())
    report = {
        "input": str(args.iq), "prn": str(args.prn), "prn_row": args.prn_row,
        "prn_length_chips": int(len(prn)), "sample_rate_hz": args.fs,
        "if_hz": args.if_hz, "acquired_doppler_hz": acq["doppler_hz"],
        "residual_doppler_hz": residual,
        "estimated_total_doppler_hz": acq["doppler_hz"] + residual,
        "initial_code_phase_samples": acq["code_phase_samples"],
        "prn_code_delay_samples": acq["code_phase_samples"],
        "prn_code_delay_chips": acq["code_phase_samples"] * args.chip_rate / args.fs,
        "prn_code_delay_microseconds": acq["code_phase_samples"] / args.fs * 1e6,
        "prn_code_delay_meters_equivalent": acq["code_phase_samples"] / args.fs * 299792458.0,
        "acquisition_peak_ratio": acq["peak_ratio"],
        "tracked_code_periods": int(len(prompt)), "recovered_bits": int(len(rec["bits"])),
        "bit_boundary_offset_code_periods": rec["bit_offset_ms"],
        "bit_confidence": rec["confidence"], **comparison, **alignment,
    }
    if preamble is not None:
        report["preamble"] = "".join(map(str, preamble.tolist()))
        report["preamble_length_bits"] = int(len(preamble))
        report["preamble_match_count"] = len(preamble_positions)
        report["preamble_positions_zero_based"] = preamble_positions
        report["inverted_preamble_match_count"] = len(inverse_preamble_positions)
        report["inverted_preamble_positions_zero_based"] = inverse_preamble_positions
        report["subframe_candidate_count"] = len(subframes)
        report["subframes"] = subframes
    if expected is not None:
        report["expected_nav_bits"] = int(len(expected))
        report["expected_nav_duration_seconds"] = float(len(expected) / args.nav_rate)
        report["expected_message_coverage"] = float(min(len(rec["bits"]) / len(expected), 1.0))
    prefix.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print(f"Recovered bits: {prefix.with_suffix('.txt')}")
    print(f"Packed bits:    {prefix.with_suffix('.bin')}")


if __name__ == "__main__":
    if len(sys.argv) == 1 or sys.argv[1:] == ["--gui"]:
        launch_gui()
    else:
        main()
