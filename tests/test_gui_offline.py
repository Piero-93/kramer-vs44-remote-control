#!/usr/bin/env python3
#
# Part of kramer-vs44-remote-control. Copyright (C) 2026 Piero Biagini.
# Licensed under the GNU GPL v3 or later. See LICENSE for details.
"""Offline checks for the GUI: no hardware, no connection, no network.

Builds the real window and exercises the state-tracking logic - the passive
listener wiring, the periodic refresh scheduling and its guards, the config
round-trip. Tkinter runs natively on Windows and macOS; on a headless Linux box
use a virtual display:

    xvfb-run -a python3 tests/test_gui_offline.py

Exits non-zero if any check fails, so it can gate a pull request.
"""

import argparse
import json
import sys
import time
import tkinter as tk
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kramer_gui as g

# Never touch the real configuration file.
g.CONFIG_PATH = Path(mkdtemp()) / "kramer_gui_config.json"

results = []


def check(name, got, want):
    results.append((got == want, f"{name:44s} got={got!r} want={want!r}"))


root = tk.Tk()
app = g.App(root, g.load_config(),
            argparse.Namespace(host=None, port=None, serial=None))

# The worker thread is already running and drains the queue immediately, so
# inspecting jobs.qsize() would be a race. Record the submissions instead.
submitted = []
app.worker.submit = lambda tag, fn: submitted.append(tag)


# --- widgets built and labelled -------------------------------------------- #
check("title", root.title(), "Kramer VS-44HN — matrix control")
check("refresh button", app.refresh_btn.cget("text"), "Refresh state")
check("periodic refresh on by default", app.autorefresh.get(), True)
check("interval default", app.autorefresh_secs.get(), "30")

# --- unsolicited frames from the device ------------------------------------ #
for o in range(1, 5):
    app.route_vars[o].set(o)
app.log.delete("1.0", "end")
app._apply_notifications([{"raw": "41 84 83 81", "from_device": True,
                           "instr": 1, "input": 4, "output": 3, "machine": 1}])
check("front-panel press moves the grid", app.route_vars[3].get(), 4)
check("and is logged as a device change",
      "changed on the device: {3: 4}" in app.log.get("1.0", "end"), True)

app.log.delete("1.0", "end")
app._apply_notifications([{"raw": "45 80 81 81", "from_device": True,
                           "instr": 5, "input": 0, "output": 1, "machine": 1}])
check("a non-switch frame leaves the grid alone", app.route_vars[1].get(), 1)
check("and is reported as ignored",
      "unsolicited frame ignored: 45 80 81 81" in app.log.get("1.0", "end"), True)

app.log.delete("1.0", "end")
app._apply_notifications([])
check("an empty batch does nothing", app.log.get("1.0", "end").strip(), "")


# --- worker attach/detach wiring ------------------------------------------- #
class _FakeProto:
    name = "fake"
    on_notify = None

    def __init__(self):
        self.polls = 0

    def poll_notifications(self, timeout=0.2):
        self.polls += 1
        return 0


class _BrokenProto(_FakeProto):
    def poll_notifications(self, timeout=0.2):
        self.polls += 1
        raise OSError("socket gone")


worker = g.Worker(g.queue.Queue())
fake = _FakeProto()
worker.attach("transport-stub", fake, "callback-stub")
check("attach stores the transport", worker.transport, "transport-stub")
check("attach wires on_notify", fake.on_notify, "callback-stub")
worker._listen()
check("idle listen polls the protocol", fake.polls, 1)

broken = _BrokenProto()
worker.attach("transport-stub", broken, None)
worker._listen()
worker._listen()
worker._listen()
check("a broken link is polled only once", broken.polls, 1)
check("and the failure is reported once", worker.results.qsize(), 1)
worker.detach()
check("detach clears the protocol", worker.proto, None)
worker._listen()
check("no polling once detached", broken.polls, 1)

# --- scheduling: never armed while disconnected ---------------------------- #
app.autorefresh.set(True)
app._schedule_autorefresh()
check("no timer while disconnected", app._autorefresh_job, None)

# --- scheduling: armed once connected, and only once ----------------------- #
app.connected = True
app._schedule_autorefresh()
check("timer armed when connected", app._autorefresh_job is not None, True)
first = app._autorefresh_job
app._schedule_autorefresh()
check("re-arming replaces the timer", app._autorefresh_job != first, True)

# --- scheduling: unticking cancels ----------------------------------------- #
app.autorefresh.set(False)
app._schedule_autorefresh()
check("timer cancelled when unticked", app._autorefresh_job, None)

# --- the visibility gate, against the real window -------------------------- #
# withdraw() unmaps the window directly, so it behaves the same with or without a
# window manager. iconify() is deliberately not used here: it is a *request* to
# the window manager, and under a bare X server there is none, so the window
# stays mapped and the check would pass or fail depending on the desktop.
root.update()
check("a mapped window counts as visible", app._is_visible(), True)
root.withdraw()
root.update()
check("a withdrawn window does not", app._is_visible(), False)
root.deiconify()
root.update()
check("visible again after restoring", app._is_visible(), True)


