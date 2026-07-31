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
auto-refresh settings) is shared with kramer_server.py. It lives next to the
program when a file is already there, and in the per-user directory otherwise;
--config overrides both. See kramer_paths.py.
"""

import argparse
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

try:
    import kramer_paths as kp
    import kramer_vs44 as kv
except ImportError as e:
    # getattr rather than an import, because the module that failed to load may
    # be the one that would have told us whether this is a frozen build.
    if getattr(sys, "frozen", False):
        sys.exit(f"ERROR: this build is incomplete ({e}). Please report it.")
    sys.exit("ERROR: kramer_paths.py and kramer_vs44.py must be in the same "
             f"directory as this file ({e}).")


# Reassigned in main() once --config has been parsed. It stays a module global
# because that is what the test suites substitute.
CONFIG_PATH = kp.config_path()

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
    cfg.update(kp.read_json(
        CONFIG_PATH,
        on_error=lambda e: print(f"Unreadable config, falling back to "
                                 f"defaults: {e}")))
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
    """Merge into what is already on disk instead of overwriting the file.

    kramer_server.py writes the labels to this same file, so a blind overwrite
    would silently discard keys this program does not know about. It does not
    make the two safe to run at once - for the keys they both own, the last
    writer still wins - which is why only one controller should be running.

    Returns None on success, or the error. The caller has to show it: in a
    windowed build there is no console for a printed message to land in, so
    reporting the failure by printing it would mean losing the settings in
    silence - which is the whole class of bug this is here to avoid."""
    try:
        kp.merge_json(CONFIG_PATH, cfg)
    except OSError as e:
        print(f"Could not save the config: {e}")
        return e
    return None


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
    """Owns the link to the matrix: opens it, runs jobs on it, listens on it,
    probes it when it goes quiet, and reopens it when it dies.

    The protocol's 200 ms rate limit is guaranteed by the Transport, which makes
    a single thread a requirement rather than a preference: two concurrent
    threads would violate the timing. Everything therefore happens in this one
    loop - the passive listener, the liveness probe and the reconnection - and
    the loop never blocks for longer than IDLE_POLL, so stopping stays prompt
    even in the middle of a retry.

    Results are posted as (tag, payload, error) for the Tk thread to drain. The
    link-related tags are link_up, link_down and link_retry; `connect` is kept
    for a first attempt that failed, because that is the one the user is waiting
    on and the only one that deserves a dialog."""

    IDLE_POLL = 0.2                     # seconds spent listening between jobs
    RECONNECT_DELAY = kv.RECONNECT_DELAY

    def __init__(self, results, heartbeat=None):
        super().__init__(daemon=True)
        self.jobs = queue.Queue()
        self.results = results
        self.transport = None
        self.proto = None
        self.monitor = kv.LinkMonitor(heartbeat)
        self.connector = None           # callable() -> (transport, proto, detail)
        self.on_notify = None
        self.want_link = False          # the user asked for a link and has not cancelled
        self._next_try = 0.0
        self._down_since = 0.0
        self._listen_bug = False
        self._stop = threading.Event()

    # ----- what the UI calls ---------------------------------------------- #

    def link(self, connector, on_notify):
        """Ask for a link, and keep asking until unlink() or a first failure.

        connector is called on this thread and must return
        (transport, proto, detail). It is a closure built when Connect was
        pressed, so a reconnection cannot silently retarget a different address
        because someone was editing the field in the meantime."""
        self.connector = connector
        self.on_notify = on_notify
        self.want_link = True
        self._down_since = 0.0
        self._next_try = 0.0

    def unlink(self):
        self.want_link = False
        self._close()
        self.results.put(("link_down", {"reason": "disconnected",
                                        "retrying": False}, None))

    def submit(self, tag, fn, needs_link=True):
        self.jobs.put((tag, fn, needs_link))

    def stop(self):
        self._stop.set()
        self.jobs.put(None)

    # ----- the loop -------------------------------------------------------- #

    def run(self):
        while not self._stop.is_set():
            if self.want_link and not self.proto \
                    and time.monotonic() >= self._next_try:
                self._connect()
            try:
                item = self.jobs.get(timeout=self.IDLE_POLL)
            except queue.Empty:
                if self.proto:
                    self._listen()
                    self._maybe_beat()
                continue
            if item is None:
                break
            tag, fn, needs_link = item
            if needs_link and not self.proto:
                # Fail fast rather than calling fn with no protocol object. The
                # UI already says the link is down; this keeps a queued click
                # from turning into an AttributeError.
                self.results.put((tag, None,
                                  ConnectionError("the link is down")))
                continue
            try:
                self.results.put((tag, fn(self), None))
            except OSError as e:
                # Socket-level: the link is gone, not the command wrong.
                self.results.put((tag, None, e))
                self._drop(e)
            except Exception as e:
                # A bad command must not take the link down with it.
                self.results.put((tag, None, e))

    def _connect(self):
        try:
            transport, proto, detail = self.connector()
            proto.on_notify = self.on_notify
            self.transport, self.proto = transport, proto
            self.monitor.mark_ok()
            self._listen_bug = False
            relink = bool(self._down_since)
            elapsed = int(time.monotonic() - self._down_since) if relink else 0
            self._down_since = 0.0
            self.results.put(("link_up", {"detail": detail, "relink": relink,
                                          "elapsed": elapsed}, None))
        except (OSError, ConnectionError) as e:
            self._close()
            if not self._down_since:
                # The very first attempt: the user is waiting on it, so report it
                # as the failure of what they asked for and stop. Retrying behind
                # a mistyped address would be a loop nobody asked for.
                self.want_link = False
                self.results.put(("connect", None, e))
                return
            self._next_try = time.monotonic() + self.RECONNECT_DELAY
            if self.monitor.first_time(str(e)):
                self.results.put(("link_retry", {"error": str(e)}, None))
        except Exception as e:
            # Anything that is not a connection problem is a bug, and retrying a
            # bug every few seconds helps nobody.
            self._close()
            self.want_link = False
            self.results.put(("connect", None, e))

    def _listen(self):
        """Read what the device transmits by itself. A VS-44HN sends a SWITCH
        VIDEO frame when a front-panel button is pressed, which is what keeps
        the grid in sync without polling."""
        try:
            self.proto.poll_notifications(self.IDLE_POLL)
        except OSError as e:
            self._drop(e)
        except Exception as e:
            # Not a link failure - a decoding bug, say. Reconnecting cannot fix
            # it, and letting it escape would kill this thread and leave the
            # window silently inert. Report it once and stop listening.
            if not self._listen_bug:
                self._listen_bug = True
                self.results.put(("listen", None, e))

    def _maybe_beat(self):
        """Probe a link that has gone quiet. The decision lives in
        kramer_vs44.LinkMonitor, shared with the service."""
        if not self.monitor.due(self.transport):
            return
        # Held in a local: _drop() clears self.transport, and the finally below
        # still has to unmute the object it muted.
        transport = self.transport
        transport.set_muted(True)           # a probe every 30 s is not worth logging
        try:
            reason = self.monitor.beat(self.proto)
        except OSError as e:
            self._drop(e)
            return
        finally:
            transport.set_muted(False)
        if reason:
            self._drop(reason)

    def _drop(self, error):
        self._close()
        self._down_since = time.monotonic()
        self._next_try = self._down_since + self.RECONNECT_DELAY
        self.monitor.first_time(str(error))     # so the first retry stays quiet
        self.results.put(("link_down", {"reason": str(error),
                                        "retrying": self.want_link}, None))

    def _close(self):
        if self.transport:
            try:
                self.transport.close()
            except Exception:
                pass
        self.transport = None
        self.proto = None


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
        self.worker = Worker(self.results, heartbeat=cli_args.heartbeat)
        self.worker.start()
        self.connected = False
        # "idle" | "connecting" | "up" | "reconnecting". connected stays as the
        # plain boolean meaning link_state == "up", because every action guards
        # on it and the tests assign it.
        self.link_state = "idle"
        self.link_detail = ""
        self._down_since = 0.0
        self._shown_down_secs = -1
        self._autorefresh_job = None
        self._had_focus = False
        self._last_auto = 0.0
        self._auto_pending = False

        root.title("Kramer VS-44HN — matrix control")
        root.minsize(720, 640)
        self._set_window_icon()

        self._build_connection()
        self._build_routing()
        self._build_presets()
        self._build_utility()
        self._build_log()

        self._set_enabled(False)
        # Say where the settings are, exactly as the service does. That single
        # line is what makes portable mode discoverable: seeing a path under the
        # user profile is what tells you that a settings file placed next to the
        # program would be used instead.
        self._write_log(f"settings file: {CONFIG_PATH}")
        root.after(80, self._drain)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_window_icon(self):
        """Give the window an identity in the taskbar and the alt-tab list.

        Also the one thing in this program that reads a bundled resource, so it
        exercises the frozen-build resource path for real rather than leaving it
        to be discovered later. Failure is not worth reporting: an icon is not
        why anyone opened this."""
        try:
            path = kp.resource_path("packaging", "kramer.png")
            if path.exists():
                self._icon = tk.PhotoImage(file=str(path))
                self.root.iconphoto(True, self._icon)
        except tk.TclError:
            pass

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
        # The reconnecting counter ticks from here rather than from a timer of
        # its own; redrawing only when the whole second changes keeps it cheap.
        if self.link_state == "reconnecting":
            secs = int(time.monotonic() - self._down_since)
            if secs != self._shown_down_secs:
                self._shown_down_secs = secs
                self._render_link()
        self.root.after(80, self._drain)

    def _handle(self, tag, res, err):
        if tag == "status_auto":
            self._auto_pending = False
        if err:
            if tag == "status_auto" and isinstance(err, ConnectionError):
                # The link is already known to be down and the indicator says so;
                # a log line per automatic read would just repeat it.
                return
            self._write_log(f"!! {tag}: {err}")
            if tag == "connect":
                # Only a first attempt reaches here, and the user is waiting on
                # it. Reconnections never raise a dialog: one every few seconds
                # for a minute and a half would be unusable.
                self._mark_disconnected()
                messagebox.showerror("Connection failed", str(err))
            return
        if tag == "link_up":
            self.connected = True
            self.link_state = "up"
            self.link_detail = res["detail"]
            self.connect_btn.configure(text="Disconnect")
            self._set_enabled(True)
            self._render_link()
            if res["relink"]:
                self._write_log(f"== reconnected after {res['elapsed']}s, "
                                f"{res['detail']}")
            else:
                self._write_log(f"== connected, {res['detail']}")
            self._refresh()
            self._schedule_autorefresh()
        elif tag == "link_down":
            if res["retrying"]:
                self._mark_link_lost(res["reason"])
            else:
                self._mark_disconnected()
        elif tag == "link_retry":
            self._write_log(f"   still trying: {res['error']}")
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
        self.link_state = "idle"
        self.link_detail = ""
        self._down_since = 0.0
        self.connect_btn.configure(text="Connect")
        self._set_enabled(False)
        self._auto_pending = False
        self._schedule_autorefresh()            # cancels the pending tick
        self._blank_grid()
        self._render_link()

    def _mark_link_lost(self, reason):
        """The link went away by itself and the worker is trying to get it back.

        The button stays on Disconnect, because stopping the retries has to
        remain possible, and no dialog appears: one every few seconds for the
        minute and a half this device can take to come back would be unusable."""
        self.connected = False
        self.link_state = "reconnecting"
        self._down_since = time.monotonic()
        self._auto_pending = False
        self._set_enabled(False)
        self._schedule_autorefresh()
        self._blank_grid()
        self._render_link()
        self._write_log(f"!! link lost: {reason} - reconnecting")

    def _blank_grid(self):
        """Stop showing routing that cannot be vouched for.

        A greyed-out radio button still reads as *set*, and the front panel can
        move things while the link is down. Silent staleness is the worst failure
        a control panel can have, so the marks go away entirely; reconnecting
        re-reads the state and fills them back in within about a second."""
        for v in self.route_vars.values():
            v.set(-1)

    def _render_link(self):
        """The connection indicator, redrawn from link_state.

        While reconnecting it counts the seconds, and that is not decoration:
        this matrix has been measured taking about 90 seconds to accept a new
        connection, and a static amber dot for a minute and a half is
        indistinguishable from a hung program."""
        if self.link_state == "up":
            self.status.configure(text=f"● {self.link_detail}", foreground="#2a7")
        elif self.link_state == "connecting":
            self.status.configure(text="● connecting…", foreground="#a70")
        elif self.link_state == "reconnecting":
            secs = int(time.monotonic() - self._down_since)
            self.status.configure(text=f"● link lost, reconnecting… ({secs}s)",
                                  foreground="#a70")
        else:
            self.status.configure(text="● not connected", foreground="#a33")

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
        if self.link_state != "idle":
            # Covers "up" and "reconnecting": the button reads Disconnect in both,
            # and stopping a retry loop has to be possible.
            self.worker.unlink()
            return

        mode = self.mode.get()
        host, proto_choice = self.host.get().strip(), self.proto_var.get()
        dev = self.serial_dev.get().strip()
        try:
            port = int(self.port.get())
        except ValueError:
            messagebox.showerror("Invalid port", "The port must be a number.")
            return

        def connector():
            """Opens the link. Runs on the worker thread, and is called again for
            every reconnection - which is why the settings are captured here,
            when Connect was pressed, rather than read from the widgets. A
            reconnection must not quietly retarget a different address because
            someone was mid-edit in the IP field."""
            if mode == "tcp":
                t = LoggingTcp(host, port)
            else:
                t = LoggingSerial(dev, kv.BAUD)
            t.attach_log(self._logline)
            if proto_choice == "p2000":
                proto = kv.Protocol2000(t)
                if not proto.ping():
                    t.close()
                    raise ConnectionError("connected, but the matrix did not "
                                          "answer Protocol 2000")
            elif proto_choice == "p3000":
                proto = kv.Protocol3000(t)
            else:
                # Detection already proves the link, so no second identify.
                proto, _ = kv.detect_protocol(t)
                if proto is None:
                    t.close()
                    raise ConnectionError(
                        "No valid reply. Check the IP/port (5000, 10001 or 50000) "
                        "and that the device is powered on.")
            return t, proto, f"{t} — {proto.name}"

        self.link_state = "connecting"
        self._render_link()
        # The notification callback runs on the worker thread, so it only queues
        # a result: touching Tk from there would be a crash waiting to happen.
        self.worker.link(connector,
                         lambda frames: self.results.put(("notify", frames, None)))

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
        failure = save_config(self.cfg)
        if failure:
            messagebox.showerror(
                "Settings not saved",
                f"Your labels and preset names could not be written to\n"
                f"{CONFIG_PATH}\n\n{failure}")
        # Unconditional: a link that is merely being retried still has to stop
        # being retried, and unlink() copes with there being no transport.
        self.worker.unlink()
        self.worker.stop()
        self.root.destroy()


def main():
    global CONFIG_PATH

    # A windowed build has no console, so CPython leaves sys.stdout and
    # sys.stderr as None. print() tolerates that, but argparse writes to the
    # stream object itself: --version, --help and every "invalid value" path
    # would die with an AttributeError and show nothing at all.
    if getattr(sys, "frozen", False) and sys.stdout is None:
        sys.stdout = sys.stderr = open(os.devnull, "w")

    ap = argparse.ArgumentParser(description="GUI for Kramer VS-44HN / VS-44H matrices.")
    ap.add_argument("--host", help="matrix IP address (default 192.168.1.39)")
    ap.add_argument("--port", type=int, help="TCP port (5000, 10001 or 50000)")
    ap.add_argument("--serial", help="use the serial port, e.g. COM3")
    ap.add_argument("--heartbeat", type=float, default=kv.HEARTBEAT,
                    metavar="SECONDS",
                    help=f"probe the matrix after this much silence (default "
                         f"{kv.HEARTBEAT:g}; 0 disables the check, and a matrix "
                         f"switched off silently will then keep being reported "
                         f"as connected)")
    kp.add_common_arguments(ap)
    args = ap.parse_args()
    CONFIG_PATH = kp.config_path(args.config)

    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.2)
    except tk.TclError:
        pass
    App(root, load_config(), args)
    root.mainloop()


if __name__ == "__main__":
    main()
