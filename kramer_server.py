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
  POST /api/preset/<n>/store  overwrite preset n with the current routing;
                              refused with 403 unless --allow-preset-store
  POST /api/preset/<n>/delete empty preset n; behind the same flag as store
  POST /api/lock              {"locked": true|false} for the front panel;
                              locking is refused with 403 unless
                              --allow-panel-lock, unlocking never is
  GET  /api/events            Server-Sent Events: state changes as they happen
"""

import argparse
import json
import os
import queue
import re
import signal
import socket
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import kramer_paths as kp
import kramer_vs44 as kv

N_IO = 4
N_PRESETS = 8

# Protocol 2000 instructions that arrive unprompted from a VS-44HN when someone
# uses the front panel. Measured, all three: a switch carries the routing, a
# store carries the slot it filled (OUTPUT 0 = stored, 1 = deleted), and a
# recall names the slot but says nothing about what it just changed.
P2000_SWITCH_VIDEO = 1
P2000_STORE_PRESET = 3
P2000_RECALL_PRESET = 4

# Shared with the Tkinter GUI on purpose, so both show the same names. Resolved
# here so the module is usable without main(), and reassigned in main() once
# --config has been parsed. It stays a module global because that is what the
# test suites substitute.
CONFIG_PATH = kp.config_path()

# Kept in step with kramer_gui.DEFAULT_CONFIG. Duplicated rather than imported
# because importing that module would pull in Tkinter, which a headless service
# has no business requiring.
DEFAULT_LABELS = {
    "inputs": [f"IN {i}" for i in range(1, N_IO + 1)],
    "outputs": [f"OUT {o}" for o in range(1, N_IO + 1)],
    "presets": [f"Preset {n}" for n in range(1, N_PRESETS + 1)],
}

INDEX = kp.resource_path("web", "index.html")

SSE_KEEPALIVE = 15.0            # seconds between SSE comment frames
SSE_BACKLOG = 32                # events buffered per browser before dropping


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# --------------------------------------------------------------------------- #
# Labels, shared with the Tkinter GUI
# --------------------------------------------------------------------------- #

def load_labels():
    data = kp.read_json(CONFIG_PATH,
                        on_error=lambda e: log(f"unreadable config, using "
                                               f"default labels: {e}"))
    out = {}
    for key, defaults in DEFAULT_LABELS.items():
        vals = [str(v) for v in (data.get(key) or [])][:len(defaults)]
        vals += defaults[len(vals):]
        out[key] = vals
    return out


def save_labels(labels):
    """Only the label keys are touched: the Tkinter GUI writes to this same file
    and owns other keys in it. The write is atomic - see kramer_paths.merge_json.

    Raises OSError when the location cannot be written, which is a real
    possibility once this runs in a container with a read-only volume. The
    caller reports it; it must not be swallowed, because a rename that
    evaporates on restart is worse than an error."""
    kp.merge_json(CONFIG_PATH, labels)


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
    RECONNECT_DELAY = kv.RECONNECT_DELAY
    HEARTBEAT = kv.HEARTBEAT

    def __init__(self, host, port, machine=1, on_change=None, heartbeat=None):
        self.host, self.port, self.machine = host, port, machine
        # When to probe and whether it answered lives in kramer_vs44, shared with
        # the GUI: it is the same decision, and two copies of it would drift.
        self.monitor = kv.LinkMonitor(heartbeat)
        self.on_change = on_change or (lambda: None)
        self.routing = {}           # {output: input}, 0 means disconnected
        self.presets = {}           # {slot: bool}, True when the slot holds a layout
        # None means "not known", which is not the same as False. Only ever set
        # from a reply, so a page can show "unknown" instead of claiming the
        # buttons on the machine work when nobody has asked.
        self.locked = None
        # The slot whose layout the routing currently is, or None for "not
        # known". Only ever set from something observed - a recall, a store -
        # and cleared by the first switch that moves away from it. It is never
        # inferred by comparing the routing against remembered contents, because
        # the device cannot be asked what a preset holds.
        self.active_preset = None
        self.connected = False
        self.detail = f"TCP {host}:{port}"
        self.error = None
        self._transport = None
        self._proto = None
        self._jobs = queue.Queue()
        # The slot the front panel recalled, cleared once the routing has been
        # read again. See _maybe_resync.
        self._resync = None
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

    def store_preset(self, n):
        """Store the current routing into slot n, then refresh the occupancy map
        so the UI can keep marking which slots hold a layout."""
        def job(proto):
            proto.preset_store(n)
            return self._read_presets(proto)

        self.presets = self.call(job, timeout=15.0)

    def delete_preset(self, n):
        """Empty slot n, then re-read the occupancy map for the same reason
        store_preset does: the marks in the UI are the only warning before an
        overwrite, so they have to follow the change that just happened."""
        def job(proto):
            proto.preset_delete(n)
            return self._read_presets(proto)

        self.presets = self.call(job, timeout=15.0)

    def set_lock(self, locked):
        """Lock or unlock the front panel and return what the device then says.

        The read-back is the point. An acknowledgement that the command was sent
        does not tell a browser in another room whether the buttons on the
        machine currently work, and that is the only question this answers."""
        def job(proto):
            proto.lock_front_panel(locked)
            return proto.is_locked()

        self.locked = self.call(job)
        return self.locked

    def snapshot(self):
        return {
            "connected": self.connected,
            "detail": self.detail,
            "protocol": self._proto.name if self._proto else None,
            "routing": {str(o): i for o, i in sorted(self.routing.items())},
            "presets": {str(n): v for n, v in sorted(self.presets.items())},
            "active_preset": self.active_preset,
            "locked": self.locked,
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
                self._maybe_resync()
                self._maybe_beat()
                continue
            try:
                # No mark_ok here on purpose: whether the device answered is
                # recorded by the transport as bytes arrive. A job that returned
                # without a reply is not evidence of anything.
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
            # Whatever was active before this connection is not knowledge this
            # one has: the routing may have been changed while nobody was
            # listening, and no query returns the preset that produced it.
            self.active_preset = None
            # Eight more commands, so it is read here and after a store, never
            # per request. Knowing which slots are occupied is what lets the UI
            # warn before overwriting one.
            self.presets = self._read_presets(proto)
            # One command. Worth it on every connect rather than per request:
            # a panel locked by an earlier session looks exactly like a broken
            # machine to whoever is standing at it, and this is the only place
            # that can say otherwise.
            self.locked = proto.is_locked()
            self.connected = True
            self.error = None
            self.monitor.mark_ok()
            log(f"connected to {self.detail} ({proto.name}), routing {self.routing}, "
                f"presets defined {sorted(n for n, v in self.presets.items() if v)}"
                + (", front panel LOCKED" if self.locked else ""))
            self.on_change()
        except (OSError, ConnectionError) as e:
            self._close()
            self.error = str(e) or e.__class__.__name__
            # Retries are fast on purpose, so the same failure would otherwise
            # fill the log with thousands of identical lines overnight. Say it
            # once; the reconnection is logged when it happens.
            if self.monitor.first_time(self.error):
                log(f"connection to {self.detail} failed: {self.error} "
                    f"(retrying every {self.RECONNECT_DELAY:g}s, silently)")
            self.on_change()
            self._stop.wait(self.RECONNECT_DELAY)

    @staticmethod
    def _read_presets(proto):
        """Instruction 15 per slot: does this preset hold a layout?"""
        return {n: bool(proto.preset_defined(n)) for n in range(1, N_PRESETS + 1)}

    def _listen(self):
        try:
            # Anything that arrives updates the transport's own last_rx, so the
            # liveness timer needs no bookkeeping here.
            self._proto.poll_notifications(self.IDLE_POLL)
        except OSError as e:
            # Includes the ConnectionError raised when the matrix closes the
            # socket, which is the only drop that can be noticed passively.
            self._drop(e)

    def _maybe_resync(self):
        """Re-read the routing after the front panel recalled a preset.

        Measured on a VS-44HN: a recall is announced as its own frame, but the
        switches it performs are not transmitted at all. Following the
        announcements alone therefore leaves the routing showing whatever it was
        before the recall, which is worse than showing nothing. Reading it back
        is the only way to learn what changed.

        It runs here, between jobs, because _notified is called from inside the
        read loop of the command in flight and cannot issue one of its own."""
        if not self._resync or not self._proto:
            return
        slot, self._resync = self._resync, None
        try:
            self.routing = self._proto.status()
        except OSError as e:
            self._drop(e)
            return
        # Set after the read, not before: what was just read is by definition
        # what that slot holds, and a read that failed must not leave the UI
        # claiming a preset is in effect.
        self.active_preset = slot
        log(f"routing re-read after the recall: {self.routing}")
        self.on_change()

    def _maybe_beat(self):
        """Probe the link after a stretch of silence.

        A matrix switched off without closing its socket - a power cut, a pulled
        cable - leaves a connection that looks perfectly healthy from this side:
        reads simply time out, exactly as they do when the device is idle and has
        nothing to say. Silence is therefore not evidence of anything, and has to
        be probed.

        The decision and its two traps live in kramer_vs44.LinkMonitor, shared
        with the GUI."""
        if not self.monitor.due(self._transport):
            return
        try:
            reason = self.monitor.beat(self._proto)
        except OSError as e:
            self._drop(e)
            return
        if reason:
            self._drop(reason)

    def _notified(self, frames):
        """Called on this thread by Protocol2000 for frames the matrix sent by
        itself. SWITCH VIDEO carries routing, STORE PRESET carries occupancy,
        and RECALL PRESET carries neither - only the slot - so it schedules a
        re-read instead. Anything else is logged and ignored rather than guessed
        at.

        Nothing here reads from the device: this runs on the worker thread
        inside the read loop of whatever command is in flight, so a command
        issued here would interleave with that command's own reply."""
        routed = presets = False
        for f in frames:
            if not f["from_device"]:
                log(f"unsolicited frame ignored: {f['raw']}")
                continue
            if f["instr"] == P2000_SWITCH_VIDEO:
                if self.routing.get(f["output"]) != f["input"]:
                    self.routing[f["output"]] = f["input"]
                    routed = True
                    # A recall never announces the switches it performs, so an
                    # unprompted one is always somebody moving away from what
                    # the preset laid out.
                    self.active_preset = None
            elif (f["instr"] == P2000_STORE_PRESET and f["output"] in (0, 1)
                    and 1 <= f["input"] <= N_PRESETS):
                defined = f["output"] == 0
                log(f"preset {f['input']} "
                    f"{'stored' if defined else 'deleted'} on the device")
                if defined:
                    # It now holds exactly what is routed, so it is active by
                    # construction. A delete leaves the routing alone, but the
                    # slot it came from no longer exists.
                    self.active_preset = f["input"]
                elif self.active_preset == f["input"]:
                    self.active_preset = None
                if self.presets.get(f["input"]) != defined:
                    self.presets[f["input"]] = defined
                    presets = True
            elif (f["instr"] == P2000_RECALL_PRESET
                    and 1 <= f["input"] <= N_PRESETS):
                log(f"preset {f['input']} recalled on the device")
                self._resync = f["input"]
            elif f["instr"] == kv.P2000_ERROR:
                # Not unsolicited at all: the device refused the command that
                # was in flight. Saying "ignored" here sent whoever read the log
                # looking for a front-panel press that never happened.
                log(f"the matrix refused a command: {f['raw']}")
            else:
                log(f"unsolicited frame ignored: {f['raw']}")
        if routed:
            log(f"changed on the device: {self.routing}")
        if routed or presets:
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
PRESET_STORE = re.compile(r"^/api/preset/(\d+)/store$")
PRESET_DELETE = re.compile(r"^/api/preset/(\d+)/delete$")
TOKEN_IN_QUERY = re.compile(r"token=[^&\s]*")
SESSION_COOKIE = "kramer_token"
SESSION_MAX_AGE = 30 * 24 * 3600   # seconds a browser keeps the cookie



