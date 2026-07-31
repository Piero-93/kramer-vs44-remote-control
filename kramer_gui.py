#!/usr/bin/env python3
#
# kramer-vs44-remote-control - control a Kramer VS-44HN HDMI matrix switcher.
# Copyright (C) 2026 Piero Biagini
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program. If not, see <https://www.gnu.org/licenses/>.
"""
kramer_gui.py - Graphical interface for Kramer VS-44HN / VS-44H HDMI matrices.

Requires kramer_vs44.py in the SAME directory: the protocol logic lives there,
this file is only the UI. No external dependency (Tkinter is in the stdlib).

Usage:
    python kramer_gui.py
    python kramer_gui.py --host 192.168.1.50 --port 10001
    python kramer_gui.py --serial COM3

The configuration (IP, port, input/output labels, preset names and the
auto-refresh settings) is saved to kramer_gui_config.json next to this script.
"""

import argparse
import json
import queue
import re
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

try:
    import kramer_vs44 as kv
except ImportError:
    sys.exit("ERROR: kramer_vs44.py must be in the same directory as this file.")


CONFIG_PATH = Path(__file__).with_name("kramer_gui_config.json")

DEFAULT_CONFIG = {
    "host": "192.168.1.39",
    "port": 5000,
    "proto": "auto",
    "inputs": ["IN 1", "IN 2", "IN 3", "IN 4"],
    "outputs": ["OUT 1", "OUT 2", "OUT 3", "OUT 4"],
    "presets": [f"Preset {i}" for i in range(1, 9)],
    "autorefresh": True,
    "autorefresh_interval": 30,
}

# Selectable periodic-refresh intervals, in seconds. The passive listener is what
# keeps the grid current; this is only the reconciliation net for a frame that
# never arrived, so the default is deliberately slow. A full Protocol 2000 state
# read costs about 0.9 s of bus time.
AUTOREFRESH_INTERVALS = (5, 10, 30, 60)

# Minimum gap between two automatic reads, in seconds. Regaining focus triggers
# one, and alt-tabbing back and forth would otherwise keep the bus busy.
FOCUS_REFRESH_MIN_GAP = 1.5

# Protocol 2000 instruction 1, SWITCH VIDEO. The only unsolicited frame observed
# from a VS-44HN, sent when a front-panel button is pressed.
P2000_SWITCH_VIDEO = 1


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"Unreadable config, falling back to defaults: {e}")
    # normalise the lengths: a hand-edited file must not break the UI
    for key, n in (("inputs", 4), ("outputs", 4), ("presets", 8)):
        vals = list(cfg.get(key) or [])[:n]
        vals += DEFAULT_CONFIG[key][len(vals):n]
        cfg[key] = vals
    cfg["autorefresh"] = bool(cfg.get("autorefresh"))
    if cfg.get("autorefresh_interval") not in AUTOREFRESH_INTERVALS:
        cfg["autorefresh_interval"] = DEFAULT_CONFIG["autorefresh_interval"]
    return cfg


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                               encoding="utf-8")
    except Exception as e:
        print(f"Could not save the config: {e}")


# --------------------------------------------------------------------------- #
# Transports that log to the UI
# --------------------------------------------------------------------------- #

class _LogMixin:
    """Intercepts send/recv to show the bytes in the log panel."""

    _muted = False

    def attach_log(self, log_fn):
        self._log_fn = log_fn

    def set_muted(self, muted):
        """Silence the byte log. Used around automatic refreshes, whose traffic
        would otherwise bury everything the user actually asked for. Safe
        because all I/O runs on the single worker thread."""
        self._muted = muted

    def _emit(self, text):
        if self._muted:
            return
        fn = getattr(self, "_log_fn", None)
        if fn:
            fn(text)

    def send(self, data):
        self._emit(f"TX -> {kv.hexdump(data)}   {kv.printable(data)}")
        super().send(data)

    def recv(self, timeout=1.0, maxlen=512, expect=None):
        data = super().recv(timeout, maxlen, expect)
        if data:
            self._emit(f"RX <- {kv.hexdump(data)}   {kv.printable(data)}")
        return data


