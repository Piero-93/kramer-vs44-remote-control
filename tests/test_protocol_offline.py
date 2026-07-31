#!/usr/bin/env python3
#
# Part of kramer-vs44-remote-control. Copyright (C) 2026 Piero Biagini.
# Licensed under the GNU GPL v3 or later. See LICENSE for details.
"""Unit checks for kramer_vs44.py: no hardware, no sockets, no GUI.

Frame generation is compared against the byte sequences confirmed in the manual
and on real hardware, and the transport is driven through a fake socket so the
end-of-stream handling can be exercised - the case that decides whether a caller
notices the matrix going away.
"""

import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kramer_vs44 as kv

results = []


def check(name, got, want):
    results.append((got == want, f"{name:44s} got={got!r} want={want!r}"))


# --- helpers --------------------------------------------------------------- #
check("hexdump", kv.hexdump(bytes([0x01, 0x82, 0xAC])), "01 82 AC")
check("printable keeps ASCII", kv.printable(b"#VID2>3\r"), "#VID2>3.")
check("parse_raw reads hex", kv.parse_raw("01 82 83 81"),
      bytes([0x01, 0x82, 0x83, 0x81]))
check("parse_raw tolerates commas", kv.parse_raw("01,82,83,81"),
      bytes([0x01, 0x82, 0x83, 0x81]))
check("parse_raw appends CR to ASCII", kv.parse_raw("#MODEL?"), b"#MODEL?\r")

# --- Protocol 2000 frames, against the documented sequences ---------------- #
p2 = kv.Protocol2000(kv.Transport(dry_run=True))
cases = [
    ("SWITCH VIDEO in 2 to out 3", (1, 2, 3), "01 82 83 81"),
    ("STORE PRESET 1", (3, 1, 0), "03 81 80 81"),
    ("RECALL PRESET 1", (4, 1, 0), "04 81 80 81"),
    ("REQUEST STATUS OUTPUT 1", (5, 0, 1), "05 80 81 81"),
    ("LOCK FRONT PANEL", (30, 1, 0), "1E 81 80 81"),
    ("IDENTIFY MACHINE", (61, 1, 0), "3D 81 80 81"),
    ("SOFTWARE VERSION", (61, 3, 0), "3D 83 80 81"),
    ("DEFINE MACHINE, output count", (62, 2, 1), "3E 82 81 81"),
]
for name, (instr, inp, out), expected in cases:
    check(name, kv.hexdump(p2._frame(instr, inp, out)), expected)

check("CHANGE TO ASCII", kv.hexdump(kv.P2000_TO_P3000), "38 80 83 81")
check("the machine number lands in byte 4",
      kv.hexdump(kv.Protocol2000(kv.Transport(dry_run=True), machine=5)
                 ._frame(1, 2, 3)), "01 82 83 85")

# --- reply decoding -------------------------------------------------------- #
frame = kv.Protocol2000._decode(bytes([0x41, 0x84, 0x83, 0x81]))
check("one frame decoded", len(frame), 1)
check("the device flag is read", frame[0]["from_device"], True)
check("the instruction is masked out", frame[0]["instr"], 1)
check("the input is masked out", frame[0]["input"], 4)
check("the output is masked out", frame[0]["output"], 3)
check("two frames in one read", len(kv.Protocol2000._decode(
    bytes([0x41, 0x84, 0x83, 0x81, 0x45, 0x80, 0x81, 0x81]))), 2)
check("a partial frame is discarded", kv.Protocol2000._decode(b"\x41\x84"), [])


# --- the transport, against a fake socket --------------------------------- #
class FakeSocket:
    """Hands back a scripted sequence. b"" means end of stream, and None means
    the read times out, which is what an idle-but-alive device looks like."""

    def __init__(self, script):
        self.script = list(script)
        self.timeout = None
        self.closed = False
        self.at_eof = False

    def settimeout(self, t):
        self.timeout = t

    def recv(self, _n):
        # Once a socket has reported end of stream it keeps reporting it, so the
        # fake has to be sticky too or it would be easier to satisfy than reality.
        if self.at_eof:
            return b""
        if not self.script:
            raise socket.timeout()
        item = self.script.pop(0)
        if item is None:
            raise socket.timeout()
        if item == b"":
            self.at_eof = True
        return item

    def sendall(self, _data):
        pass

    def close(self):
        self.closed = True


def transport_with(script):
    t = kv.TcpTransport("10.0.0.1", 5000, dry_run=True)
    t.dry_run = False              # keep the constructor from opening a socket
    t.sock = FakeSocket(script)
    return t


# A complete reply stops the read at once when its length is declared, instead
# of waiting out the timeout: this is what took a state read from 4.4 s to 0.9 s.
t = transport_with([bytes([0x41, 0x82, 0x83, 0x81])])
check("expect stops at the declared length",
      kv.hexdump(t.recv(timeout=5.0, maxlen=64, expect=4)), "41 82 83 81")

# Without expect, a binary reply has no terminator, so the read can only end by
# timing out - correct, just slow.
t = transport_with([bytes([0x41, 0x82]), bytes([0x83, 0x81])])
check("without expect the chunks are joined",
      kv.hexdump(t.recv(timeout=0.3, maxlen=64)), "41 82 83 81")

