#!/usr/bin/env python3
#
# Part of kramer-vs44-remote-control. Copyright (C) 2026 Piero Biagini.
# Licensed under the GNU GPL v3 or later. See LICENSE for details.
"""HTTP layer checks for kramer_server.py: no matrix, no network beyond loopback.

The device link is replaced by a stub that records what was asked of it, so the
routes, the validation, the label round-trip, the token gate and the event stream
can be exercised without hardware. Exits non-zero if any check fails.
"""

import json
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kramer_server as ks

# Never touch the real configuration file.
ks.CONFIG_PATH = Path(mkdtemp()) / "kramer_gui_config.json"

results = []


def check(name, got, want):
    results.append((got == want, f"{name:46s} got={got!r} want={want!r}"))


class FakeProto:
    """Stands in for Protocol2000. Records commands, replies like the device."""
    name = "Protocol 2000"

    def __init__(self):
        self.switches = []
        self.recalls = []
        self.stores = []
        self.defined = {4}
        self.routing = {1: 1, 2: 2, 3: 0, 4: 0}

    def switch(self, inp, out):
        self.switches.append((inp, out))
        for o in (range(1, 5) if out == 0 else [out]):
            self.routing[o] = inp
        return [{"raw": "41 82 83 81", "from_device": True, "instr": 1,
                 "input": inp, "output": out, "machine": 1}]

    def preset_recall(self, n):
        self.recalls.append(n)
        self.routing = {1: 4, 2: 4, 3: 4, 4: 4}

    def preset_store(self, n):
        self.stores.append(n)
        self.defined.add(n)

    def preset_defined(self, n):
        return n in self.defined

    def status(self):
        return dict(self.routing)


class FakeLink:
    """Same surface as DeviceLink, without a socket."""

    def __init__(self):
        self.proto = FakeProto()
        self.routing = self.proto.status()
        self.presets = {n: n in self.proto.defined for n in range(1, 9)}
        self.connected = True
        self.detail = "TCP 10.0.0.1:5000"
        self.error = None
        self.fail_with = None

    def call(self, fn, timeout=10.0):
        if self.fail_with:
            raise self.fail_with
        if not self.connected:
            raise ConnectionError("not connected to the matrix")
        return fn(self.proto)

    # Real implementation, borrowed so the occupancy refresh is exercised rather
    # than faked away.
    store_preset = ks.DeviceLink.store_preset
    _read_presets = staticmethod(ks.DeviceLink._read_presets)

    def snapshot(self):
        return {"connected": self.connected, "detail": self.detail,
                "protocol": self.proto.name if self.connected else None,
                "routing": {str(o): i for o, i in sorted(self.routing.items())},
                "presets": {str(n): v for n, v in sorted(self.presets.items())},
                "error": self.error}


link = FakeLink()
server = ks.Server(("127.0.0.1", 0), link)
link.on_change = server.publish_state
base = f"http://127.0.0.1:{server.server_address[1]}"
threading.Thread(target=server.serve_forever, daemon=True).start()


