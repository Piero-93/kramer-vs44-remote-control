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
            argparse.Namespace(host=None, port=None, serial=None,
                               heartbeat=g.kv.HEARTBEAT))

# The worker thread is already running and drains the queue immediately, so
# inspecting jobs.qsize() would be a race. Record the submissions instead.
submitted = []
app.worker.submit = lambda tag, fn, **kw: submitted.append(tag)


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


# --- the worker owns the link ---------------------------------------------- #
class _FakeTransport:
    def __init__(self, heard_ago=0.0):
        self.last_rx = g.time.monotonic() - heard_ago
        self.muted = False
        self.closed = False

    def set_muted(self, muted):
        self.muted = muted

    def close(self):
        self.closed = True


class _FakeProto:
    name = "fake"
    on_notify = None

    def __init__(self, answer=None):
        self.answer = answer
        self.polls = 0
        self.pings = 0

    def poll_notifications(self, timeout=0.2):
        self.polls += 1
        return 0

    def ping(self):
        self.pings += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def drain(worker):
    out = []
    while True:
        try:
            out.append(worker.results.get_nowait())
        except g.queue.Empty:
            return out


def linked(answer=None, heartbeat=30.0, heard_ago=0.0):
    """A worker holding a link, without any of it being real."""
    w = g.Worker(g.queue.Queue(), heartbeat=heartbeat)
    w.transport = _FakeTransport(heard_ago)
    w.proto = _FakeProto(answer)
    w.want_link = True
    w.monitor.mark_ok()
    w.monitor._last_ok = g.time.monotonic() - heard_ago
    return w


alive = [{"raw": "7D 80 AC 81", "from_device": True, "instr": 61,
          "input": 0, "output": 44, "machine": 1}]

w = linked()
w._listen()
check("an idle worker listens", w.proto.polls, 1)

# A decoding bug is not a link failure: reconnecting cannot fix it, and letting
# it escape would kill the thread and leave the window silently inert.
w = linked()
w.proto.poll_notifications = lambda timeout=0.2: (_ for _ in ()).throw(
    ValueError("bad frame"))
w._listen()
w._listen()
w._listen()
check("a bug while listening is reported once", len(drain(w)), 1)
check("and does not drop the link", w.proto is not None, True)

# A socket error is a link failure, and must start the reconnection.
w = linked()
w.proto.poll_notifications = lambda timeout=0.2: (_ for _ in ()).throw(
    OSError("socket gone"))
w._listen()
events = drain(w)
check("a socket error drops the link", w.proto, None)
check("and says it is retrying",
      [(t, r["retrying"]) for t, r, _ in events], [("link_down", True)])
check("and the transport was closed", w.transport, None)

# --- the liveness probe, mirroring the service ----------------------------- #
w = linked(answer=alive, heard_ago=1000)
w._maybe_beat()
check("a quiet link is probed", w.proto.pings, 1)
check("and stays up when it answers", w.proto is not None, True)
check("the probe is not written to the byte log", w.transport.muted, False)

# The case the whole design exists for: the probe is sent, nothing comes back,
# and no exception is raised.
w = linked(answer=[], heard_ago=1000)
w._maybe_beat()
check("silence counts as a failure", w.proto, None)
check("with a reason that says so",
      "liveness" in drain(w)[0][1]["reason"], True)

w = linked(answer=OSError("reset"), heard_ago=1000)
w._maybe_beat()
check("a socket error during the probe drops it too", w.proto, None)

w = linked(answer=alive, heard_ago=1)
w._maybe_beat()
check("recent bytes mean no probe", w.proto.pings, 0)

w = linked(answer=alive, heartbeat=0, heard_ago=1000)
w._maybe_beat()
check("heartbeat 0 disables it", w.proto.pings, 0)

# The trap that does not exist in the service: this program refreshes on the
# same period as it probes, so if a job that returned nothing counted as proof
# of life the probe would never fire at all.
w = linked(answer=[], heard_ago=1000)
w.transport.last_rx = 0.0
check("a read that answered nothing does not postpone the probe",
      w.monitor.due(w.transport), True)

# --- reconnecting ----------------------------------------------------------- #
attempts = []


def flaky_connector():
    attempts.append(1)
    if len(attempts) < 3:
        raise OSError("timed out")
    return _FakeTransport(), _FakeProto(alive), "TCP 10.0.0.1:5000 — fake"


w = g.Worker(g.queue.Queue())
w.RECONNECT_DELAY = 0
w.link(flaky_connector, None)
w._down_since = g.time.monotonic() - 5      # pretend a link existed and died
w._connect()
w._connect()
w._connect()
events = drain(w)
check("it kept retrying", len(attempts), 3)
check("the same failure is reported once, not per attempt",
      [t for t, _, _ in events].count("link_retry"), 1)