class Handler(BaseHTTPRequestHandler):
    server_version = "kramer-vs44"
    protocol_version = "HTTP/1.1"

    # ----- plumbing -------------------------------------------------------- #

    def address_string(self):
        """Behind a reverse proxy every request arrives from the proxy, so the
        log would name it and nothing else - one address for the whole house.
        With --trust-proxy the first entry of X-Forwarded-For is logged instead:
        the client as the proxy saw it.

        Off by default because that header is written by whoever sends the
        request. Nothing here decides anything from the address, so a lie costs
        a misleading log line and no more - but it is still a lie, so only turn
        this on when the service is reachable through the proxy alone."""
        if self.server.trust_proxy:
            forwarded = self.headers.get("X-Forwarded-For", "")
            first = forwarded.partition(",")[0].strip()
            if first:
                return first
        return super().address_string()

    def log_message(self, fmt, *args):
        # The SSE stream is one long request; logging it once is enough.
        if self.path == "/api/events":
            return
        # The request line carries the query string, so a ?token=... request
        # would otherwise write the token into the log - and in a container
        # those logs get collected.
        log(f"{self.address_string()} {TOKEN_IN_QUERY.sub('token=***', fmt % args)}")

    def _authorized(self):
        """Single gate for every request. There is no authentication by default,
        by choice: the service is meant for a trusted LAN. It exists so a token
        or a session cookie can be added here alone, without touching the routes.
        Pass --token, or set KRAMER_TOKEN, to require ?token=..., an
        Authorization: Bearer header, or the cookie the page itself is given."""
        token = self.server.token
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and header[7:] == token:
            return True
        if self._cookie_token() == token:
            return True
        return f"token={token}" in (self.path.partition("?")[2] or "")

    def _cookie_token(self):
        """A header the browser mangled parses to nothing rather than raising,
        so it arrives here as a request carrying no usable token - which the
        gate above refuses exactly as it refuses one carrying none at all."""
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = jar.get(SESSION_COOKIE)
        return morsel.value if morsel else None

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
            return self._serve_index(path)
        if path == "/api/state":
            return self._json(200, self.server.state_payload())
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
        try:
            save_labels(labels)
        except OSError as e:
            # 500, not 503: in this API 503 already means "the matrix is not
            # connected", and reusing it here would make the message in the page
            # actively misleading. A config location that cannot be written is
            # the same class of fault as a missing index.html - installed wrong -
            # which already answers 500. Nothing else degrades: routing, presets
            # and the event stream never touch this file, so the result is a
            # fully working controller that cannot rename things.
            log(f"cannot save the names to {CONFIG_PATH}: {e}")
            return self._error(500, f"cannot save the names: {e} ({CONFIG_PATH})")
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
        if path == "/api/lock":
            return self._do_lock()
        recall = PRESET_RECALL.match(path)
        if recall:
            return self._do_preset_recall(int(recall.group(1)))
        store = PRESET_STORE.match(path)
        if store:
            return self._do_preset_store(int(store.group(1)))
        delete = PRESET_DELETE.match(path)
        if delete:
            return self._do_preset_delete(int(delete.group(1)))
        self._error(404, f"no such resource: {path}")

    # ----- handlers -------------------------------------------------------- #

    def _serve_index(self, path):
        """A token in the address is answered with a redirect to the same page
        without it. Left there it goes into the history, into a bookmark and
        into the next screenshot; the cookie set on the way survives, because a
        browser keeps cookies from a 303 like from any other answer.

        The Location is relative for the same reason every path in the page is:
        behind a reverse proxy this service is asked for / while the browser is
        at /kramer-controller/, and an absolute Location would send it to the
        root of the host instead. A browser that refuses the cookie does not
        loop - the address it lands on carries no token, so it gets a plain 401
        and says so."""
        cookie = self._session_cookie()
        if cookie and "token=" in (self.path.partition("?")[2] or ""):
            here = path.rpartition("/")[2] or "./"
            return self._send(303, extra_headers=cookie + (("Location", here),))
        try:
            body = INDEX.read_bytes()
        except OSError:
            return self._error(500, f"{INDEX.name} is missing next to the script")
        self._send(200, body, "text/html; charset=utf-8", cookie)

    def _session_cookie(self):
        """The address of the page can carry a token - ?token=... - but nothing
        the page then does can. fetch() could send an Authorization header,
        EventSource cannot send one at all, and appending the token to every URL
        writes it into the browser history and into any log the answer passes.
        So the one request that does arrive with a token is handed a cookie, and
        every request after it is let through by that - see _serve_index, which
        then redirects the token out of the address bar.

        No Path attribute, deliberately: the browser then scopes the cookie to
        the directory it asked for, which behind a reverse proxy is the mount
        point - /kramer-controller/ - rather than the whole host. Path=/ would
        hand this token to every other app served under the same name. No
        Secure either: this speaks plain HTTP on a LAN, by the same choice that
        makes the token optional in the first place."""
        if not self.server.token:
            return ()
        return (("Set-Cookie",
                 f"{SESSION_COOKIE}={self.server.token}; "
                 f"Max-Age={SESSION_MAX_AGE}; HttpOnly; SameSite=Lax"),)

    def _serve_events(self):
        """One long-lived response per browser. ThreadingHTTPServer gives this
        request its own thread, so holding it open costs a thread and nothing
        else."""
        hub = self.server.hub
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
            self._sse(json.dumps({"type": "state",
                                  "state": self.server.state_payload()}))
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
            # The stream is delimited by the connection closing, so there is no
            # next request on it. Saying so keeps the server from trying to read
            # one and printing a traceback for a socket the browser abandoned.
            self.close_connection = True
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
        # Whatever preset was in effect no longer is, even if this switch put
        # back exactly what it had: the service cannot read a preset to find
        # out, so the honest answer here is "not known" rather than a guess.
        link.active_preset = None
        log(f"routed input {inp} to output {out} for {self.address_string()}")
        self.server.publish_state()
        self._json(200, self.server.state_payload())

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
        link.active_preset = n
        log(f"recalled preset {n} for {self.address_string()}, "
            f"routing {link.routing}")
        self.server.publish_state()
        self._json(200, self.server.state_payload())

    def _do_lock(self):
        """POST /api/lock with {"locked": true|false}.

        Locking is gated by --allow-panel-lock; unlocking never is. That
        asymmetry is deliberate and is the whole safety story of this endpoint:
        the failure worth designing against is a panel locked from a browser by
        someone who then walks away, leaving whoever is at the machine with dead
        buttons. Releasing it must never depend on how the service was started."""
        try:
            payload = self._read_json()
            locked = payload.get("locked")
        except ValueError as e:
            return self._error(400, str(e))
        if not isinstance(locked, bool):
            return self._error(400, 'expected {"locked": true} or {"locked": false}')
        if locked and not self.server.allow_panel_lock:
            # Name the environment variable too: in a container there is no shell
            # to restart the service in, and the remedy is a redeploy.
            return self._error(403, "locking the front panel is disabled: restart "
                                    "the service with --allow-panel-lock, or set "
                                    "KRAMER_ALLOW_PANEL_LOCK=1. Unlocking is "
                                    "always allowed")
        try:
            now = self.server.link.set_lock(locked)
        except ConnectionError as e:
            return self._error(503, str(e))
        except (TimeoutError, OSError) as e:
            return self._error(504, str(e))
        log(f"front panel {'locked' if locked else 'unlocked'} by "
            f"{self.address_string()} (device now reports "
            f"{'locked' if now else 'unlocked' if now is not None else 'unknown'})")
        self.server.publish_state()
        self._json(200, self.server.state_payload())

    def _do_preset_store(self, n):
        """The only destructive operation exposed, so it is off unless the
        service was started with --allow-preset-store. The confirmation in the
        page is a courtesy; this is the actual gate."""
        if not self.server.allow_preset_changes:
            # Name the environment variable too: in a container there is no shell
            # to restart the service in, and the remedy is a redeploy.
            return self._error(403, "changing presets is disabled: restart the "
                                    "service with --allow-preset-changes, or "
                                    "set KRAMER_ALLOW_PRESET_CHANGES=1")
        if not 1 <= n <= N_PRESETS:
            return self._error(400, f"preset must be between 1 and {N_PRESETS}")
        link = self.server.link
        was_defined = link.presets.get(n)
        try:
            link.store_preset(n)
        except ConnectionError as e:
            return self._error(503, str(e))
        except (TimeoutError, OSError) as e:
            return self._error(504, str(e))
        # The slot now holds exactly what is routed, so it is in effect.
        link.active_preset = n
        log(f"stored preset {n} for {self.address_string()} "
            f"({'overwritten' if was_defined else 'was empty'}), "
            f"routing {link.routing}")
        self.server.publish_state()
        self._json(200, self.server.state_payload())

    def _do_preset_delete(self, n):
        """Behind the same flag as storing, and deliberately not its own.

        The flag answers one question - may this service change what the
        hardware holds in its presets - and emptying a slot is that same change,
        not a milder one. A separate switch would let a service be configured to
        refuse an overwrite while permitting a wipe, which is a distinction
        nobody wants to have made by accident."""
        if not self.server.allow_preset_changes:
            # Name the environment variable too: in a container there is no shell
            # to restart the service in, and the remedy is a redeploy.
            return self._error(403, "changing presets is disabled: restart the "
                                    "service with --allow-preset-changes, or "
                                    "set KRAMER_ALLOW_PRESET_CHANGES=1")
        if not 1 <= n <= N_PRESETS:
            return self._error(400, f"preset must be between 1 and {N_PRESETS}")
        link = self.server.link
        was_defined = link.presets.get(n)
        try:
            link.delete_preset(n)
        except ConnectionError as e:
            return self._error(503, str(e))
        except (TimeoutError, OSError) as e:
            return self._error(504, str(e))
        # The routing is untouched by a delete, but the slot that explains it
        # is gone, so there is nothing left to point at.
        if link.active_preset == n:
            link.active_preset = None
        log(f"deleted preset {n} for {self.address_string()} "
            f"({'held a layout' if was_defined else 'was already empty'})")
        self.server.publish_state()
        self._json(200, self.server.state_payload())

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

    def __init__(self, address, link, token=None, allow_preset_changes=False,
                 allow_panel_lock=False, trust_proxy=False):
        super().__init__(address, Handler)
        self.link = link
        self.hub = EventHub()
        self.token = token
        self.allow_preset_changes = allow_preset_changes
        self.allow_panel_lock = allow_panel_lock
        self.trust_proxy = trust_proxy

    def handle_error(self, request, client_address):
        """A client that goes away is not an error, and must not be a traceback.

        The default prints twenty lines to stderr, and this container's own
        health check triggers it every thirty seconds: it reads the answer and
        exits without closing, so the socket is reset rather than closed and the
        next read on that keep-alive connection fails. Measured on the NAS,
        minutes after a restart: two stack traces out of three probes, and forty
        of the fifty-one log lines were them.

        That matters because the log is the only diagnostic a headless service
        has. Left alone it grows about 2 MB a day of nothing, and the lines
        worth having - what it connected to, what the front panel did - rotate
        out inside a fortnight. Anything that is not a peer disappearing still
        gets the full traceback, because that would be a real bug."""
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError,
                              ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def state_payload(self):
        """Device state plus what this service permits, so the page can hide a
        function it is not allowed to use instead of offering a dead button."""
        return {**self.link.snapshot(),
                "allow_preset_changes": self.allow_preset_changes,
                # Deprecated alias, kept so a page loaded before an upgrade goes
                # on working until it is reloaded. Remove it once nothing reads
                # it - the page prefers the name above.
                "allow_preset_store": self.allow_preset_changes,
                "allow_panel_lock": self.allow_panel_lock}

    def publish_state(self):
        self.hub.publish({"type": "state", "state": self.state_payload()})


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def build_parser():
    """Separate from main() so the option handling can be tested without
    starting a server.

    Every option falls back to an environment variable, which makes a given flag
    win with no precedence logic to get wrong: the environment only supplies the
    default. Numeric defaults are left as strings on purpose, because argparse
    applies its own `type` to a string default - so KRAMER_PORT=abc produces the
    ordinary "invalid int value" exit rather than a crash further in."""
    ap = argparse.ArgumentParser(
        description="HTTP API and web UI for Kramer VS-44HN HDMI matrices.")
    ap.add_argument("--matrix", metavar="HOST[:PORT]",
                    default=kp.env_default("KRAMER_MATRIX", "192.168.1.39"),
                    help="matrix address (env KRAMER_MATRIX, default "
                         f"192.168.1.39, TCP port {kv.DEFAULT_TCP_PORT})")
    ap.add_argument("--machine", type=int,
                    default=kp.env_default("KRAMER_MACHINE", "1"),
                    help="Protocol 2000 machine number (env KRAMER_MACHINE, "
                         "default 1)")
    ap.add_argument("--host", default=kp.env_default("KRAMER_HOST", "0.0.0.0"),
                    help="address THIS SERVICE listens on, not the matrix (env "
                         "KRAMER_HOST, default 0.0.0.0, the whole LAN; use "
                         "127.0.0.1 to keep it on this machine)")
    ap.add_argument("--port", type=int, default=kp.env_default("KRAMER_PORT", "8000"),
                    help="HTTP port for this service (env KRAMER_PORT, default 8000)")
    ap.add_argument("--token", default=kp.env_default("KRAMER_TOKEN"),
                    help="require this token on every request (env KRAMER_TOKEN; "
                         "Authorization: Bearer, or ?token= - which the page "
                         "then keeps in a cookie)")
    ap.add_argument("--trust-proxy", action="store_true",
                    default=kp.env_flag("KRAMER_TRUST_PROXY"),
                    help="log the client address from X-Forwarded-For instead of "
                         "the connecting one (env KRAMER_TRUST_PROXY=1). Only "
                         "behind a reverse proxy: any client that can reach this "
                         "service directly can put what it likes in that header")
    ap.add_argument("--allow-preset-changes", action="store_true",
                    default=kp.env_flag("KRAMER_ALLOW_PRESET_CHANGES"),
                    help="allow changing the hardware presets - overwriting one "
                         "and emptying one - off by default because they are the "
                         "only destructive operations here (env "
                         "KRAMER_ALLOW_PRESET_CHANGES=1). Note the flag can only "
                         "turn this on: clear the variable to turn it off")
    # Deprecated spelling, kept working because it is what is written in the
    # compose files and TrueNAS app definitions already deployed. It only ever
    # covered storing by name; it has gated emptying a slot since that existed,
    # which is exactly why the honest name was added next to it.
    ap.add_argument("--allow-preset-store", action="store_true",
                    default=kp.env_flag("KRAMER_ALLOW_PRESET_STORE"),
                    help="deprecated alias for --allow-preset-changes (env "
                         "KRAMER_ALLOW_PRESET_STORE=1); still honoured, and it "
                         "permits emptying a preset as well as overwriting one")
    ap.add_argument("--allow-panel-lock", action="store_true",
                    default=kp.env_flag("KRAMER_ALLOW_PANEL_LOCK"),
                    help="allow locking the front panel; off by default because "
                         "it disables the buttons on the machine itself (env "
                         "KRAMER_ALLOW_PANEL_LOCK=1). Unlocking is always "
                         "allowed, so a panel left locked can be released "
                         "whatever this is set to")
    ap.add_argument("--heartbeat", type=float,
                    default=kp.env_default("KRAMER_HEARTBEAT",
                                           str(DeviceLink.HEARTBEAT)),
                    metavar="SECONDS",
                    help=f"probe the matrix after this much silence (env "
                         f"KRAMER_HEARTBEAT, default {DeviceLink.HEARTBEAT:g}; "
                         f"0 disables the check)")
    kp.add_common_arguments(ap)
    return ap