class LoggingTcp(_LogMixin, kv.TcpTransport):
    pass


class LoggingSerial(_LogMixin, kv.SerialTransport):
    pass


# --------------------------------------------------------------------------- #
# Worker: all I/O stays out of the UI thread
# --------------------------------------------------------------------------- #

class Worker(threading.Thread):
    """Runs the jobs in sequence, and listens passively while idle.

    The protocol's 200 ms rate limit is guaranteed by the Transport, which makes
    a single thread a requirement rather than a preference: two concurrent
    threads would violate the timing. The passive listener therefore lives in
    this same loop instead of in a thread of its own."""

    IDLE_POLL = 0.2                     # seconds spent listening between jobs

    def __init__(self, results):
        super().__init__(daemon=True)
        self.jobs = queue.Queue()
        self.results = results
        self.transport = None
        self.proto = None
        self._listen_broken = False

    def attach(self, transport, proto, on_notify):
        """Called by the connect job once the link is up."""
        self.transport = transport
        self.proto = proto
        proto.on_notify = on_notify
        self._listen_broken = False

    def detach(self):
        self.transport = None
        self.proto = None
        self._listen_broken = False

    def submit(self, tag, fn):
        self.jobs.put((tag, fn))

    def stop(self):
        self.jobs.put(None)

    def run(self):
        while True:
            try:
                item = self.jobs.get(timeout=self.IDLE_POLL)
            except queue.Empty:
                self._listen()
                continue
            if item is None:
                break
            tag, fn = item
            try:
                self.results.put((tag, fn(self), None))
            except Exception as e:
                self.results.put((tag, None, e))

    def _listen(self):
        """Read what the device transmits by itself. A VS-44HN sends a SWITCH
        VIDEO frame when a front-panel button is pressed, which is what keeps
        the grid in sync without polling."""
        if self._listen_broken or not (self.transport and self.proto):
            return
        try:
            self.proto.poll_notifications(self.IDLE_POLL)
        except Exception as e:
            # Reported once: repeating it every 200 ms would bury the log.
            self._listen_broken = True
            self.results.put(("listen", None, e))


# --------------------------------------------------------------------------- #
# Reply parsing
# --------------------------------------------------------------------------- #

# The VS-44HN manual documents the COMMAND as #VID<in>><out>, but it does NOT
# document the reply format of #VID?. The same direction, in>out, is assumed
# here. If the grid comes out transposed, just set this to False.
#
# How to check it in 10 seconds: route input 1 to output 4 ONLY, then press
# "Refresh state". If the mark shows up on (in 1, out 4) the assumption holds;
# if it shows up on (in 4, out 1) it is inverted.
VID_REPLY_IS_IN_TO_OUT = True


def parse_vid_reply(text):
    """Extracts the pairs from a Protocol 3000 reply to #VID?.

    Best-effort parsing: looks for every N>M pair. If nothing is found the UI
    leaves the grid untouched and shows the raw reply in the log.
    Returns {output: input}."""
    pairs = re.findall(r"(\d+)\s*>\s*(\d+)", text or "")
    if VID_REPLY_IS_IN_TO_OUT:
        return {int(o): int(i) for i, o in pairs}
    return {int(o): int(i) for o, i in pairs}


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #

class App:
    N_IO = 4
    N_PRESETS = 8

    def __init__(self, root, cfg, cli_args):
        self.root = root
        self.cfg = cfg
        self.cli = cli_args
        self.results = queue.Queue()
        self.log_queue = queue.Queue()
        self.worker = Worker(self.results)
        self.worker.start()
        self.connected = False
        self._autorefresh_job = None
        self._had_focus = False
        self._last_auto = 0.0
        self._auto_pending = False

        root.title("Kramer VS-44HN — matrix control")
        root.minsize(720, 640)

        self._build_connection()
        self._build_routing()
        self._build_presets()
        self._build_utility()
        self._build_log()

        self._set_enabled(False)
        root.after(80, self._drain)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----- widget construction -------------------------------------------- #

    def _build_connection(self):
        f = ttk.LabelFrame(self.root, text="Connection", padding=8)
        f.pack(fill="x", padx=10, pady=(10, 4))

        self.mode = tk.StringVar(value="serial" if self.cli.serial else "tcp")
        ttk.Radiobutton(f, text="Network", variable=self.mode, value="tcp",
                        command=self._sync_mode).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(f, text="Serial", variable=self.mode, value="serial",
                        command=self._sync_mode).grid(row=1, column=0, sticky="w")

        ttk.Label(f, text="IP:").grid(row=0, column=1, padx=(12, 2))
        self.host = tk.StringVar(value=self.cli.host or self.cfg["host"])
        self.host_entry = ttk.Entry(f, textvariable=self.host, width=16)
        self.host_entry.grid(row=0, column=2)

        ttk.Label(f, text="Port:").grid(row=0, column=3, padx=(12, 2))
        self.port = tk.StringVar(value=str(self.cli.port or self.cfg["port"]))
        self.port_box = ttk.Combobox(f, textvariable=self.port, width=8,
                                     values=("5000", "10001", "50000"))
        self.port_box.grid(row=0, column=4)

        ttk.Label(f, text="Device:").grid(row=1, column=1, padx=(12, 2))
        self.serial_dev = tk.StringVar(value=self.cli.serial or "COM3")
        self.serial_entry = ttk.Entry(f, textvariable=self.serial_dev, width=16)
        self.serial_entry.grid(row=1, column=2)

        ttk.Label(f, text="Protocol:").grid(row=1, column=3, padx=(12, 2))
        self.proto_var = tk.StringVar(value=self.cfg["proto"])
        ttk.Combobox(f, textvariable=self.proto_var, width=8, state="readonly",
                     values=("auto", "p2000", "p3000")).grid(row=1, column=4)

        self.connect_btn = ttk.Button(f, text="Connect", command=self._toggle_connect)
        self.connect_btn.grid(row=0, column=5, rowspan=2, padx=(16, 6), sticky="ns")

        self.status = ttk.Label(f, text="● not connected", foreground="#a33")
        self.status.grid(row=0, column=6, rowspan=2, sticky="w")

        self._sync_mode()

    def _build_routing(self):
        f = ttk.LabelFrame(self.root, text="Routing", padding=8)
        f.pack(fill="x", padx=10, pady=4)

        self.out_labels = []
        self.in_labels = []
        self.route_vars = {}

        # column headers: the inputs, plus the "disconnect" column
        ttk.Label(f, text="output ↓ / input →").grid(row=0, column=0, padx=4,
                                                    pady=(0, 4))
        for i in range(1, self.N_IO + 1):
            v = tk.StringVar(value=self.cfg["inputs"][i - 1])
            ttk.Entry(f, textvariable=v, width=13, justify="center").grid(
                row=0, column=i, padx=3, pady=(0, 4))
            self.in_labels.append(v)
        ttk.Label(f, text="disconnect", foreground="#666").grid(
            row=0, column=self.N_IO + 1, padx=(12, 2), pady=(0, 4))

        # one row per output: a single radio group, read horizontally
        for o in range(1, self.N_IO + 1):
            v = tk.StringVar(value=self.cfg["outputs"][o - 1])
            ttk.Entry(f, textvariable=v, width=17).grid(row=o, column=0,
                                                        padx=(0, 6), pady=2)
            self.out_labels.append(v)
            self.route_vars[o] = tk.IntVar(value=-1)
            for i in range(1, self.N_IO + 1):
                ttk.Radiobutton(f, variable=self.route_vars[o], value=i,
                                command=lambda i=i, o=o: self._switch(i, o)
                                ).grid(row=o, column=i)
            ttk.Radiobutton(f, variable=self.route_vars[o], value=0,
                            command=lambda o=o: self._switch(0, o)
                            ).grid(row=o, column=self.N_IO + 1, padx=(12, 2))

        row = self.N_IO + 1
        bar = ttk.Frame(f)
        bar.grid(row=row, column=0, columnspan=self.N_IO + 2, pady=(10, 0), sticky="w")
        ttk.Label(bar, text="One input to all outputs:").pack(side="left",
                                                              padx=(0, 6))
        self.all_btns = []
        for i in range(1, self.N_IO + 1):
            b = ttk.Button(bar, text=str(i), width=3,
                           command=lambda i=i: self._switch(i, 0))
            b.pack(side="left", padx=2)
            self.all_btns.append(b)
        self.refresh_btn = ttk.Button(bar, text="Refresh state", command=self._refresh)
        self.refresh_btn.pack(side="left", padx=(16, 0))

        self.autorefresh = tk.BooleanVar(value=self.cfg["autorefresh"])
        ttk.Checkbutton(bar, text="Auto", variable=self.autorefresh,
                        command=self._schedule_autorefresh).pack(side="left", padx=(12, 2))
        self.autorefresh_secs = tk.StringVar(value=str(self.cfg["autorefresh_interval"]))
        secs_box = ttk.Combobox(bar, textvariable=self.autorefresh_secs, width=3,
                                state="readonly",
                                values=[str(s) for s in AUTOREFRESH_INTERVALS])
        secs_box.pack(side="left")
        secs_box.bind("<<ComboboxSelected>>", lambda _e: self._schedule_autorefresh())
        ttk.Label(bar, text="s").pack(side="left", padx=(2, 0))

        ttk.Label(f, text="The labels are editable and are saved when the window closes. "
                          "Front-panel switches show up on their own; Auto adds a periodic "
                          "re-read as a safety net while the window is visible.",
                  foreground="#666").grid(row=row + 1, column=0,
                                          columnspan=self.N_IO + 2,
                                          sticky="w", pady=(6, 0))

    def _build_presets(self):
        f = ttk.LabelFrame(self.root, text="Presets", padding=8)
        f.pack(fill="x", padx=10, pady=4)
        self.preset_labels = []
        for n in range(1, self.N_PRESETS + 1):
            r, c = divmod(n - 1, 4)
            box = ttk.Frame(f)
            box.grid(row=r, column=c, padx=4, pady=3, sticky="w")
            v = tk.StringVar(value=self.cfg["presets"][n - 1])
            ttk.Entry(box, textvariable=v, width=14).pack(side="left")
            self.preset_labels.append(v)
            ttk.Button(box, text="▶", width=3,
                       command=lambda n=n: self._preset_recall(n)).pack(side="left", padx=2)
            ttk.Button(box, text="store", width=6,
                       command=lambda n=n: self._preset_store(n)).pack(side="left")

    def _build_utility(self):
        f = ttk.LabelFrame(self.root, text="Utility", padding=8)
        f.pack(fill="x", padx=10, pady=4)
        self.util_btns = []
        for text, cmd in (("Device info", self._device_info),
                          ("Input signal", self._signal),
                          ("Lock panel", lambda: self._lock(True)),
                          ("Unlock panel", lambda: self._lock(False))):
            b = ttk.Button(f, text=text, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self.util_btns.append(b)

        ttk.Label(f, text="Raw command:").pack(side="left", padx=(16, 4))
        self.raw_var = tk.StringVar()
        e = ttk.Entry(f, textvariable=self.raw_var, width=20)
        e.pack(side="left")
        e.bind("<Return>", lambda _e: self._raw())
        b = ttk.Button(f, text="Send", command=self._raw)
        b.pack(side="left", padx=4)
        self.util_btns.append(b)

    def _build_log(self):
        f = ttk.LabelFrame(self.root, text="Log", padding=6)
        f.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.log = scrolledtext.ScrolledText(f, height=12, wrap="none",
                                             font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)
        ttk.Button(f, text="Clear", command=lambda: self.log.delete("1.0", "end")
                   ).pack(anchor="e", pady=(4, 0))

    # ----- UI helpers ----------------------------------------------------- #

    def _sync_mode(self):
        tcp = self.mode.get() == "tcp"
        for w in (self.host_entry, self.port_box):
            w.configure(state="normal" if tcp else "disabled")
        self.serial_entry.configure(state="disabled" if tcp else "normal")

    def _set_enabled(self, on):
        state = "normal" if on else "disabled"
        for child in self.root.winfo_children():
            self._walk_state(child, state)
        # the connection bar always stays enabled
        for w in (self.connect_btn,):
            w.configure(state="normal")
        self._sync_mode()

    def _walk_state(self, widget, state):
        cls = widget.winfo_class()
        if cls in ("TRadiobutton", "TButton") and widget not in (self.connect_btn,):
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        for child in widget.winfo_children():
            self._walk_state(child, state)

    def _logline(self, text):
        self.log_queue.put(text)

    def _write_log(self, text):
        self.log.insert("end", text + "\n")
        self.log.see("end")

    def _drain(self):
        while True:
            try:
                self._write_log(self.log_queue.get_nowait())
            except queue.Empty:
                break
        while True:
            try:
                tag, res, err = self.results.get_nowait()
            except queue.Empty:
                break
            self._handle(tag, res, err)
        # Rising edge of the window focus. Deliberately polled here instead of
        # bound to <FocusIn>: that event bubbles up from the child widgets, so
        # moving between the entry fields would fire it over and over.
        focus = self._has_focus()
        if focus and not self._had_focus:
            self._on_focus_gained()
        self._had_focus = focus
        self.root.after(80, self._drain)

    def _handle(self, tag, res, err):
        if tag == "status_auto":
            self._auto_pending = False
        if err:
            self._write_log(f"!! {tag}: {err}")
            if tag == "connect":
                self._mark_disconnected()
                messagebox.showerror("Connection failed", str(err))
            elif tag == "status_auto":
                # One failure is enough: the link is broken, and retrying every
                # few seconds would only fill the log with the same error.
                self.autorefresh.set(False)
                self._schedule_autorefresh()
                self._write_log("   auto-refresh turned off after the error above")
            return
        if tag == "connect":
            self.connected = True
            self.status.configure(text=f"● {res}", foreground="#2a7")
            self.connect_btn.configure(text="Disconnect")
            self._set_enabled(True)
            self._write_log(f"== connected, {res}")
            self._refresh()
            self._schedule_autorefresh()
        elif tag == "disconnect":
            self._mark_disconnected()
        elif tag == "status":
            self._apply_status(res)
        elif tag == "status_auto":
            self._apply_status(res, quiet=True)
        elif tag == "notify":
            self._apply_notifications(res)
        elif tag == "info":
            for k, v in (res or {}).items():
                self._write_log(f"   {k}: {v}")
        else:
            if res not in (None, "", []):
                self._write_log(f"   {tag}: {res}")

    def _mark_disconnected(self):
        self.connected = False
        self.status.configure(text="● not connected", foreground="#a33")
        self.connect_btn.configure(text="Connect")
        self._set_enabled(False)
        self._auto_pending = False
        self._schedule_autorefresh()            # cancels the pending tick
        for v in self.route_vars.values():
            v.set(-1)

    def _apply_status(self, mapping, quiet=False):
        """quiet=True is the automatic refresh: only differences are logged, so
        the panel shows front-panel presses instead of a wall of identical
        state dumps."""
        if not mapping:
            if not quiet:
                self._write_log("   state not interpretable, grid left unchanged")
            return
        changed = {o: i for o, i in mapping.items()
                   if o in self.route_vars and i is not None
                   and self.route_vars[o].get() != i}
        for out, inp in mapping.items():
            if out in self.route_vars and inp is not None:
                self.route_vars[out].set(inp)
        if not quiet:
            self._write_log(f"   state: {mapping}")
        elif changed:
            self._write_log(f"   changed on the device: {changed}")

    def _apply_notifications(self, frames):
        """Frames the device transmitted on its own, typically a front-panel
        press. Only SWITCH VIDEO carries routing information; anything else is
        logged and ignored rather than guessed at."""
        routed = {}
        for f in frames or ():
            if f["instr"] == P2000_SWITCH_VIDEO and f["from_device"]:
                routed[f["output"]] = f["input"]
            else:
                self._write_log(f"   unsolicited frame ignored: {f['raw']}")
        if routed:
            self._apply_status(routed, quiet=True)

    # ----- automatic refresh ---------------------------------------------- #

    def _is_visible(self):
        """True unless the window is minimised or withdrawn. This gates the
        periodic refresh: a control panel parked on a side monitor is visible
        without holding the focus, and it still has to be in sync. On Windows a
        maximised window reports "zoomed", not "normal"."""
        try:
            return self.root.state() in ("normal", "zoomed")
        except tk.TclError:
            return False

    def _has_focus(self):
        """True when this application owns the keyboard focus. Only used to
        detect the moment the window comes back to the foreground."""
        try:
            return self.root.focus_displayof() is not None
        except (tk.TclError, KeyError):
            return False

    def _schedule_autorefresh(self):
        """Cancels the pending tick and re-arms it. Called on connect, on
        disconnect, when the checkbox or the interval change, and after every
        tick, so there is never more than one timer alive."""
        if self._autorefresh_job is not None:
            self.root.after_cancel(self._autorefresh_job)
            self._autorefresh_job = None
        if not (self.autorefresh.get() and self.connected):
            return
        try:
            secs = int(self.autorefresh_secs.get())
        except ValueError:
            secs = DEFAULT_CONFIG["autorefresh_interval"]
        self._autorefresh_job = self.root.after(secs * 1000, self._autorefresh_tick)

    def _autorefresh_tick(self):
        self._autorefresh_job = None
        if self.autorefresh.get() and self.connected and self._is_visible():
            self._refresh(quiet=True)
        self._schedule_autorefresh()

    def _on_focus_gained(self):
        """Read the state once when the window returns to the foreground, so it
        is already correct the moment you look at it instead of within the
        polling interval."""
        if not (self.autorefresh.get() and self.connected):
            return
        if time.monotonic() - self._last_auto < FOCUS_REFRESH_MIN_GAP:
            return
        self._refresh(quiet=True)
        self._schedule_autorefresh()        # push the next tick a full interval away

    # ----- actions -------------------------------------------------------- #

    def _toggle_connect(self):
        if self.connected:
            def job(w):
                if w.transport:
                    w.transport.close()
                w.detach()
                return "closed"
            self.worker.submit("disconnect", job)
            return

        mode = self.mode.get()
        host, proto_choice = self.host.get().strip(), self.proto_var.get()
        dev = self.serial_dev.get().strip()
        try:
            port = int(self.port.get())
        except ValueError:
            messagebox.showerror("Invalid port", "The port must be a number.")
            return

        def job(w):
            if mode == "tcp":
                t = LoggingTcp(host, port)
            else:
                t = LoggingSerial(dev, kv.BAUD)
            t.attach_log(self._logline)
            if proto_choice == "p2000":
                proto = kv.Protocol2000(t)
            elif proto_choice == "p3000":
                proto = kv.Protocol3000(t)
            else:
                proto, _ = kv.detect_protocol(t)
                if proto is None:
                    t.close()
                    raise RuntimeError(
                        "No valid reply. Check the IP/port (5000, 10001 or 50000) "
                        "and that the device is powered on.")
            # The callback runs on the worker thread, so it only queues a result:
            # touching Tk from here would be a crash waiting to happen.
            w.attach(t, proto,
                     lambda frames: self.results.put(("notify", frames, None)))
            return f"{t} — {proto.name}"

        self.status.configure(text="● connecting…", foreground="#a70")
        self.worker.submit("connect", job)

    def _switch(self, inp, out):
        if not self.connected:
            return
        label = "all outputs" if out == 0 else self.out_labels[out - 1].get()
        src = "disconnected" if inp == 0 else self.in_labels[inp - 1].get()
        self._write_log(f"-> {src} to {label}")
        self.worker.submit("switch", lambda w: w.proto.switch(inp, out))
        if out == 0:
            for v in self.route_vars.values():
                v.set(inp)

    def _refresh(self, quiet=False):
        if not self.connected:
            return
        # A full Protocol 2000 read is one command per output and takes seconds.
        # Without this guard a short interval would queue a new read before the
        # previous one returned, and the job queue would grow without bound.
        if quiet and self._auto_pending:
            return

        def job(w):
            if quiet:
                w.transport.set_muted(True)
            try:
                res = w.proto.status()
            finally:
                if quiet:
                    w.transport.set_muted(False)
            if isinstance(res, dict):
                return res                      # Protocol 2000
            return parse_vid_reply(res)         # Protocol 3000, best-effort

        if quiet:
            self._last_auto = time.monotonic()
            self._auto_pending = True
        else:
            self._write_log("-> refreshing state")
        self.worker.submit("status_auto" if quiet else "status", job)

    def _preset_recall(self, n):
        if not self.connected:
            return
        self._write_log(f"-> recalling preset {n} ({self.preset_labels[n-1].get()})")
        self.worker.submit("preset", lambda w: w.proto.preset_recall(n))
        self.root.after(900, self._refresh)

    def _preset_store(self, n):
        if not self.connected:
            return
        name = self.preset_labels[n - 1].get()
        if not messagebox.askyesno(
                "Overwrite the preset?",
                f"Store the current routing into preset {n} ({name})?\n"
                "The previous content will be lost."):
            return
        self._write_log(f"-> storing preset {n}")
        self.worker.submit("preset", lambda w: w.proto.preset_store(n))

    def _device_info(self):
        if not self.connected:
            return
        self._write_log("-> device info")

        def job(w):
            if isinstance(w.proto, kv.Protocol3000):
                return w.proto.device_info()
            return {"identify": w.proto.ping(), "version": w.proto.version(),
                    "counts": w.proto.io_count()}

        self.worker.submit("info", job)

    def _signal(self):
        if not self.connected:
            return
        self._write_log("-> signal presence on the inputs")

        def job(w):
            if isinstance(w.proto, kv.Protocol3000):
                return {"SIGNAL?": w.proto.signal()}
            return {self.in_labels[i - 1].get(): w.proto.signal(i)
                    for i in range(1, self.N_IO + 1)}

        self.worker.submit("info", job)

    def _lock(self, locked):
        if not self.connected:
            return
        self._write_log(f"-> front panel {'locked' if locked else 'unlocked'}")
        self.worker.submit("lock", lambda w: w.proto.lock_front_panel(locked))

    def _raw(self):
        if not self.connected:
            return
        text = self.raw_var.get().strip()
        if not text:
            return

        def job(w):
            w.transport.send(kv.parse_raw(text))
            data = w.transport.recv(1.5)
            return f"{kv.hexdump(data)} | {kv.printable(data)}"

        self.worker.submit("raw", job)

    # ----- shutdown ------------------------------------------------------- #

    def _on_close(self):
        self.cfg.update({
            "host": self.host.get().strip(),
            "port": int(self.port.get()) if self.port.get().isdigit() else 5000,
            "proto": self.proto_var.get(),
            "inputs": [v.get() for v in self.in_labels],
            "outputs": [v.get() for v in self.out_labels],
            "presets": [v.get() for v in self.preset_labels],
            "autorefresh": self.autorefresh.get(),
            "autorefresh_interval": (int(self.autorefresh_secs.get())
                                     if self.autorefresh_secs.get().isdigit()
                                     else DEFAULT_CONFIG["autorefresh_interval"]),
        })
        save_config(self.cfg)
        if self.connected:
            self.worker.submit("disconnect", lambda w: (w.transport.close(), "closed")[1])
        self.worker.stop()
        self.root.destroy()


def main():
    ap = argparse.ArgumentParser(description="GUI for Kramer VS-44HN / VS-44H matrices.")
    ap.add_argument("--host", help="matrix IP address (default 192.168.1.39)")
    ap.add_argument("--port", type=int, help="TCP port (5000, 10001 or 50000)")
    ap.add_argument("--serial", help="use the serial port, e.g. COM3")
    args = ap.parse_args()

    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.2)
    except tk.TclError:
        pass
    App(root, load_config(), args)
    root.mainloop()


if __name__ == "__main__":
    main()
