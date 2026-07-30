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
kramer_vs44.py - Test and diagnostic tool for Kramer VS-44HN (and VS-44H) HDMI matrices.

Verified against the VS-44HN manual, P/N 2900-300161 Rev 8.

Protocols (the device ALWAYS boots in Protocol 2000):
  - Protocol 2000 : 4 binary bytes.   RS-232 9600 8N1 or raw TCP.
  - Protocol 3000 : ASCII "#CMD<CR>". RS-232 9600 8N1 (NOT 115200) or raw TCP.

Default Ethernet settings: 192.168.1.39 / 255.255.255.0, TCP 5000 or 10001 or 50000.
There is no web UI: the device is driven with raw bytes/strings only.

WARNING
  - The IR remote works ONLY in Protocol 2000. Switching to Protocol 3000
    disables it until you switch back.
  - The rear RESET button clears ONLY the IP parameters: switching and presets
    survive. The Protocol 3000 #FACTORY command wipes everything instead:
    do not confuse the two.
  - At least 200 ms between consecutive commands, 1 s after EDID operations.

Examples
--------
  python kramer_vs44.py ports
  python kramer_vs44.py --serial COM3 probe
  python kramer_vs44.py discover 192.168.1.0/24
  python kramer_vs44.py --tcp 192.168.1.39 switch 2 3
  python kramer_vs44.py --tcp 192.168.1.39 preset-store 1
  python kramer_vs44.py --serial COM3 proto-switch p3000
  python kramer_vs44.py --serial COM3 --proto p3000 device-info
  python kramer_vs44.py --tcp 192.168.1.39 shell
  python kramer_vs44.py --serial COM3 raw "#HELP"
