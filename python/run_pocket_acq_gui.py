#!/usr/bin/env python3
"""Tkinter front end for run_pocket_acq.sh."""

from __future__ import annotations

import os
import queue
import shlex
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText


BASE_DIR = Path(__file__).resolve().parent
WRAPPER = BASE_DIR / "run_pocket_acq.sh"

DEFAULTS = {
    "freq": "16.368",
    "if": "4.013",
    "iq": "2",
    "prn": "1",
    "toff": "100",
    "tint": "30",
    "sdr": "gnuradio",
    "script": "./pocket_acq_custKasami.py",
    "wm": str(
        Path.home()
        / "SDR_Recording/remote/"
        "regus_test_1500M_wm_P4_-44.50dBm_G30.0dB_"
        "20260730_163919_20260730-163921-838.c64"
    ),
}

FIELDS = (
    ("Sampling frequency (MHz)", "freq"),
    ("Intermediate frequency (MHz)", "if"),
    ("IQ mode", "iq"),
    ("PRN", "prn"),
    ("Time offset (ms)", "toff"),
    ("Integration time (ms)", "tint"),
    ("SDR type", "sdr"),
    ("Acquisition script", "script"),
    ("Watermark file", "wm"),
)


class AcquisitionGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Pocket Acquisition")
        self.minsize(820, 620)
        self.process: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[str | None] = queue.Queue()
        self.values = {key: tk.StringVar(value=value) for key, value in DEFAULTS.items()}
        self.log_enabled = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Ready")
        self._build()
        self._refresh_preview()
        self.after(100, self._drain_output)

    def _build(self) -> None:
        form = ttk.Frame(self, padding=12)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        for row, (label, key) in enumerate(FIELDS):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
            entry = ttk.Entry(form, textvariable=self.values[key])
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            entry.bind("<KeyRelease>", lambda _event: self._refresh_preview())
            if key in {"script", "wm"}:
                ttk.Button(
                    form,
                    text="Browse…",
                    command=lambda selected=key: self._browse(selected),
                ).grid(row=row, column=2, padx=(8, 0), pady=4)

        ttk.Checkbutton(
            form,
            text="Enable logging (-l)",
            variable=self.log_enabled,
            command=self._refresh_preview,
        ).grid(row=len(FIELDS), column=1, sticky="w", pady=(6, 4))

        preview_frame = ttk.LabelFrame(self, text="Command preview", padding=8)
        preview_frame.pack(fill="x", padx=12, pady=(0, 8))
        self.preview = tk.Text(preview_frame, height=4, wrap="word")
        self.preview.pack(fill="x")
        self.preview.configure(state="disabled")

        controls = ttk.Frame(self, padding=(12, 0))
        controls.pack(fill="x")
        self.run_button = ttk.Button(controls, text="Run acquisition", command=self._run)
        self.run_button.pack(side="left")
        self.stop_button = ttk.Button(
            controls, text="Stop", command=self._stop, state="disabled"
        )
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(controls, text="Reset defaults", command=self._reset).pack(side="left")
        ttk.Label(controls, textvariable=self.status).pack(side="right")

        output_frame = ttk.LabelFrame(self, text="Output", padding=8)
        output_frame.pack(fill="both", expand=True, padx=12, pady=12)
        self.output = ScrolledText(output_frame, height=12, wrap="word")
        self.output.pack(fill="both", expand=True)

    def _browse(self, key: str) -> None:
        initial = Path(os.path.expanduser(self.values[key].get()))
        selected = filedialog.askopenfilename(
            initialdir=initial.parent if initial.parent.exists() else Path.home(),
            initialfile=initial.name,
        )
        if selected:
            self.values[key].set(selected)
            self._refresh_preview()

    def _command(self) -> list[str]:
        command = [
            str(WRAPPER),
            "--freq", self.values["freq"].get(),
            "--if", self.values["if"].get(),
            "--iq", self.values["iq"].get(),
            "--prn", self.values["prn"].get(),
            "--toff", self.values["toff"].get(),
            "--tint", self.values["tint"].get(),
            "--sdr", self.values["sdr"].get(),
            "--script", os.path.expanduser(self.values["script"].get()),
            "--wm", os.path.expanduser(self.values["wm"].get()),
        ]
        command.append("--log" if self.log_enabled.get() else "--no-log")
        return command

    def _refresh_preview(self) -> None:
        preview = shlex.join(self._command())
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", preview)
        self.preview.configure(state="disabled")

    def _run(self) -> None:
        if self.process is not None:
            return
        if not WRAPPER.is_file():
            messagebox.showerror("Missing wrapper", f"Could not find:\n{WRAPPER}")
            return

        self.output.delete("1.0", "end")
        self.output.insert("end", f"$ {shlex.join(self._command())}\n\n")
        self.status.set("Running")
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        threading.Thread(target=self._worker, args=(self._command(),), daemon=True).start()

    def _worker(self, command: list[str]) -> None:
        try:
            self.process = subprocess.Popen(
                command,
                cwd=BASE_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.output_queue.put(line)
            return_code = self.process.wait()
            self.output_queue.put(f"\nProcess exited with status {return_code}.\n")
        except OSError as error:
            self.output_queue.put(f"\nCould not start acquisition: {error}\n")
        finally:
            self.process = None
            self.output_queue.put(None)

    def _drain_output(self) -> None:
        try:
            while True:
                item = self.output_queue.get_nowait()
                if item is None:
                    self.status.set("Finished")
                    self.run_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                else:
                    self.output.insert("end", item)
                    self.output.see("end")
        except queue.Empty:
            pass
        self.after(100, self._drain_output)

    def _stop(self) -> None:
        if self.process is not None:
            self.status.set("Stopping…")
            self.process.terminate()

    def _reset(self) -> None:
        for key, value in DEFAULTS.items():
            self.values[key].set(value)
        self.log_enabled.set(True)
        self.status.set("Ready")
        self._refresh_preview()


if __name__ == "__main__":
    AcquisitionGUI().mainloop()