# Silence is not an error: an idle device is a healthy device.
t = transport_with([None])
check("a timeout returns nothing", t.recv(timeout=0.2, maxlen=64), b"")

# End of stream is an error, and this is the check that matters: treating it as
# silence is how a caller stays convinced it is talking to a device that is gone.
t = transport_with([b""])
try:
    t.recv(timeout=0.2, maxlen=64)
    check("a closed socket raises", "no exception", "ConnectionError")
except ConnectionError as e:
    check("a closed socket raises ConnectionError", type(e).__name__,
          "ConnectionError")
    check("with a message that says so", "closed" in str(e), True)

# Data already received is not thrown away by a close that follows it.
t = transport_with([bytes([0x41, 0x82, 0x83, 0x81]), b""])
check("data before the close is returned",
      kv.hexdump(t.recv(timeout=0.3, maxlen=64)), "41 82 83 81")
try:
    t.recv(timeout=0.2, maxlen=64)
    check("and the next read raises", "no exception", "ConnectionError")
except ConnectionError:
    check("and the next read raises", "ConnectionError", "ConnectionError")

# --- notifications are separated from replies ----------------------------- #
# A front-panel press produces a frame indistinguishable in shape from a reply.
# Asking for output status must not return the press as if it were the answer.
seen = []
t = transport_with([bytes([0x41, 0x84, 0x83, 0x81]),      # unsolicited switch
                    bytes([0x45, 0x80, 0x82, 0x81])])     # the real reply
proto = kv.Protocol2000(t)
proto.on_notify = seen.append
reply = proto._cmd(5, inp=0, out=2)
check("the reply carries the instruction asked for", reply[0]["instr"], 5)
check("and its value", reply[0]["output"], 2)
check("the unsolicited frame was handed over separately", len(seen), 1)
check("with its own instruction", seen[0][0]["instr"], 1)
check("and it is flagged as coming from the device", seen[0][0]["from_device"],
      True)

# Nothing at all must not spin until the deadline.
t = transport_with([None])
proto = kv.Protocol2000(t)
check("no reply gives an empty list", proto._cmd(5, inp=0, out=1), [])

# --- the transport records when the device last said something ------------- #
t = transport_with([None])
check("nothing heard yet", t.last_rx, 0.0)
t.recv(timeout=0.2, maxlen=64)
check("a read with no data leaves it alone", t.last_rx, 0.0)

t = transport_with([bytes([0x41, 0x82, 0x83, 0x81])])
t.recv(timeout=0.2, maxlen=64, expect=4)
check("bytes arriving are timestamped", t.last_rx > 0, True)


# --- LinkMonitor: when to probe, and what counts as an answer -------------- #
class PingProto:
    """Answers a probe with frames, with silence, or by raising."""

    def __init__(self, answer):
        self.answer = answer
        self.pings = 0

    def ping(self):
        self.pings += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Heard:
    def __init__(self, ago):
        self.last_rx = time.monotonic() - ago


alive = [{"raw": "7D 80 AC 81", "from_device": True, "instr": 61,
          "input": 0, "output": 44, "machine": 1}]

m = kv.LinkMonitor()
check("the default heartbeat comes from one place", m.heartbeat, kv.HEARTBEAT)
check("a long silence is due for a probe", m.due(Heard(1000)), True)
check("recent bytes are not", m.due(Heard(1)), False)
check("a heartbeat of 0 disables probing", kv.LinkMonitor(0).due(Heard(1000)),
      False)
check("no transport at all still counts as silence",
      kv.LinkMonitor().due(None), True)

# mark_ok is the other source of proof: a connection that has just proved itself
# has not received anything yet.
m = kv.LinkMonitor()
m.mark_ok()
check("marking it ok postpones the probe", m.due(Heard(1000)), False)

# The whole reason this class exists: no reply is a failure, and it arrives
# without an exception to notice.
m = kv.LinkMonitor()
p = PingProto([])
check("an unanswered probe gives a reason", m.beat(p), "no reply to the "
                                                       "liveness check")
check("and it did send one", p.pings, 1)

m = kv.LinkMonitor()
check("an answered probe reports nothing wrong", m.beat(PingProto(alive)), None)
check("and resets the timer", m.due(Heard(1000)), False)

m = kv.LinkMonitor()
try:
    m.beat(PingProto(OSError("connection reset")))
    check("a socket error propagates", "no exception", "OSError")
except OSError:
    check("a socket error propagates", "OSError", "OSError")

# A retry loop must not repeat itself: a night with the matrix off would
# otherwise bury everything that mattered under identical lines.
m = kv.LinkMonitor()
check("the first occurrence is reported", m.first_time("timed out"), True)
check("the second is not", m.first_time("timed out"), False)
check("a different failure is", m.first_time("no route to host"), True)
m.mark_ok()
check("and after a recovery the same one is reported again",
      m.first_time("timed out"), True)


failed = [line for ok, line in results if not ok]
for ok, line in results:
    print(f"  {'OK  ' if ok else 'FAIL'} {line}")
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