"""

import argparse
import ipaddress
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor

MIN_CMD_INTERVAL = 0.25          # the manual asks for >= 200 ms, keep a margin
EDID_CMD_INTERVAL = 1.0          # >= 1 s after EDID commands
DEFAULT_TCP_PORT = 5000
DISCOVER_PORTS = (5000, 10001, 50000)
BAUD = 9600                      # identical for both protocols on the VS-44HN

# Protocol 2000: instruction 56 (0x38), output=3 -> switch to Protocol 3000
P2000_TO_P3000 = bytes([0x38, 0x80, 0x83, 0x81])


# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #

class Transport:
    """Common interface, with the rate limit required by the protocol."""

    def __init__(self, verbose=False, dry_run=False):
        self.verbose = verbose
        self.dry_run = dry_run
        self._last_tx = 0.0
        self._next_gap = MIN_CMD_INTERVAL

    def _throttle(self):
        delta = time.monotonic() - self._last_tx
        if delta < self._next_gap:
            time.sleep(self._next_gap - delta)
        self._next_gap = MIN_CMD_INTERVAL

    def defer_next(self, seconds):
        """Lengthen the pause before the next command (use after EDID)."""
        self._next_gap = max(self._next_gap, seconds)

    def send(self, data: bytes):
        self._throttle()
        if self.verbose or self.dry_run:
            print(f"  TX -> {hexdump(data)}   {printable(data)}")
        if not self.dry_run:
            self._write(data)
        self._last_tx = time.monotonic()

    def recv(self, timeout=1.0, maxlen=512) -> bytes:
        if self.dry_run:
            return b""
        data = self._read(timeout, maxlen)
        if self.verbose and data:
            print(f"  RX <- {hexdump(data)}   {printable(data)}")
        return data

    def flush_input(self):
        if not self.dry_run:
            try:
                self._read(0.15, 4096)
            except Exception:
                pass

    def _write(self, data): raise NotImplementedError
    def _read(self, timeout, maxlen): raise NotImplementedError
    def close(self): pass

    def __enter__(self): return self
    def __exit__(self, *exc): self.close()


class TcpTransport(Transport):
    def __init__(self, host, port=DEFAULT_TCP_PORT, **kw):
        super().__init__(**kw)
        self.host, self.port = host, port
        self.sock = None
        if not self.dry_run:
            self.sock = socket.create_connection((host, port), timeout=3.0)

    def _write(self, data):
        self.sock.sendall(data)

    def _read(self, timeout, maxlen):
        self.sock.settimeout(timeout)
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = self.sock.recv(maxlen)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            if buf.endswith(b"\r\n") or len(buf) >= maxlen:
                break
        return buf

    def close(self):
        if self.sock:
            self.sock.close()

    def __str__(self):
        return f"TCP {self.host}:{self.port}"


class SerialTransport(Transport):
    def __init__(self, device, baud=BAUD, **kw):
        super().__init__(**kw)
        self.device, self.baud = device, baud
        self.ser = None
        if not self.dry_run:
            try:
                import serial  # pyserial
            except ImportError:
                sys.exit("ERROR: pyserial is required ->  pip install pyserial")
            self.ser = serial.Serial(device, baud, bytesize=8, parity="N",
                                     stopbits=1, timeout=0.1)

    def _write(self, data):
        self.ser.write(data)
        self.ser.flush()

    def _read(self, timeout, maxlen):
        deadline = time.monotonic() + timeout
        buf = b""
        while time.monotonic() < deadline:
            chunk = self.ser.read(maxlen)
            if chunk:
                buf += chunk
                if buf.endswith(b"\r\n"):
                    break
            elif buf:
                break
        return buf

    def close(self):
        if self.ser:
            self.ser.close()

    def __str__(self):
        return f"SERIAL {self.device} @ {self.baud} 8N1"


# --------------------------------------------------------------------------- #
# Protocol 2000 (the device default)
# --------------------------------------------------------------------------- #
#   byte1 = 0 D N5..N0     D=0 PC->matrix, D=1 matrix->PC ; N = instruction
#   byte2 = 1 I6..I0       INPUT
#   byte3 = 1 O6..O0       OUTPUT
#   byte4 = 1 OVR X M4..M0 machine number (1 -> 0x81)

class Protocol2000:
    name = "Protocol 2000"

    def __init__(self, transport, machine=1):
        self.t = transport
        self.machine = machine

    def _frame(self, instr, inp=0, out=0):
        return bytes([instr & 0x3F, 0x80 | (inp & 0x7F),
                      0x80 | (out & 0x7F), 0x80 | (self.machine & 0x1F)])

    def _cmd(self, instr, inp=0, out=0, expect_reply=True):
        self.t.send(self._frame(instr, inp, out))
        if not expect_reply:
            return None
        return self._decode(self.t.recv(timeout=1.0, maxlen=64))

    @staticmethod
    def _decode(raw):
        """Replies are 4-byte frames with the DESTINATION bit set (0x40)."""
        frames = []
        for i in range(0, len(raw) - 3, 4):
            b = raw[i:i + 4]
            frames.append({
                "raw": hexdump(b),
                "from_device": bool(b[0] & 0x40),
                "instr": b[0] & 0x3F,
                "input": b[1] & 0x7F,
                "output": b[2] & 0x7F,
                "machine": b[3] & 0x1F,
            })
        return frames

    # --- commands --------------------------------------------------------- #
    def ping(self):
        """Instruction 61 IDENTIFY MACHINE, input 1 = video machine name."""
        return self._cmd(61, inp=1, out=0)

    def version(self):
        """Instruction 61, input 3 = video software version.
        Reply: input = integer part, output = decimal part."""
        f = self._cmd(61, inp=3, out=0)
        return f"{f[0]['input']}.{f[0]['output']}" if f else None

    def io_count(self):
        """Instruction 62 DEFINE MACHINE: 1 = inputs, 2 = outputs, 3 = presets."""
        res = {}
        for key, code in (("inputs", 1), ("outputs", 2), ("presets", 3)):
            f = self._cmd(62, inp=code, out=1)   # out=1 -> video
            res[key] = f[0]["output"] if f else None
        return res

    def switch(self, inp, out):
        """out=0 -> all outputs ; inp=0 -> disconnect."""
        return self._cmd(1, inp=inp, out=out)

    def preset_store(self, n):
        return self._cmd(3, inp=n, out=0)       # output 0 = store, 1 = delete

    def preset_recall(self, n):
        return self._cmd(4, inp=n, out=0)

    def preset_defined(self, n):
        f = self._cmd(15, inp=n, out=0)
        return bool(f[0]["output"]) if f else None

    def signal(self, inp):
        """Instruction 15 with output=1: is a valid input detected?"""
        f = self._cmd(15, inp=inp, out=1)
        return bool(f[0]["output"]) if f else None

    def lock_front_panel(self, locked=True):
        return self._cmd(30, inp=1 if locked else 0, out=0)

    def is_locked(self):
        f = self._cmd(31, inp=0, out=0)
        return bool(f[0]["output"]) if f else None

    def status(self):
        """Instruction 5 for each output.
        NOTE 4 of the manual: in the reply the OUTPUT field carries the
        requested value, so the routed input is read from there. Always
        cross-check against the front-panel 7-segment display."""
        return {o: (lambda f: f[0]["output"] if f else None)(self._cmd(5, inp=0, out=o))
                for o in range(1, 5)}

    def to_protocol3000(self):
        """Instruction 56 CHANGE TO ASCII. This disables the IR remote."""
        self.t.send(P2000_TO_P3000)
        return self.t.recv(timeout=1.0)


# --------------------------------------------------------------------------- #
# Protocol 3000 (ASCII) - the actual VS-44HN command set
# --------------------------------------------------------------------------- #

class Protocol3000:
    name = "Protocol 3000"

    QUERIES = ("MODEL?", "VERSION?", "SN?", "BUILDDATE?", "PROT-VER?",
               "INFO-IO?", "INFO-PRST?", "LOCK-FP?")

    def __init__(self, transport, machine=1):
        self.t = transport

    def cmd(self, text, timeout=1.5):
        if not text.startswith("#"):
            text = "#" + text
        if len(text) > 64:
            raise ValueError("Protocol 3000: string longer than the 64-character limit.")
        self.t.send(text.encode() + b"\r")
        return self.t.recv(timeout=timeout, maxlen=1024).decode(errors="replace").strip()

    def ping(self):
        return self.cmd("")                     # handshake, expecting "~01@OK"

    def switch(self, inp, out):
        """The manual documents #VID1>1<CR>. Fall back to the spaced form if refused."""
        r = self.cmd(f"VID{inp}>{out}")
        if not r or "ERR" in r.upper():
            r = f"{r} | {self.cmd(f'VID {inp}>{out}')}"
        return r

    def status(self):
        return self.cmd("VID?")

    def preset_store(self, n):
        return self.cmd(f"PRST-STO {n}")

    def preset_recall(self, n):
        return self.cmd(f"PRST-RCL {n}")

    def presets(self):
        return {"PRST-LST?": self.cmd("PRST-LST?"),
                "PRST-VID?": self.cmd("PRST-VID?")}

    def signal(self, inp=None):
        return self.cmd("SIGNAL?" if inp is None else f"SIGNAL? {inp}")

    def display(self):
        return self.cmd("DISPLAY?")

    def identify_visual(self):
        return self.cmd("IDV")

    def lock_front_panel(self, locked=True):
        return self.cmd(f"LOCK-FP {1 if locked else 0}")

    def device_info(self):
        """NOTE: the VS-44HN exposes no network command at all (no NET-IP?).
        The IP cannot be queried over the protocol: use the rear RESET button."""
        return {q: self.cmd(q) for q in self.QUERIES}

    def help(self):
        return self.cmd("HELP", timeout=3.0)

    def to_protocol2000(self):
        return self.cmd("P2000")

    def reboot(self):
        return self.cmd("RESET")                # reboot, clears nothing

    def factory(self):
        """DESTRUCTIVE: wipes the whole configuration, presets and EDID included."""
        return self.cmd("FACTORY")


