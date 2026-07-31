# Tests

Plain scripts, no test framework and no dependencies: run them directly and check the exit code.
They are deliberately not built on `unittest` yet — the GUI checks share one window and run in a
fixed order, and forcing that into per-test isolation would cost more than it currently returns.

| Script | Needs hardware | What it covers |
|---|---|---|
| `test_gui_offline.py` | no | Window construction, the passive-listener wiring, periodic-refresh scheduling and its guards, notification handling, config round-trip and sanitisation |
| `test_gui_live.py` | yes | The worker against a live socket, the listener running in the idle gaps without stealing command replies, log muting, connect/disconnect |

```bash
python tests/test_gui_offline.py
python tests/test_gui_live.py --host 192.168.1.39
```

Both exit non-zero if any check fails, so either can gate a pull request. Only
`test_gui_offline.py` belongs in CI: the live one needs a matrix on the network.

On Windows and macOS Tkinter runs natively. On a headless Linux machine, use a virtual display:

```bash
xvfb-run -a python3 tests/test_gui_offline.py
```

## The live test and your video

`test_gui_live.py` is **read-only by default**: it only queries state. To also verify a switch end
to end, name an output you are willing to have changed — the previous input is restored right
after:

```bash
python tests/test_gui_live.py --host 192.168.1.39 --switch-output 3
```

Pick an output that is not showing anything you care about. The video on it will change for a few
seconds.

## Why these exist

They are not box-ticking. Between them they caught two real defects that reading the code did not
reveal:

- the automatic refresh queued a new read before the previous one returned, so a short interval
  made the job queue grow without bound;
- every Protocol 2000 command waited out its full 1 s timeout after the reply had already arrived,
  because a binary frame has no terminator — a full state read cost 4.4 s instead of 0.9 s.

Anything added on top of this code — a REST API in particular — inherits both the single-worker
constraint and the reply/notification split, which is exactly the kind of thing that breaks
quietly. Run these first.
