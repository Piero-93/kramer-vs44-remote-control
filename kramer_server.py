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
kramer_server.py - HTTP API and web UI for Kramer VS-44HN HDMI matrices.

Requires kramer_vs44.py in the SAME directory: the protocol lives there. No
external dependency - the standard library serves both the API and the page.

    python kramer_server.py
    python kramer_server.py --matrix 192.168.1.50:10001 --port 8080

Then open http://<this-machine>:8000/ from any browser on the network.

Protocol 2000 over TCP only. That is the factory default, it keeps the IR remote
working, and it is the only mode where the matrix reports front-panel presses -
which is what lets this service push changes to the browser instead of polling.

RUN ONE CONTROLLER AT A TIME
  The matrix reports front-panel presses to every connected client, but NOT the
  commands issued by another client. So this service and the Tkinter GUI cannot
  see each other's switches: whichever you are not looking at will show stale
  routing. Use one or the other.

Endpoints
---------
  GET  /                      the web UI
  GET  /api/state             connection state and current routing
  GET  /api/labels            input, output and preset names
  PUT  /api/labels            update those names
  POST /api/route             {"input": n, "output": m}
  POST /api/preset/<n>/recall recall preset n, then re-read the routing
  GET  /api/events            Server-Sent Events: state changes as they happen