def preset_changes_allowed(args):
    """Either spelling turns it on, and neither can turn it off.

    Both are store_true, so the only way to withdraw the permission is to stop
    passing them - which is why this is an or rather than a precedence rule with
    a winner. A rule with a winner would let the deprecated name silently cancel
    the current one, and the deployments that still use it are exactly the ones
    nobody is watching."""
    return bool(args.allow_preset_changes or args.allow_preset_store)


def main():
    global CONFIG_PATH
    args = build_parser().parse_args()
    CONFIG_PATH = kp.config_path(args.config)

    allow_preset_changes = preset_changes_allowed(args)

    host, _, port = args.matrix.partition(":")
    link = DeviceLink(host, int(port) if port else kv.DEFAULT_TCP_PORT,
                      args.machine, heartbeat=args.heartbeat)
    try:
        server = Server((args.host, args.port), link, args.token,
                        allow_preset_changes, args.allow_panel_lock,
                        args.trust_proxy)
    except OSError as e:
        # Almost always the port being taken. A traceback here tells the reader
        # nothing they can act on, and this is the first thing that goes wrong.
        log(f"cannot listen on {args.host}:{args.port} - {e}")
        log("another copy may already be running; try --port with another number")
        return 1
    link.on_change = server.publish_state

    if not INDEX.exists():
        log(f"WARNING: {INDEX} is missing, the API works but / will fail")
    link.start()

    log(f"kramer-vs44 {kp.VERSION}")
    if in_container():
        # A container's own address is not reachable from the network, so
        # printing a URL built from it would be a confident lie. The host decides
        # what the address is, through its published port.
        log(f"listening on {args.host}:{args.port} - reach it at the host's "
            f"address and published port  (matrix {link.detail})")
    else:
        shown = args.host if args.host != "0.0.0.0" else _local_address()
        log(f"serving http://{shown}:{args.port}/  (matrix {link.detail})")
    # Naming the resolved file turns every future "where did my names go" into a
    # line someone can read, instead of an investigation.
    log(f"settings file: {CONFIG_PATH}")
    if not _config_writable():
        log(f"WARNING: {CONFIG_PATH.parent} is not writable, so names cannot be "
            "changed from the browser. Everything else works. In a container, "
            "check the ownership of the mounted directory")
    if not args.token:
        log("no token set: anyone on this network can switch the matrix")
    if args.allow_preset_store:
        log("--allow-preset-store / KRAMER_ALLOW_PRESET_STORE is deprecated: "
            "use --allow-preset-changes / KRAMER_ALLOW_PRESET_CHANGES. The old "
            "name still works and means the same thing")
    if allow_preset_changes:
        log("preset changes are ENABLED: the hardware presets can be "
            "overwritten and emptied")
    if args.allow_panel_lock:
        log("front-panel locking is ENABLED: the buttons on the machine can be "
            "disabled from the browser")
    if not args.heartbeat:
        log("liveness checking is disabled: a matrix switched off silently will "
            "still be reported as connected")
    log("run only one controller at a time - see the README")

    # A container is stopped with SIGTERM, whose default disposition kills the
    # process outright: the finally below would never run and the socket to the
    # matrix would stay open as far as the matrix is concerned. Measured
    # consequence: it then refuses a new connection for about 90 seconds, so
    # every redeploy would begin with a minute and a half of "not connected".
    # Raising KeyboardInterrupt reuses the clean shutdown that already exists.
    install_stop_signals()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopping")
    finally:
        server.shutdown()
        server.server_close()
        link.stop()
    return 0


