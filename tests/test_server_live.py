#!/usr/bin/env python3
#
# Part of kramer-vs44-remote-control. Copyright (C) 2026 Piero Biagini.
# Licensed under the GNU GPL v3 or later. See LICENSE for details.
"""Integration checks for kramer_server.py against a real matrix.

Covers what the offline test cannot: the device thread against a real socket,
the initial state read, event delivery end to end, and recovery after the link
drops - which is simulated by closing the socket under the thread.

    python tests/test_server_live.py --matrix 192.168.1.39

Read-only by default. To also verify a switch end to end, name an output you are
willing to have changed; its previous input is restored afterwards:

    python tests/test_server_live.py --matrix 192.168.1.39 --switch-output 4

Exits non-zero if any check fails.
"""

import argparse
import json
import queue
import sys
import threading
import time
import urllib.request
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kramer_server as ks
import kramer_vs44 as kv

ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("--matrix", default="192.168.1.39", metavar="HOST[:PORT]")
ap.add_argument("--switch-output", type=int, choices=(1, 2, 3, 4), metavar="N",
                help="also switch this output and restore it (CHANGES THE VIDEO "
                     "on that output for a few seconds)")
ap.add_argument("--store-preset", type=int, metavar="N",
                help="also verify storing, using preset slot N. OVERWRITES THAT "
                     "SLOT permanently: pick an empty one. Requires "
                     "--switch-output as well, since proving the slot really "
                     "captured the layout means changing an output and "
                     "recalling it back")
args = ap.parse_args()
if args.store_preset is not None:
    if not 1 <= args.store_preset <= 8:
        ap.error("--store-preset must be between 1 and 8")
    if not args.switch_output:
        ap.error("--store-preset also needs --switch-output")

# Never touch the real configuration file.
ks.CONFIG_PATH = Path(mkdtemp()) / "kramer_gui_config.json"

results = []
notes = []


def check(name, got, want):
    results.append((got == want, f"{name:46s} got={got!r} want={want!r}"))


def wait_for(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


host, _, port = args.matrix.partition(":")
link = ks.DeviceLink(host, int(port) if port else kv.DEFAULT_TCP_PORT)
server = ks.Server(("127.0.0.1", 0), link,
                   allow_preset_store=args.store_preset is not None)
link.on_change = server.publish_state
base = f"http://127.0.0.1:{server.server_address[1]}"
threading.Thread(target=server.serve_forever, daemon=True).start()

started = time.monotonic()
link.start()
if not wait_for(lambda: link.connected, 15):
    print(f"FAIL could not reach the matrix at {args.matrix}: {link.error}")
    sys.exit(1)
notes.append(f"connected and read the routing in {time.monotonic() - started:.1f} s")
check("the link comes up", link.connected, True)
check("the protocol is Protocol 2000", link.snapshot()["protocol"], "Protocol 2000")


def get(path):
    with urllib.request.urlopen(base + path, timeout=10) as res:
        return res.status, json.loads(res.read().decode())


def post(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method="POST")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as res:
        return res.status, json.loads(res.read().decode())


status, state = get("/api/state")
check("GET /api/state over a live link", status, 200)
routing = state["routing"]
check("every output has a value", sorted(routing), ["1", "2", "3", "4"])
notes.append(f"routing read from the device: {routing}")

# --- the event stream ------------------------------------------------------ #
events = queue.Queue()


def reader():
    try:
        with urllib.request.urlopen(base + "/api/events", timeout=60) as res:
            for line in res:
                line = line.decode().strip()
                if line.startswith("data:"):
                    events.put(json.loads(line[5:].strip()))
    except Exception:
        pass


threading.Thread(target=reader, daemon=True).start()
first = events.get(timeout=10)
check("a subscriber gets the live state at once", first["state"]["routing"], routing)

# --- optional: a real switch, end to end ---------------------------------- #
if args.switch_output:
    out = args.switch_output
    original = int(routing[str(out)])
    other = next(i for i in (1, 2, 3, 4) if i != original)

    status, state = post("/api/route", {"input": other, "output": out})
    check(f"POST route input {other} to output {out}", status, 200)
    check("the answer reflects the switch", state["routing"][str(out)], other)
    event = events.get(timeout=10)
    check("and the switch is pushed to subscribers",
          event["state"]["routing"][str(out)], other)

    status, state = post("/api/route", {"input": original, "output": out})
    check(f"output {out} restored to input {original}",
          state["routing"][str(out)], original)
    events.get(timeout=10)
    untouched = [o for o in ("1", "2", "3", "4") if o != str(out)]
    check("no other output moved",
          {o: state["routing"][o] for o in untouched},
          {o: routing[o] for o in untouched})

    # --- optional: storing, proven by a round trip ------------------------- #
    # A store that only flips the "slot is occupied" flag proves nothing about
    # the layout being captured. Change an output, recall, and see it come back.
    if args.store_preset is not None:
        slot = args.store_preset
        status, state = post(f"/api/preset/{slot}/store")
        check(f"POST /api/preset/{slot}/store", status, 200)
        check("the slot now reports as occupied", state["presets"][str(slot)], True)
        events.get(timeout=10)

        post("/api/route", {"input": other, "output": out})
        events.get(timeout=10)
        status, state = post(f"/api/preset/{slot}/recall")
        check("recalling the slot restores the stored layout",
              state["routing"], routing)
        events.get(timeout=10)
    else:
        notes.append("store check skipped (pass --store-preset N to include it)")
else:
    notes.append("switch check skipped (pass --switch-output N to include it)")

# --- recovery after the link drops ---------------------------------------- #
# Closing the transport under the device thread is the closest thing to pulling
# the cable that a test can do without touching the hardware.
link._transport.close()
went_down = wait_for(lambda: not link.connected, 10)
check("a dead socket is noticed", went_down, True)
status, state = get("/api/state")
check("the API keeps answering while down", status, 200)
check("and says so", state["connected"], False)
back = wait_for(lambda: link.connected,
                ks.DeviceLink.RECONNECT_DELAY * 3 + 15)
check("the link reconnects on its own", back, True)
if back:
    check("and the routing is read again", link.snapshot()["routing"], routing)

server.shutdown()
server.server_close()
link.stop()

failed = [line for ok, line in results if not ok]
for ok, line in results:
    print(f"  {'OK  ' if ok else 'FAIL'} {line}")
for note in notes:
    print(f"  ...  {note}")
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