check("and then it comes up", [t for t, _, _ in events][-1], "link_up")
check("flagged as a reconnection", events[-1][1]["relink"], True)

# A first attempt that fails must not start a retry loop behind a typo.
w = g.Worker(g.queue.Queue())
w.link(lambda: (_ for _ in ()).throw(OSError("no route to host")), None)
w._connect()
events = drain(w)
check("a first failure is reported as connect", events[0][0], "connect")
check("and stops asking", w.want_link, False)

# A queued action with no link fails fast instead of reaching a missing protocol.
# The loop is run inline: the job first, then the sentinel that ends it.
ran = []
w = g.Worker(g.queue.Queue())
w.submit("switch", lambda _w: ran.append("ran"))
w.jobs.put(None)
w.run()
tag, res, err = w.results.get_nowait()
check("an action with no link fails fast", (tag, type(err).__name__),
      ("switch", "ConnectionError"))
check("and the action itself never ran", ran, [])

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
# It used to switch the checkbox off, which was a stand-in for the missing
# reconnection. Now a real failure drops the link and the indicator says so, so
# the user's setting is never touched behind their back.
check("an error leaves the setting alone", app.autorefresh.get(), True)
app._auto_pending = False

# And an automatic read that fails only because the link is already down should
# not add a line saying what the indicator is already showing.
before_log = app.log.get("1.0", "end")
app._handle("status_auto", None, ConnectionError("the link is down"))
check("a read refused for a known-down link is not logged",
      app.log.get("1.0", "end"), before_log)

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

# --- what the window does when the link goes away -------------------------- #
app.connected = True
app.link_state = "up"
app.connect_btn.configure(text="Disconnect")
for o in range(1, 5):
    app.route_vars[o].set(o)
app.autorefresh.set(True)
app.log.delete("1.0", "end")


def colour_of(widget):
    """cget("foreground") hands back a Tcl colour object, not a string."""
    return str(widget.cget("foreground"))

app._handle("link_down", {"reason": "no reply to the liveness check",
                          "retrying": True}, None)
check("a lost link is not 'connected'", app.connected, False)
check("but it is not idle either", app.link_state, "reconnecting")
check("the button still offers to stop it",
      app.connect_btn.cget("text"), "Disconnect")
# A greyed-out radio button still reads as set, and the front panel can move
# things while we are not looking. Showing nothing is the honest state.
check("the grid stops claiming a routing",
      [v.get() for v in app.route_vars.values()], [-1, -1, -1, -1])
check("the indicator counts the wait",
      "reconnecting" in app.status.cget("text"), True)
check("it is amber, not green or red", colour_of(app.status), "#a70")
check("and the reason is logged once",
      app.log.get("1.0", "end").count("link lost"), 1)
check("the user's refresh setting is untouched", app.autorefresh.get(), True)
check("with no pending automatic read left over", app._auto_pending, False)

app._handle("link_retry", {"error": "timed out"}, None)
check("a retry adds one line", "still trying: timed out"
      in app.log.get("1.0", "end"), True)

submitted.clear()
app._handle("link_up", {"detail": "TCP 10.0.0.1:5000 — fake", "relink": True,
                        "elapsed": 92}, None)
check("coming back marks it connected", app.connected, True)
check("and up", app.link_state, "up")
check("the indicator shows the link", app.status.cget("text"),
      "● TCP 10.0.0.1:5000 — fake")
check("green again", colour_of(app.status), "#2a7")
check("the outage length is logged",
      "reconnected after 92s" in app.log.get("1.0", "end"), True)
check("and the routing is read again immediately", submitted, ["status"])

# A deliberate disconnect is a different thing from a lost link.
app._handle("link_down", {"reason": "disconnected", "retrying": False}, None)
check("a deliberate disconnect goes idle", app.link_state, "idle")
check("and the button offers to connect", app.connect_btn.cget("text"),
      "Connect")

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
check("a readable file reports no problem", g.CONFIG_PROBLEM, None)

# An unusable settings file must be *said*, not printed into a console that a
# windowed build does not have. The window shows it once the log panel exists.
g.CONFIG_PATH.write_text("{ not json at all", encoding="utf-8")
g.load_config()
check("an unreadable settings file is recorded", g.CONFIG_PROBLEM is not None,
      True)
check("and names the file", str(g.CONFIG_PATH) in g.CONFIG_PROBLEM, True)


failed = [line for ok, line in results if not ok]
for ok, line in results:
    print(f"  {'OK  ' if ok else 'FAIL'} {line}")
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
