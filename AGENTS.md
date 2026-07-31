# Notes for automated contributors

The [README](README.md) is the documentation; this file is the part that is easy to get
wrong when you act before reading. Read `## Design notes` and `## Known limitations` there
before changing behaviour, and `tests/README.md` before touching the hardware.

## There is real hardware on the other end

The switcher is in someone's home, wired to monitors they are probably using right now.

1. **Close what you open.** The device holds a TCP slot for about **90 seconds** after a
   connection dies ungracefully. A window you spawned and did not close costs the next run a
   minute and a half — and costs the owner their control panel. Kill the process you actually
   started, not its launcher, and verify nothing of yours is left connected.
2. **One controller at a time**, for the reason in the README — the loser is told nothing.
   Check before you start; do not add yourself to what is already connected.
3. **`--dry-run` prints the bytes without opening anything.** Use it for anything about
   framing. Use hardware only for questions that are about hardware.
4. **Never send `#FACTORY`.** It erases presets and EDID, there is no undo, and it is absent
   from every UI on purpose.
5. **Restore any route you change.** The live tests do; anything new should, and should
   default to read-only.

## Two invariants worth naming, both explained at the site

All device I/O is one worker thread — the 200 ms interval lives in `Transport`, so a second
reader breaks timing rather than merely being untidy; add a job, not a thread. And no writable
path is ever resolved from `__file__`: a frozen build unpacks into a directory that is deleted
on exit, so everything path-related goes through `kramer_paths`.

## Before claiming a change works

The four `tests/*_offline.py` scripts must exit zero (`xvfb-run -a` for the GUI one on headless
Linux). `Dockerfile` and the PyInstaller spec have no local feedback loop — CI is the only one,
so say that rather than implying you ran them.