def request(method, path, body=None, headers=None, timeout=5):
    """Returns (status, parsed-json-or-raw-text)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers=headers or {})
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode()
            status = res.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode(), e.code
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def raw_body(path, timeout=5):
    with urllib.request.urlopen(base + path, timeout=timeout) as res:
        return res.status, res.headers.get("Content-Type"), res.read()


# --- state ----------------------------------------------------------------- #
status, payload = request("GET", "/api/state")
check("GET /api/state", status, 200)
check("state reports the routing", payload["routing"],
      {"1": 1, "2": 2, "3": 0, "4": 0})
check("state reports the protocol", payload["protocol"], "Protocol 2000")

# --- the page -------------------------------------------------------------- #
status, ctype, body = raw_body("/")
check("GET / serves the page", status, 200)
check("as HTML", ctype, "text/html; charset=utf-8")
check("and it is the real page", b"<title>Kramer VS-44HN</title>" in body, True)
check("with no external request in it",
      b"http://" not in body.replace(b"http://127.0.0.1", b""), True)

# --- routing --------------------------------------------------------------- #
status, payload = request("POST", "/api/route", {"input": 3, "output": 2})
check("POST /api/route", status, 200)
check("the command reached the device", link.proto.switches[-1], (3, 2))
check("the answer carries the new routing", payload["routing"]["2"], 3)

status, payload = request("POST", "/api/route", {"input": 1, "output": 0})
check("output 0 means every output", status, 200)
check("and all four are updated", payload["routing"],
      {"1": 1, "2": 1, "3": 1, "4": 1})

# --- routing: rejected input ----------------------------------------------- #
before = len(link.proto.switches)
for body, expected in (
        ({"input": 2, "output": 9}, '"output" must be between 0 and 4'),
        ({"input": -1, "output": 2}, '"input" must be between 0 and 4'),
        ({"input": "2", "output": 2}, '"input" must be an integer'),
        ({"input": True, "output": 2}, '"input" must be an integer'),
        ({"input": 2}, 'missing "output"'),
        ({}, 'missing "input"')):
    status, payload = request("POST", "/api/route", body)
    check(f"rejected {json.dumps(body)}", (status, payload.get("error")),
          (400, expected))
check("nothing was sent to the device", len(link.proto.switches), before)

# --- presets --------------------------------------------------------------- #
status, payload = request("POST", "/api/preset/3/recall")
check("POST /api/preset/3/recall", status, 200)
check("the preset reached the device", link.proto.recalls[-1], 3)
check("the routing was read back", payload["routing"],
      {"1": 4, "2": 4, "3": 4, "4": 4})

for n, code in ((0, 400), (9, 400), (99, 400)):
    status, payload = request("POST", f"/api/preset/{n}/recall")
    check(f"preset {n} rejected", status, code)

# --- state reports which slots hold a layout ------------------------------- #
status, payload = request("GET", "/api/state")
check("occupied slots are reported", payload["presets"]["4"], True)
check("empty ones too", payload["presets"]["1"], False)
check("storing is off by default", payload["allow_preset_store"], False)

# --- storing is refused unless the service was started for it -------------- #
status, payload = request("POST", "/api/preset/2/store")
check("store refused with 403", status, 403)
check("with an actionable message",
      "--allow-preset-store" in payload["error"], True)
check("and nothing reached the device", link.proto.stores, [])

server.allow_preset_store = True
status, payload = request("GET", "/api/state")
check("the capability is advertised", payload["allow_preset_store"], True)

status, payload = request("POST", "/api/preset/2/store")
check("POST /api/preset/2/store", status, 200)
check("the store reached the device", link.proto.stores, [2])
check("and the slot now shows as occupied", payload["presets"]["2"], True)
check("while the others are unchanged", payload["presets"]["1"], False)
check("an occupied slot stays occupied", payload["presets"]["4"], True)

for n in (0, 9, 99):
    status, payload = request("POST", f"/api/preset/{n}/store")
    check(f"store into preset {n} rejected", status, 400)
check("still only one store happened", link.proto.stores, [2])

link.connected = False
status, payload = request("POST", "/api/preset/3/store")
check("store with no link is 503", status, 503)
link.connected = True
check("and it never reached the device", link.proto.stores, [2])
server.allow_preset_store = False

# --- labels ---------------------------------------------------------------- #
status, payload = request("GET", "/api/labels")
check("GET /api/labels falls back to defaults", payload["inputs"],
      ["IN 1", "IN 2", "IN 3", "IN 4"])

new = {"inputs": ["Desktop", "Laptop A", "Laptop B", "Spare"]}
status, payload = request("PUT", "/api/labels", new)
check("PUT /api/labels", status, 200)
check("names are returned", payload["inputs"], new["inputs"])
check("names are persisted",
      json.loads(ks.CONFIG_PATH.read_text(encoding="utf-8"))["inputs"],
      new["inputs"])
check("untouched groups keep their defaults", payload["outputs"],
      ["OUT 1", "OUT 2", "OUT 3", "OUT 4"])

# The Tkinter GUI owns other keys in the same file: writing labels must not
# remove them, which is the whole point of the read-modify-write.
data = json.loads(ks.CONFIG_PATH.read_text(encoding="utf-8"))
data["host"] = "192.168.1.99"
data["autorefresh_interval"] = 60
ks.CONFIG_PATH.write_text(json.dumps(data), encoding="utf-8")
request("PUT", "/api/labels", {"outputs": ["Left", "Right", "Wing", "Spare"]})
after = json.loads(ks.CONFIG_PATH.read_text(encoding="utf-8"))
check("foreign keys survive a label write", after.get("host"), "192.168.1.99")
check("and so do their values", after.get("autorefresh_interval"), 60)
check("while the labels did change", after["outputs"][0], "Left")

for body, expected in (
        ({"inputs": ["a", "b"]}, '"inputs" must be a list of 4 names'),
        ({"inputs": [1, 2, 3, 4]}, '"inputs" must contain strings only'),
        ({"nothing": []}, "nothing to update: expected inputs, outputs "
                          "or presets")):
    status, payload = request("PUT", "/api/labels", body)
    check(f"rejected labels {json.dumps(body)}",
          (status, payload.get("error")), (400, expected))

status, payload = request("PUT", "/api/labels",
                          {"presets": ["  padded  "] + ["x"] * 7})
check("a name is trimmed", payload["presets"][0], "padded")
status, payload = request("PUT", "/api/labels", {"inputs": ["y" * 100] + ["x"] * 3})
check("and capped in length", len(payload["inputs"][0]), 40)

# --- unknown routes -------------------------------------------------------- #
status, payload = request("GET", "/api/nope")
check("unknown GET is 404", status, 404)
status, payload = request("POST", "/api/nope")
check("unknown POST is 404", status, 404)
status, payload = request("PUT", "/api/nope")
check("unknown PUT is 404", status, 404)

# --- the link being down --------------------------------------------------- #
link.connected = False
status, payload = request("POST", "/api/route", {"input": 1, "output": 1})
check("a switch with no link is 503", status, 503)
status, payload = request("GET", "/api/state")
check("but the state is still served", status, 200)
check("and reports the truth", payload["connected"], False)
link.connected = True

link.fail_with = TimeoutError("the matrix did not answer within 10 s")
status, payload = request("POST", "/api/route", {"input": 1, "output": 1})
check("a timeout is 504", status, 504)
link.fail_with = None

# --- the token gate -------------------------------------------------------- #
server.token = "s3cr3t"
status, payload = request("GET", "/api/state")
check("no token is refused", status, 401)
status, payload = request("GET", "/api/state?token=s3cr3t")
check("the right token in the query is accepted", status, 200)
status, payload = request("GET", "/api/state?token=wrong")
check("a wrong token is refused", status, 401)
status, payload = request("GET", "/api/state",
                          headers={"Authorization": "Bearer s3cr3t"})
check("a bearer header is accepted", status, 200)
status, payload = request("POST", "/api/route", {"input": 1, "output": 1})
check("a command with no token is refused too", status, 401)
server.token = None

# --- the event stream ------------------------------------------------------ #
received = queue.Queue()


def reader():
    try:
        with urllib.request.urlopen(base + "/api/events", timeout=10) as res:
            for line in res:
                line = line.decode().strip()
                if line.startswith("data:"):
                    received.put(json.loads(line[5:].strip()))
    except Exception:
        pass


threading.Thread(target=reader, daemon=True).start()
first = received.get(timeout=5)
check("a new subscriber gets the state at once", first["type"], "state")
check("with the current routing", first["state"]["routing"]["1"], 4)

request("POST", "/api/route", {"input": 2, "output": 3})
event = received.get(timeout=5)
check("a switch is pushed to subscribers", event["type"], "state")
check("carrying the change", event["state"]["routing"]["3"], 2)

request("PUT", "/api/labels", {"inputs": ["A", "B", "C", "D"]})
event = received.get(timeout=5)
check("a label change is pushed too", event["type"], "labels")
check("with the new names", event["labels"]["inputs"], ["A", "B", "C", "D"])

# A front-panel press reaches the browser through the same path.
link.routing[4] = 3
server.publish_state()
event = received.get(timeout=5)
check("a device-side change is pushed", event["state"]["routing"]["4"], 3)

# --- a stalled browser must not be able to block anything ------------------ #
hub = ks.EventHub()
stalled = hub.subscribe()
for _ in range(ks.SSE_BACKLOG + 20):
    hub.publish({"type": "state"})
check("events are dropped, not buffered without bound",
      stalled.qsize(), ks.SSE_BACKLOG)


# --- the liveness check ---------------------------------------------------- #
# A matrix switched off without closing its socket leaves reads timing out,
# which is indistinguishable from an idle device. These checks cover the probe
# that turns that silence into a verdict.

class BeatProto:
    name = "Protocol 2000"
    on_notify = None

    def __init__(self, answer):
        self.answer = answer            # frames, [] for silence, or an exception
        self.pings = 0

    def ping(self):
        self.pings += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def poll_notifications(self, timeout=0.2):
        return 0


def beat_link(answer, heartbeat=30.0, silent_for=100.0):
    dl = ks.DeviceLink("10.0.0.1", 5000, heartbeat=heartbeat)
    dl._proto = BeatProto(answer)
    dl.connected = True
    dl._last_ok = time.monotonic() - silent_for
    return dl


alive = [{"raw": "7D 80 AC 81", "from_device": True, "instr": 61,
          "input": 0, "output": 44, "machine": 1}]

dl = beat_link(alive)
dl._maybe_beat()
check("a live matrix is probed once", dl._proto.pings, 1)
check("and stays connected", dl.connected, True)
check("and the silence timer is reset", time.monotonic() - dl._last_ok < 1, True)

# The important case: the command goes out, nothing comes back, and _cmd returns
# an empty list without raising. A check that only watched for exceptions would
# report the link as healthy forever.
dl = beat_link([])
dl._maybe_beat()
check("silence counts as a failure", dl.connected, False)
check("and is explained", "liveness" in (dl.error or ""), True)

dl = beat_link(OSError("connection reset"))
dl._maybe_beat()
check("a socket error drops the link too", dl.connected, False)

dl = beat_link(ConnectionError("the device closed the connection"))
dl._maybe_beat()
check("so does the close reported by the transport", dl.connected, False)

# No probing while the matrix is demonstrably answering: using it is proof enough.
dl = beat_link(alive, silent_for=1.0)
dl._maybe_beat()
check("recent traffic means no probe", dl._proto.pings, 0)
check("and the link is left alone", dl.connected, True)

dl = beat_link(alive, heartbeat=0)
dl._maybe_beat()
check("heartbeat 0 disables the probe", dl._proto.pings, 0)

# Anything the device says on its own also counts as proof of life.
dl = beat_link(alive)
dl._proto.poll_notifications = lambda timeout=0.2: 2
dl._listen()
check("an unsolicited frame resets the timer",
      time.monotonic() - dl._last_ok < 1, True)
dl._maybe_beat()
check("so no probe follows it", dl._proto.pings, 0)


# --- a retry loop must not repeat itself in the log ------------------------ #
logged = []
real_log = ks.log
ks.log = logged.append
try:
    dl = ks.DeviceLink("203.0.113.1", 5000)     # reserved, never routed
    dl.RECONNECT_DELAY = 0
    dl._stop.set()                              # so wait() returns immediately
    for _ in range(5):
        dl._connect()
finally:
    ks.log = real_log
check("the same failure is logged once", len(logged), 1)
check("and says retries continue", "retrying" in logged[0], True)
check("while the state still reports the error", dl.connected, False)

server.shutdown()
server.server_close()

failed = [line for ok, line in results if not ok]
for ok, line in results:
    print(f"  {'OK  ' if ok else 'FAIL'} {line}")
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