def in_container():
    """Whether this process is running inside a container.

    Only used to keep the startup message honest, so best effort is enough:
    /.dockerenv is what Docker itself creates, and the environment variable is
    set by this project's own image so another runtime can be told explicitly."""
    return kp.env_flag("KRAMER_IN_CONTAINER") or Path("/.dockerenv").exists()


def stop_on_signal(signum, _frame):
    """Turn a stop signal into the KeyboardInterrupt that main() already handles,
    so there is one shutdown path rather than two."""
    log(f"signal {signum} received, stopping")
    raise KeyboardInterrupt


def install_stop_signals():
    """SIGTERM is what a container runtime sends. SIGINT is already handled by
    Python raising KeyboardInterrupt, so it needs nothing. Guarded because
    signal.SIGTERM does not exist everywhere, and signal handlers can only be
    installed from the main thread - which is where main() runs."""
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_on_signal)


def _config_writable():
    """Best effort, purely so the warning can be printed at startup instead of
    first met when someone tries to rename an input.

    The authoritative answer is the write attempt itself, which is why this only
    produces a warning and PUT /api/labels still reports its own failure: an
    os.access check is optimistic for uid 0 and says nothing about a filesystem
    remounted read-only later."""
    probe = CONFIG_PATH.parent
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return os.access(probe, os.W_OK)


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