# --------------------------------------------------------------------------- #
# Auto-detect
# --------------------------------------------------------------------------- #

def detect_protocol(transport, machine=1):
    """Try Protocol 2000 first (factory default, well-defined instruction),
    then Protocol 3000. Avoids firing ASCII at a binary parser."""
    transport.flush_input()

    p2 = Protocol2000(transport, machine)
    frames = p2.ping()
    if frames and frames[0]["from_device"]:
        return p2, frames

    transport.flush_input()
    p3 = Protocol3000(transport)
    reply = p3.ping()
    if reply and ("~" in reply or "OK" in reply.upper()):
        return p3, reply

    return None, reply


# --------------------------------------------------------------------------- #
# Utilities
# --------------------------------------------------------------------------- #

def hexdump(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def printable(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


def parse_raw(text: str) -> bytes:
    """Accepts "01 82 83 81" (hex) or "#MODEL?" (ASCII, the CR is appended)."""
    t = text.strip()
    if t.startswith("#"):
        return t.encode() + b"\r"
    return bytes(int(x, 16) for x in t.replace(",", " ").split())


def discover(cidr, ports=DISCOVER_PORTS, timeout=0.4, workers=128):
    net = ipaddress.ip_network(cidr, strict=False)
    targets = [(ip, p) for ip in net.hosts() for p in ports]
    print(f"Scanning {len(targets)} host/port combinations on {list(ports)}...")

    def probe(target):
        ip, port = target
        s = socket.socket()
        s.settimeout(timeout)
        try:
            s.connect((str(ip), port))
            return f"{ip}:{port}"
        except OSError:
            return None
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        found = [r for r in ex.map(probe, targets) if r]

    if not found:
        print("Nothing found. The IP is probably on another subnet: use the rear RESET\n"
              "button (it only clears the IP parameters) and retry on 192.168.1.39.")
    for hit in found:
        print(f"  FOUND {hit}")
    return found


def list_serial_ports():
    try:
        from serial.tools import list_ports
    except ImportError:
        sys.exit("ERROR: pyserial is required ->  pip install pyserial")
    ports = list(list_ports.comports())
    if not ports:
        print("No serial port detected.")
    for p in ports:
        print(f"  {p.device:12s} {p.description}")


def shell(proto, transport):
    print(f"\nInteractive shell on {transport} ({proto.name}).")
    print("  <in> <out>    route an input to an output (out 0 = all)")
    print("  preset <n>    recall preset n")
    print("  store <n>     store the current layout into preset n")
    print("  status        read the routing")
    print("  raw <...>     hex bytes or a '#...' string")
    print("  quit\n")
    while True:
        try:
            line = input("kramer> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("q", "quit", "exit"):
            break
        parts = line.split()
        try:
            if parts[0] == "preset":
                print(proto.preset_recall(int(parts[1])))
            elif parts[0] == "store":
                print(proto.preset_store(int(parts[1])))
            elif parts[0] == "status":
                print(proto.status())
            elif parts[0] == "raw":
                transport.send(parse_raw(" ".join(parts[1:])))
                data = transport.recv(1.5)
                print(hexdump(data), "|", printable(data))
            elif len(parts) == 2 and all(p.isdigit() for p in parts):
                print(proto.switch(int(parts[0]), int(parts[1])))
            else:
                print("Unknown command.")
        except Exception as e:
            print(f"error: {e}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(
        description="Test tool for Kramer VS-44HN / VS-44H HDMI matrices.")

    p.add_argument("--tcp", metavar="HOST[:PORT]",
                   help=f"TCP connection (default port {DEFAULT_TCP_PORT}; "
                        "the VS-44HN also listens on 10001 and 50000)")
    p.add_argument("--serial", metavar="DEVICE", help="e.g. COM3 or /dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=BAUD,
                   help=f"serial baud rate (default {BAUD}, same for P2000 and P3000)")
    p.add_argument("--proto", choices=("auto", "p2000", "p3000"), default="auto")
    p.add_argument("--machine", type=int, default=1,
                   help="Protocol 2000 machine number (default 1)")
    p.add_argument("-v", "--verbose", action="store_true", help="show the TX/RX bytes")
    p.add_argument("--dry-run", action="store_true",
                   help="print the bytes without opening any connection")

    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe", help="detect the protocol and identify the device")
    sub.add_parser("status", help="read the current routing")
    sub.add_parser("device-info", help="model, firmware, serial number, counts")
    sub.add_parser("presets", help="list the defined presets")
    sub.add_parser("help-cmds", help="ask the device for its supported commands (P3000 only)")
    sub.add_parser("ports", help="list the available serial ports")
    sub.add_parser("shell", help="interactive shell")

    sw = sub.add_parser("switch", help="route an input to an output")
    sw.add_argument("input", type=int, help="1-4 (0 = disconnect)")
    sw.add_argument("output", type=int, help="1-4 (0 = all outputs)")

    for name, helptext in (("preset-recall", "recall a preset"),
                           ("preset-store", "store the current layout into a preset")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("n", type=int, help="preset number (1-8)")

    ps = sub.add_parser("proto-switch", help="change the protocol the device speaks")
    ps.add_argument("target", choices=("p2000", "p3000"))

    r = sub.add_parser("raw", help="send hex bytes or an ASCII '#...' string")
    r.add_argument("data", help='e.g. "01 82 83 81" or "#MODEL?"')

    d = sub.add_parser("discover", help="scan a subnet looking for the Kramer ports")
    d.add_argument("cidr", help="e.g. 192.168.1.0/24")
    d.add_argument("--port", type=int, action="append", dest="ports",
                   help="port to try (repeatable; default 5000, 10001, 50000)")

    return p


def open_transport(args):
    kw = dict(verbose=args.verbose, dry_run=args.dry_run)
    if args.tcp:
        host, _, port = args.tcp.partition(":")
        return TcpTransport(host, int(port) if port else DEFAULT_TCP_PORT, **kw)
    if args.serial:
        return SerialTransport(args.serial, args.baud, **kw)
    sys.exit("ERROR: specify either --tcp HOST or --serial DEVICE.")


def main():
    args = build_parser().parse_args()

    if args.cmd == "ports":
        return list_serial_ports()
    if args.cmd == "discover":
        return discover(args.cidr, tuple(args.ports) if args.ports else DISCOVER_PORTS)

    with open_transport(args) as t:
        print(f"Connected: {t}")

        if args.proto == "p2000":
            proto = Protocol2000(t, args.machine)
        elif args.proto == "p3000":
            proto = Protocol3000(t)
        else:
            proto, extra = detect_protocol(t, args.machine)
            if proto is None:
                print("No valid reply with either protocol.")
                print(f"  last data received: {extra!r}")
                print("Check: cable (2-2, 3-3, 5-5, NOT null-modem), 9600 8N1,")
                print("machine number, front panel not in LOCK, correct TCP port.")
                return 1
            print(f"Protocol detected: {proto.name}")

        is_p3 = isinstance(proto, Protocol3000)

        if args.cmd == "probe":
            if is_p3:
                for k, v in proto.device_info().items():
                    print(f"  {k:12s} {v}")
            else:
                print(f"  identify : {proto.ping()}")
                print(f"  version  : {proto.version()}")
                print(f"  counts   : {proto.io_count()}")
                print("  (Protocol 2000: no network query available)")
        elif args.cmd == "device-info":
            print(proto.device_info() if is_p3 else proto.io_count())
        elif args.cmd == "switch":
            print(proto.switch(args.input, args.output))
        elif args.cmd == "preset-recall":
            print(proto.preset_recall(args.n))
        elif args.cmd == "preset-store":
            print(proto.preset_store(args.n))
        elif args.cmd == "presets":
            if is_p3:
                print(proto.presets())
            else:
                print({n: proto.preset_defined(n) for n in range(1, 9)})
        elif args.cmd == "status":
            print(proto.status())
        elif args.cmd == "help-cmds":
            print(proto.help() if is_p3 else "Protocol 3000 only.")
        elif args.cmd == "proto-switch":
            if args.target == "p3000":
                if is_p3:
                    print("Already in Protocol 3000.")
                else:
                    print(hexdump(proto.to_protocol3000() or b""))
                    print("Switched to Protocol 3000. NOTE: the IR remote does not "
                          "work in this mode.")
            else:
                if is_p3:
                    print(proto.to_protocol2000())
                    print("Back in Protocol 2000. The IR remote works again.")
                else:
                    print("Already in Protocol 2000.")
        elif args.cmd == "raw":
            t.send(parse_raw(args.data))
            data = t.recv(1.5)
            print(hexdump(data), "|", printable(data))
        elif args.cmd == "shell":
            shell(proto, t)

    return 0


if __name__ == "__main__":
    sys.exit(main())