"""

import argparse
import json
import queue
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import kramer_vs44 as kv

N_IO = 4
N_PRESETS = 8

# Shared with the Tkinter GUI on purpose, so both show the same names.
CONFIG_PATH = Path(__file__).with_name("kramer_gui_config.json")

# Kept in step with kramer_gui.DEFAULT_CONFIG. Duplicated rather than imported
# because importing that module would pull in Tkinter, which a headless service
# has no business requiring.
DEFAULT_LABELS = {
    "inputs": [f"IN {i}" for i in range(1, N_IO + 1)],
    "outputs": [f"OUT {o}" for o in range(1, N_IO + 1)],
    "presets": [f"Preset {n}" for n in range(1, N_PRESETS + 1)],
}

WEB_ROOT = Path(__file__).with_name("web")
INDEX = WEB_ROOT / "index.html"

SSE_KEEPALIVE = 15.0            # seconds between SSE comment frames
SSE_BACKLOG = 32                # events buffered per browser before dropping


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# --------------------------------------------------------------------------- #
# Labels, shared with the Tkinter GUI
# --------------------------------------------------------------------------- #

def load_labels():
    data = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"unreadable config, using default labels: {e}")
    out = {}
    for key, defaults in DEFAULT_LABELS.items():
        vals = [str(v) for v in (data.get(key) or [])][:len(defaults)]
        vals += defaults[len(vals):]
        out[key] = vals
    return out


def save_labels(labels):
    """Read-modify-write: only the label keys are touched, everything else in
    the file is preserved. The Tkinter GUI writes to the same file, and blindly
    overwriting it would discard its settings."""
    data = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.update(labels)
    CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                           encoding="utf-8")


# --------------------------------------------------------------------------- #
# The device link: one thread owns the socket
# --------------------------------------------------------------------------- #

class DeviceLink:
    """Owns the transport and serialises every access to it.

    One thread and one queue, for the same reason the Tkinter GUI has them: the
    200 ms command interval is enforced by the Transport object, so concurrent
    HTTP handlers must never touch it directly. They hand a callable to call()
    and wait for the result.

    While no job is pending the thread listens, because the matrix reports
    front-panel presses only to a client that is connected and reading. That is
    also why the connection is held open permanently, and why dropping it has to
    be detected and repaired here rather than left to the user.
    """

    IDLE_POLL = 0.2                 # seconds spent listening between jobs
    RECONNECT_DELAY = 3.0           # pause before retrying a failed connection

    def __init__(self, host, port, machine=1, on_change=None):
        self.host, self.port, self.machine = host, port, machine
        self.on_change = on_change or (lambda: None)
        self.routing = {}           # {output: input}, 0 means disconnected
        self.connected = False
        self.detail = f"TCP {host}:{port}"
        self.error = None
        self._transport = None
        self._proto = None
        self._jobs = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="device-link")

    # ----- public API ----------------------------------------------------- #

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._close()

    def call(self, fn, timeout=10.0):
        """Run fn(proto) on the device thread and return its result.

        Raises ConnectionError when the link is down and whatever fn raised
        otherwise, so an HTTP handler can map it straight onto a status code."""
        if not self.connected:
            raise ConnectionError(self.error or "not connected to the matrix")
        box = queue.Queue(1)
        self._jobs.put((fn, box))
        try:
            ok, value = box.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"the matrix did not answer within {timeout:g} s")
        if not ok:
            raise value
        return value

    def snapshot(self):
        return {
            "connected": self.connected,
            "detail": self.detail,
            "protocol": self._proto.name if self._proto else None,
            "routing": {str(o): i for o, i in sorted(self.routing.items())},
            "error": self.error,
        }

    # ----- the thread ----------------------------------------------------- #

    def _run(self):
        while not self._stop.is_set():
            if not self._proto:
                self._connect()
                continue
            try:
                fn, box = self._jobs.get(timeout=self.IDLE_POLL)
            except queue.Empty:
                self._listen()
                continue
            try:
                box.put((True, fn(self._proto)))
            except OSError as e:
                # A socket-level failure means the link is gone, not that the
                # command was wrong: report it and start reconnecting.
                box.put((False, e))
                self._drop(e)
            except Exception as e:
                box.put((False, e))

    def _connect(self):
        try:
            transport = kv.TcpTransport(self.host, self.port)
            proto = kv.Protocol2000(transport, self.machine)
            proto.on_notify = self._notified
            # Prove the link before declaring it up: a TCP connect succeeding
            # says nothing about the device answering.
            if not proto.ping():
                transport.close()
                raise ConnectionError("connected, but the matrix did not answer "
                                      "Protocol 2000")
            self._transport, self._proto = transport, proto
            self.routing = proto.status()
            self.connected = True
            self.error = None
            log(f"connected to {self.detail} ({proto.name}), routing {self.routing}")
            self.on_change()
        except (OSError, ConnectionError) as e:
            self._close()
            self.error = str(e) or e.__class__.__name__
            log(f"connection to {self.detail} failed: {self.error}")
            self.on_change()
            self._stop.wait(self.RECONNECT_DELAY)

    def _listen(self):
        try:
            self._proto.poll_notifications(self.IDLE_POLL)
        except OSError as e:
            self._drop(e)

    def _notified(self, frames):
        """Called on this thread by Protocol2000 for frames the matrix sent by
        itself. Only SWITCH VIDEO carries routing; anything else is logged and
        ignored rather than guessed at."""
        changed = False
        for f in frames:
            if f["instr"] == 1 and f["from_device"]:
                if self.routing.get(f["output"]) != f["input"]:
                    self.routing[f["output"]] = f["input"]
                    changed = True
            else:
                log(f"unsolicited frame ignored: {f['raw']}")
        if changed:
            log(f"changed on the device: {self.routing}")
            self.on_change()

    def _drop(self, error):
        self._close()
        self.error = f"link lost: {error}"
        log(self.error + ", reconnecting")
        self.on_change()

    def _close(self):
        if self._transport:
            try:
                self._transport.close()
            except Exception:
                pass
        self._transport = None
        self._proto = None
        self.connected = False


# --------------------------------------------------------------------------- #
# Server-Sent Events
# --------------------------------------------------------------------------- #

class EventHub:
    """Fan-out to connected browsers. Each subscriber gets a bounded queue: a
    browser that stops reading must never be able to block the device thread,
    so its events are dropped instead of buffered without limit."""

    def __init__(self):
        self._subs = set()
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(SSE_BACKLOG)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs.discard(q)

    def publish(self, payload):
        data = json.dumps(payload)
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass

    def count(self):
        with self._lock:
            return len(self._subs)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

PRESET_RECALL = re.compile(r"^/api/preset/(\d+)/recall$")


class Handler(BaseHTTPRequestHandler):
    server_version = "kramer-vs44"
    protocol_version = "HTTP/1.1"

    # ----- plumbing -------------------------------------------------------- #

    def log_message(self, fmt, *args):
        # The SSE stream is one long request; logging it once is enough.
        if self.path != "/api/events":
            log(f"{self.address_string()} {fmt % args}")

    def _authorized(self):
        """Single gate for every request. There is no authentication yet, by
        choice: the service is meant for a trusted LAN. It exists so a token or
        a session cookie can be added here alone, without touching the routes.
        Set "token" in the config file to require ?token=... or an
        Authorization: Bearer header."""
        token = self.server.token
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and header[7:] == token:
            return True
        return f"token={token}" in (self.path.partition("?")[2] or "")

    def _send(self, code, body=b"", content_type="application/json",
              extra_headers=()):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in extra_headers:
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload).encode("utf-8"))

    def _error(self, code, message):
        self._json(code, {"error": message})

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            raise ValueError("empty request body")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError(f"malformed JSON: {e}")
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
        return payload

    # ----- routing --------------------------------------------------------- #

    def do_GET(self):
        if not self._authorized():
            return self._error(401, "a token is required")
        path = self.path.partition("?")[0]
        if path in ("/", "/index.html"):
            return self._serve_index()
        if path == "/api/state":
            return self._json(200, self.server.link.snapshot())
        if path == "/api/labels":
            return self._json(200, load_labels())
        if path == "/api/events":
            return self._serve_events()
        if path == "/favicon.ico":
            return self._send(204)
        self._error(404, f"no such resource: {path}")

    def do_PUT(self):
        if not self._authorized():
            return self._error(401, "a token is required")
        if self.path.partition("?")[0] != "/api/labels":
            return self._error(404, f"no such resource: {self.path}")
        try:
            payload = self._read_json()
            labels = self._validated_labels(payload)
        except ValueError as e:
            return self._error(400, str(e))
        save_labels(labels)
        # Read back the complete set rather than echoing the partial update: a
        # client replaces its whole label state with what arrives here, and
        # handing it half an object would leave it with holes.
        current = load_labels()
        log(f"labels updated by {self.address_string()}")
        self.server.hub.publish({"type": "labels", "labels": current})
        self._json(200, current)

    def do_POST(self):
        if not self._authorized():
            return self._error(401, "a token is required")
        path = self.path.partition("?")[0]
        if path == "/api/route":
            return self._do_route()
        recall = PRESET_RECALL.match(path)
        if recall:
            return self._do_preset_recall(int(recall.group(1)))
        self._error(404, f"no such resource: {path}")

    # ----- handlers -------------------------------------------------------- #

    def _serve_index(self):
        try:
            body = INDEX.read_bytes()
        except OSError:
            return self._error(500, f"{INDEX.name} is missing next to the script")
        self._send(200, body, "text/html; charset=utf-8")

    def _serve_events(self):
        """One long-lived response per browser. ThreadingHTTPServer gives this
        request its own thread, so holding it open costs a thread and nothing
        else."""
        link, hub = self.server.link, self.server.hub
        q = hub.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        log(f"{self.address_string()} subscribed to events "
            f"({hub.count()} listening)")
        try:
            # Send the current state at once: a browser that just connected must
            # not have to wait for something to change before it can draw.
            self._sse(json.dumps({"type": "state", "state": link.snapshot()}))
            while True:
                try:
                    self._sse(q.get(timeout=SSE_KEEPALIVE))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                                # the browser went away
        finally:
            hub.unsubscribe(q)
            log(f"{self.address_string()} stopped listening "
                f"({hub.count()} left)")

    def _sse(self, data):
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _do_route(self):
        try:
            payload = self._read_json()
            inp = self._validated_port(payload, "input")
            out = self._validated_port(payload, "output")
        except ValueError as e:
            return self._error(400, str(e))

        link = self.server.link
        try:
            link.call(lambda proto: proto.switch(inp, out))
        except ConnectionError as e:
            return self._error(503, str(e))
        except (TimeoutError, OSError) as e:
            return self._error(504, str(e))

        # out 0 means every output; the reply frame reports 0 there, so trust
        # what was commanded rather than trying to read it back out of it.
        if out == 0:
            link.routing = {o: inp for o in range(1, N_IO + 1)}
        else:
            link.routing[out] = inp
        log(f"routed input {inp} to output {out} for {self.address_string()}")
        self.server.publish_state()
        self._json(200, link.snapshot())

    def _do_preset_recall(self, n):
        if not 1 <= n <= N_PRESETS:
            return self._error(400, f"preset must be between 1 and {N_PRESETS}")
        link = self.server.link

        def job(proto):
            proto.preset_recall(n)
            # A preset changes an unknown number of outputs, so the routing has
            # to be read back. Done inside the same job to stay serialised.
            return proto.status()

        try:
            link.routing = link.call(job, timeout=15.0)
        except ConnectionError as e:
            return self._error(503, str(e))
        except (TimeoutError, OSError) as e:
            return self._error(504, str(e))
        log(f"recalled preset {n} for {self.address_string()}, "
            f"routing {link.routing}")
        self.server.publish_state()
        self._json(200, link.snapshot())

    # ----- validation ------------------------------------------------------ #

    @staticmethod
    def _validated_port(payload, key):
        if key not in payload:
            raise ValueError(f'missing "{key}"')
        value = payload[key]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f'"{key}" must be an integer')
        if not 0 <= value <= N_IO:
            raise ValueError(f'"{key}" must be between 0 and {N_IO}')
        return value

    @staticmethod
    def _validated_labels(payload):
        labels = {}
        for key, defaults in DEFAULT_LABELS.items():
            if key not in payload:
                continue
            values = payload[key]
            if not isinstance(values, list) or len(values) != len(defaults):
                raise ValueError(f'"{key}" must be a list of {len(defaults)} names')
            cleaned = []
            for v in values:
                if not isinstance(v, str):
                    raise ValueError(f'"{key}" must contain strings only')
                # A name is shown, never parsed: trim it and cap the length so a
                # client cannot grow the config file without bound.
                cleaned.append(v.strip()[:40])
            labels[key] = cleaned
        if not labels:
            raise ValueError("nothing to update: expected inputs, outputs "
                             "or presets")
        return labels


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, link, token=None):
        super().__init__(address, Handler)
        self.link = link
        self.hub = EventHub()
        self.token = token

    def publish_state(self):
        self.hub.publish({"type": "state", "state": self.link.snapshot()})


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(
        description="HTTP API and web UI for Kramer VS-44HN HDMI matrices.")
    ap.add_argument("--matrix", default="192.168.1.39", metavar="HOST[:PORT]",
                    help="matrix address (default 192.168.1.39, TCP port "
                         f"{kv.DEFAULT_TCP_PORT})")
    ap.add_argument("--machine", type=int, default=1,
                    help="Protocol 2000 machine number (default 1)")
    ap.add_argument("--host", default="0.0.0.0",
                    help="address to listen on (default 0.0.0.0, the whole LAN; "
                         "use 127.0.0.1 to keep it on this machine)")
    ap.add_argument("--port", type=int, default=8000, help="HTTP port (default 8000)")
    ap.add_argument("--token", help="require this token on every request "
                                    "(Authorization: Bearer, or ?token=)")
    args = ap.parse_args()

    host, _, port = args.matrix.partition(":")
    link = DeviceLink(host, int(port) if port else kv.DEFAULT_TCP_PORT,
                      args.machine)
    server = Server((args.host, args.port), link, args.token)
    link.on_change = server.publish_state

    if not INDEX.exists():
        log(f"WARNING: {INDEX} is missing, the API works but / will fail")
    link.start()

    shown = args.host if args.host != "0.0.0.0" else _local_address()
    log(f"serving http://{shown}:{args.port}/  (matrix {link.detail})")
    if not args.token:
        log("no token set: anyone on this network can switch the matrix")
    log("run only one controller at a time - see the README")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopping")
    finally:
        server.shutdown()
        server.server_close()
        link.stop()
    return 0


def _local_address():
    """Best-effort LAN address, only to print a URL worth clicking."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 1))             # reserved, never routed
        return s.getsockname()[0]
    except OSError:
        return "localhost"
    finally:
        s.close()


if __name__ == "__main__":
    raise SystemExit(main())