# --- and the state mapping itself, which is the part we wrote --------------- #
# Whether Tk reports "iconic" after a minimise is Tk's business and the window
# manager's. Ours is only which states count as visible, so that is checked
# directly instead of through a window we cannot reliably put into those states.
class FakeRoot:
    def __init__(self, value):
        self.value = value

    def state(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class RootHolder:
    def __init__(self, value):
        self.root = FakeRoot(value)


for tk_state, expected in (("normal", True), ("zoomed", True),
                           ("iconic", False), ("withdrawn", False),
                           ("icon", False)):
    check(f"Tk state {tk_state!r} means visible={expected}",
          g.App._is_visible(RootHolder(tk_state)), expected)
check("a destroyed window is not visible",
      g.App._is_visible(RootHolder(tk.TclError("application has been destroyed"))),
      False)

# --- a tick while minimised must not submit any job ------------------------ #
app.autorefresh.set(True)
app._is_visible = lambda: False
submitted.clear()
app._autorefresh_tick()
check("no job submitted while minimised", submitted, [])

# --- a tick while visible submits exactly one quiet status job ------------- #
app._is_visible = lambda: True
submitted.clear()
app._autorefresh_tick()
check("one automatic job while visible", submitted, ["status_auto"])

# --- an automatic read in flight is never queued twice --------------------- #
# Without this guard a short interval on Protocol 2000, where a read takes about
# a second, would queue faster than it drains and the job queue would grow.
app._auto_pending = False
submitted.clear()
app._refresh(quiet=True)
check("first automatic read submitted", submitted, ["status_auto"])
submitted.clear()
app._refresh(quiet=True)
check("second suppressed while one is pending", submitted, [])
app._handle("status_auto", {1: 1, 2: 2, 3: 3, 4: 4}, None)
check("pending cleared by the result", app._auto_pending, False)
submitted.clear()
app._refresh(quiet=True)
check("allowed again after the result", submitted, ["status_auto"])
# An error must clear it too, or the automatic refresh would jam for good.
app._handle("status_auto", None, RuntimeError("boom"))
check("pending cleared by an error", app._auto_pending, False)
check("an error turns the periodic refresh off", app.autorefresh.get(), False)
app.autorefresh.set(True)
app._auto_pending = False

# --- a manual read is never suppressed by a pending automatic one ---------- #
app._auto_pending = True
submitted.clear()
app._refresh()
check("manual read always goes through", submitted, ["status"])
app._auto_pending = False

# --- refresh on regaining focus ------------------------------------------- #
app._last_auto = 0.0
app._auto_pending = False
app._had_focus = False
app._has_focus = lambda: True
submitted.clear()
app._on_focus_gained()
check("focus gain refreshes", submitted, ["status_auto"])

# --- and not again immediately: alt-tabbing must not become a burst -------- #
# _auto_pending is cleared on purpose, so only the time gap can suppress this.
app._auto_pending = False
submitted.clear()
app._on_focus_gained()
check("second focus gain suppressed by the gap", submitted, [])

app._last_auto = time.monotonic() - (g.FOCUS_REFRESH_MIN_GAP + 0.1)
app._auto_pending = False
submitted.clear()
app._on_focus_gained()
check("refresh allowed once the gap elapsed", submitted, ["status_auto"])

# --- no focus refresh when the periodic refresh is off -------------------- #
app._last_auto = 0.0
app._auto_pending = False
app.autorefresh.set(False)
submitted.clear()
app._on_focus_gained()
check("focus gain ignored when unticked", submitted, [])
app.autorefresh.set(True)
app._auto_pending = False

# --- disconnecting cancels the timer -------------------------------------- #
app._schedule_autorefresh()
app._mark_disconnected()
check("timer cancelled on disconnect", app._autorefresh_job, None)

# --- a quiet apply logs only the differences ------------------------------ #
app.connected = True
for o in range(1, 5):
    app.route_vars[o].set(o)
before = app.log.get("1.0", "end").strip()
app._apply_status({1: 1, 2: 2, 3: 3, 4: 4}, quiet=True)
check("no log when nothing changed", app.log.get("1.0", "end").strip(), before)
app._apply_status({1: 1, 2: 2, 3: 3, 4: 2}, quiet=True)
check("grid follows the device", app.route_vars[4].get(), 2)
check("the change is logged",
      "changed on the device: {4: 2}" in app.log.get("1.0", "end"), True)

# --- config round-trip ---------------------------------------------------- #
app.autorefresh_secs.set("30")
app.autorefresh.set(True)
app._on_close()
saved = json.loads(g.CONFIG_PATH.read_text(encoding="utf-8"))
check("periodic refresh persisted", saved["autorefresh"], True)
check("interval persisted", saved["autorefresh_interval"], 30)

# --- reloading sanitises a hand-edited file ------------------------------- #
g.CONFIG_PATH.write_text('{"autorefresh_interval": 7, "autorefresh": "yes"}',
                         encoding="utf-8")
cfg = g.load_config()
check("an out-of-range interval falls back", cfg["autorefresh_interval"],
      g.DEFAULT_CONFIG["autorefresh_interval"])
check("a truthy string is coerced to bool", cfg["autorefresh"], True)


failed = [line for ok, line in results if not ok]
for ok, line in results:
    print(f"  {'OK  ' if ok else 'FAIL'} {line}")
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
