# Tests

Plain scripts, no test framework and no dependencies: run them directly and check the exit code.
They are deliberately not built on `unittest` yet — the GUI checks share one window and run in a
fixed order, and forcing that into per-test isolation would cost more than it currently returns.

| Script | Needs hardware | What it covers |
|---|---|---|
| `test_protocol_offline.py` | no | Frame generation against the byte sequences confirmed in the manual and on hardware, reply decoding, the `expect` shortcut, end-of-stream handling, and the separation of unsolicited frames from replies — driven through a fake socket |
| `test_gui_offline.py` | no | Window construction, the passive-listener wiring, periodic-refresh scheduling and its guards, notification handling, config round-trip and sanitisation |
| `test_server_offline.py` | no | Every HTTP route, request validation, the label round-trip and its read-modify-write, the token gate, the `--allow-preset-store` gate and the preset occupancy map, the event stream, and that a stalled browser cannot block the device thread |
| `test_gui_live.py` | yes | The GUI worker against a live socket, the listener running in the idle gaps without stealing command replies, log muting, connect/disconnect |
| `test_server_live.py` | yes | The service's device thread against a live socket, events end to end, and recovery after the link drops |

```bash
python tests/test_protocol_offline.py
python tests/test_gui_offline.py
python tests/test_server_offline.py
python tests/test_gui_live.py --host 192.168.1.39
python tests/test_server_live.py --matrix 192.168.1.39
```

All of them exit non-zero if any check fails, so any can gate a pull request. The three `*_offline`
scripts run in CI on every push and pull request — see `.github/workflows/tests.yml`. The live ones
need a matrix on the network and are deliberately left out of it.

On Windows and macOS Tkinter runs natively. On a headless Linux machine, use a virtual display:

```bash
xvfb-run -a python3 tests/test_gui_offline.py
```

One thing to know before adding window-state checks there: **a bare X server has no window
manager**, so `iconify()` does nothing — it is a request to a window manager that is not running,
and the window stays mapped. `withdraw()` unmaps directly and behaves the same everywhere, which is
why the real-window check uses it. Whether Tk reports `iconic` after a minimise is Tk's business and
the desktop's, not this project's, so the state-to-visible mapping is checked on its own instead.

## The live tests and your video

Both live scripts are **read-only by default**: they only query state. To also verify a switch end
to end, name an output you are willing to have changed — the previous input is restored right
after:

```bash
python tests/test_gui_live.py --host 192.168.1.39 --switch-output 3
python tests/test_server_live.py --matrix 192.168.1.39 --switch-output 3
```

Pick an output that is not showing anything you care about. The video on it will change for a few
seconds.

`test_server_live.py` also closes the socket under the device thread on purpose, to check that the
link is noticed as dead and re-established. Expect a few seconds during which the service reports
itself disconnected — that is the test working, not a fault.

Run the live tests **one at a time and with no other controller running**, for the same reason the
main README gives: the matrix does not report one client's switches to another.

## Why these exist

They are not box-ticking. Between them they caught two real defects that reading the code did not
reveal:

- the automatic refresh queued a new read before the previous one returned, so a short interval
  made the job queue grow without bound;
- every Protocol 2000 command waited out its full 1 s timeout after the reply had already arrived,
  because a binary frame has no terminator — a full state read cost 4.4 s instead of 0.9 s.

- `PUT /api/labels` returned only the keys that were updated, and put that same partial object on
  the event stream. Since a client replaces its whole label state with what arrives, any partial
  update would have left every connected browser with an incomplete set and a broken grid.

Anything added on top of this code inherits the single-worker constraint and the reply/notification
split, which is exactly the kind of thing that breaks quietly. Run these first.
