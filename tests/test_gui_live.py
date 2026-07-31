#!/usr/bin/env python3
#
# Part of kramer-vs44-remote-control. Copyright (C) 2026 Piero Biagini.
# Licensed under the GNU GPL v3 or later. See LICENSE for details.
"""Integration checks against a real matrix. Read-only unless asked otherwise.

Covers what the offline test cannot reach: the worker thread against a live
socket, the passive listener running in the idle gaps without stealing command
replies, and log muting during automatic reads.

    python tests/test_gui_live.py --host 192.168.1.39

By default nothing is switched. To also verify a real switch end to end, name an
output you are willing to have changed - it is restored to its previous input
afterwards:

    python tests/test_gui_live.py --host 192.168.1.39 --switch-output 3

Exits non-zero if any check fails.
"""

import argparse
import sys
import time
import tkinter as tk
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kramer_gui as g
import kramer_vs44 as kv

ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("--host", default="192.168.1.39", help="matrix IP address")
ap.add_argument("--port", type=int, help="TCP port (5000, 10001 or 50000)")
ap.add_argument("--switch-output", type=int, choices=(1, 2, 3, 4), metavar="N",
                help="also switch this output and restore it (CHANGES THE VIDEO "
                     "on that output for a few seconds)")
ap.add_argument("--drop-link", action="store_true",
                help="also close the socket under the worker and check that the "
                     "window notices and reconnects. Takes a while: the matrix "
                     "has been measured refusing a new connection for about 90 "
                     "seconds after one is lost")
args = ap.parse_args()

# Never touch the real configuration file.
g.CONFIG_PATH = Path(mkdtemp()) / "kramer_gui_config.json"

results = []
notes = []


def check(name, got, want):
    results.append((got == want, f"{name:44s} got={got!r} want={want!r}"))


root = tk.Tk()
app = g.App(root, g.load_config(),
            argparse.Namespace(host=args.host, port=args.port, serial=None,
                               heartbeat=kv.HEARTBEAT))

# Record which results land, so the waits are driven by what happened rather than
# by a guess about how slow the device is.
seen = []
_orig_handle = app._handle
app._handle = lambda tag, res, err: (seen.append(tag), _orig_handle(tag, res, err))[1]


def pump(seconds, until=None):
    """Run the Tk event loop, so _drain keeps processing worker results."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        root.update()
        if until and until():
            return True
        time.sleep(0.02)
    return False


def quiet_read():
    seen.clear()
    app._auto_pending = False
    app._refresh(quiet=True)
    return pump(30, until=lambda: "status_auto" in seen)


app._toggle_connect()
if not pump(15, until=lambda: app.connected):
    print(f"FAIL could not connect to {args.host}. Is the matrix powered on?")
    sys.exit(1)
check("connects over TCP", app.connected, True)

# The connect handler fires one visible refresh; wait for it to actually land.
started = time.monotonic()
landed = pump(30, until=lambda: "status" in seen)
check("initial state read completes", landed, True)
notes.append(f"a full state read took {time.monotonic() - started:.1f} s")

routing = {o: v.get() for o, v in app.route_vars.items()}
check("grid populated by the initial read", all(v >= 0 for v in routing.values()), True)
notes.append(f"routing read from the device: {routing}")

# --- an automatic read must leave no trace in the log ---------------------- #
app.log.delete("1.0", "end")
check("no header line for an automatic read", app.log.get("1.0", "end").strip(), "")
check("the automatic read landed", quiet_read(), True)
logged = app.log.get("1.0", "end")
check("no TX line logged", "TX ->" in logged, False)
check("no RX line logged", "RX <-" in logged, False)
check("nothing logged at all", logged.strip(), "")
check("grid unchanged by the automatic read",
      {o: v.get() for o, v in app.route_vars.items()}, routing)

# --- a manual read must log the bytes ------------------------------------- #
app.log.delete("1.0", "end")
seen.clear()
app._refresh()
pump(30, until=lambda: "status" in seen)
check("manual read logs TX", "TX ->" in app.log.get("1.0", "end"), True)
check("mute flag released", app.worker.transport._muted, False)

# --- the passive listener is wired up ------------------------------------- #
check("on_notify wired after connect", callable(app.worker.proto.on_notify), True)
check("the link is up, not merely 'not failed'", app.link_state, "up")

# --- repeated reads stay correct with the listener running in between ----- #
# Between reads the worker sits in poll_notifications(). If that stole replies,
# these reads would come back wrong or empty.
for n in (2, 3):
    check(f"read #{n} still matches", quiet_read() and
          {o: v.get() for o, v in app.route_vars.items()}, routing)

# --- optional: a real switch, end to end --------------------------------- #
if args.switch_output:
    out = args.switch_output
    original = routing[out]
    other = next(i for i in (1, 2, 3, 4) if i != original)
    seen.clear()
    app._switch(other, out)
    pump(10, until=lambda: "switch" in seen)
    quiet_read()
    check(f"device reports input {other} on output {out}",
          app.route_vars[out].get(), other)

    seen.clear()
    app._switch(original, out)
    pump(10, until=lambda: "switch" in seen)
    quiet_read()
    check(f"output {out} restored to input {original}",
          app.route_vars[out].get(), original)
    untouched = [o for o in (1, 2, 3, 4) if o != out]
    check("no other output moved",
          {o: app.route_vars[o].get() for o in untouched},
          {o: routing[o] for o in untouched})
else:
    notes.append("switch check skipped (pass --switch-output N to include it)")

# --- losing the link and getting it back ---------------------------------- #
# Closing the socket under the worker is the closest a test can get to pulling
# the cable. What matters is that the window stops claiming a routing it can no
# longer verify, and repairs itself without anyone clicking anything.
if args.drop_link:
    app.worker.transport.close()
    went_down = pump(30, until=lambda: app.link_state == "reconnecting")
    check("the window notices the link is gone", went_down, True)
    check("and stops calling itself connected", app.connected, False)
    check("the grid stops claiming a routing",
          set(v.get() for v in app.route_vars.values()), {-1})
    check("the indicator says what is happening",
          "reconnecting" in app.status.cget("text"), True)
    check("and the button still offers to stop it",
          app.connect_btn.cget("text"), "Disconnect")

    started = time.monotonic()
    back = pump(180, until=lambda: app.link_state == "up")
    check("it reconnects on its own", back, True)
    if back:
        notes.append(f"reconnected after {time.monotonic() - started:.0f}s")
        check("and reads the routing again",
              pump(30, until=lambda: any(v.get() >= 0
                                         for v in app.route_vars.values())),
              True)
else:
    notes.append("link-drop check skipped (pass --drop-link to include it)")

# --- the periodic timer arms and is cancelled on disconnect --------------- #
app.autorefresh.set(True)
app._schedule_autorefresh()
check("timer armed against a live device", app._autorefresh_job is not None, True)

app._toggle_connect()
pump(10, until=lambda: not app.connected)
check("disconnects cleanly", app.connected, False)
check("and goes idle rather than retrying", app.link_state, "idle")
check("timer cancelled after disconnect", app._autorefresh_job, None)

app.autorefresh.set(False)
app._on_close()

failed = [line for ok, line in results if not ok]
for ok, line in results:
    print(f"  {'OK  ' if ok else 'FAIL'} {line}")
for note in notes:
    print(f"  ...  {note}")
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
