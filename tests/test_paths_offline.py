#!/usr/bin/env python3
#
# Part of kramer-vs44-remote-control. Copyright (C) 2026 Piero Biagini.
# Licensed under the GNU GPL v3 or later. See LICENSE for details.
"""Unit checks for kramer_paths.py: no hardware, no GUI, no network.

This file guards the one mistake in the whole project that would be both easy to
make and invisible: writing the settings somewhere that disappears. A bundled
executable unpacks itself into a temporary directory and deletes it on exit, so a
settings file resolved from __file__ would vanish on every close without a single
error message. The checks below pin the resolution rules down instead of trusting
a manual run to notice.
"""

import json
import os
import stat
import sys
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kramer_paths as kp

results = []


def check(name, got, want):
    results.append((got == want, f"{name:52s} got={got!r} want={want!r}"))


def temp_dir():
    return Path(mkdtemp())


# --- the version is a single string, and the programs report it ------------- #
check("version looks like a version", kp.VERSION.count("."), 2)
check("the shared file name never drifts", kp.CONFIG_NAME,
      "kramer_gui_config.json")


# --- resolving where the program lives ------------------------------------- #
# Not frozen: the module's own directory, which is the project root.
check("not frozen by default", kp.frozen(), False)
check("program_dir is the project root", kp.program_dir(),
      Path(__file__).resolve().parent.parent)

# Frozen: the directory holding the executable, NEVER __file__. Getting this
# wrong is the bug this file exists for, so it is checked rather than assumed.
fake_exe_dir = temp_dir()
saved_frozen = getattr(sys, "frozen", None)
saved_exe = sys.executable
try:
    sys.frozen = True
    sys.executable = str(fake_exe_dir / "kramer-gui.exe")
    check("frozen is detected", kp.frozen(), True)
    check("frozen program_dir follows the executable", kp.program_dir(),
          fake_exe_dir.resolve())
    check("and not the module directory",
          kp.program_dir() != Path(kp.__file__).resolve().parent, True)
finally:
    sys.executable = saved_exe
    if saved_frozen is None:
        del sys.frozen
    else:
        sys.frozen = saved_frozen
check("frozen state restored", kp.frozen(), False)


# --- bundled read-only resources ------------------------------------------- #
check("the web page is found from source",
      kp.resource_path("web", "index.html").exists(), True)

meipass = temp_dir()
saved_meipass = getattr(sys, "_MEIPASS", None)
try:
    sys._MEIPASS = str(meipass)
    check("a bundle's resources come from _MEIPASS",
          kp.resource_path("web", "index.html"),
          meipass / "web" / "index.html")
finally:
    if saved_meipass is None:
        del sys._MEIPASS
    else:
        sys._MEIPASS = saved_meipass


# --- the per-user directory, both branches, from either OS ----------------- #
check("Windows uses APPDATA",
      kp.user_config_dir("win32", {"APPDATA": r"C:\Users\x\AppData\Roaming"}),
      Path(r"C:\Users\x\AppData\Roaming") / kp.APP_DIR_NAME)
check("elsewhere XDG_CONFIG_HOME wins",
      kp.user_config_dir("linux", {"XDG_CONFIG_HOME": "/somewhere/cfg"}),
      Path("/somewhere/cfg") / kp.APP_DIR_NAME)
check("and falls back to ~/.config",
      kp.user_config_dir("linux", {}),
      Path.home() / ".config" / kp.APP_DIR_NAME)
check("an empty APPDATA is not a value",
      kp.user_config_dir("win32", {"APPDATA": ""}),
      Path.home() / "AppData" / "Roaming" / kp.APP_DIR_NAME)


# --- config_path precedence ------------------------------------------------ #
saved_program_dir = kp.program_dir
saved_env = os.environ.get(kp.CONFIG_ENV)


def set_env(value):
    if value is None:
        os.environ.pop(kp.CONFIG_ENV, None)
    else:
        os.environ[kp.CONFIG_ENV] = value


try:
    # 4. nothing beside the program and nothing in the environment.
    empty = temp_dir()
    kp.program_dir = lambda: empty
    set_env(None)
    check("with nothing else, the per-user directory", kp.config_path(),
          kp.user_config_dir() / kp.CONFIG_NAME)

    # 3. portable mode: a settings file already sitting next to the program.
    # This is what keeps a development checkout behaving exactly as it always
    # has, and it is the reason no migration is needed.
    beside = temp_dir()
    (beside / kp.CONFIG_NAME).write_text("{}", encoding="utf-8")
    kp.program_dir = lambda: beside
    check("a file beside the program wins", kp.config_path(),
          beside / kp.CONFIG_NAME)

    # 2. the environment beats portable mode.
    from_env = temp_dir() / "from_env.json"
    set_env(str(from_env))
    check("the environment beats a file beside the program", kp.config_path(),
          from_env)

    # 1. an explicit path beats everything.
    from_flag = temp_dir() / "from_flag.json"
    check("an explicit path beats the environment",
          kp.config_path(str(from_flag)), from_flag)

    set_env(None)
    check("a tilde is expanded", kp.config_path("~/kramer.json"),
          Path.home() / "kramer.json")
finally:
    kp.program_dir = saved_program_dir
    set_env(saved_env)

# On a working copy that already has a settings file next to the scripts - which
# is what a developer machine looks like - the resolved path must still be
# exactly the one used before any of this existed. If this check ever fails,
# someone's labels have just moved.
#
# It is conditional on purpose. The file is gitignored, so a fresh clone does not
# have one and portable mode correctly does not apply: asserting it
# unconditionally would be asserting a property of one machine rather than of the
# code. The rule itself is covered above, against a temporary directory.
here = kp.program_dir() / kp.CONFIG_NAME
if here.exists():
    check("an existing settings file next to the scripts stays the one used",
          kp.config_path(), here)
else:
    results.append((True, "no settings file beside the program: portable-mode "
                          "check skipped, as on a fresh clone"))


# --- reading ---------------------------------------------------------------- #
missing = temp_dir() / "absent.json"
reported = []
check("a missing file reads as empty", kp.read_json(missing, reported.append), {})
check("and is not reported, because it is normal", reported, [])

corrupt = temp_dir() / "corrupt.json"
corrupt.write_text("{ this is not json", encoding="utf-8")
check("a corrupt file reads as empty", kp.read_json(corrupt, reported.append), {})
check("but is reported", len(reported), 1)

not_an_object = temp_dir() / "list.json"
not_an_object.write_text("[1, 2, 3]", encoding="utf-8")
check("a JSON array is not a settings file", kp.read_json(not_an_object), {})


# --- writing --------------------------------------------------------------- #
target = temp_dir() / "missing" / "sub" / kp.CONFIG_NAME
kp.merge_json(target, {"host": "192.168.1.39", "inputs": ["a"]})
check("a missing directory is created", target.exists(), True)

kp.merge_json(target, {"port": 5000})
stored = json.loads(target.read_text(encoding="utf-8"))
check("keys are merged, not replaced", sorted(stored), ["host", "inputs", "port"])
check("and the earlier value survives", stored["host"], "192.168.1.39")

# The GUI and the service own different keys in this one file. A write from
# either must never discard what the other put there.
kp.merge_json(target, {"inputs": ["renamed"]})
check("a foreign key survives someone else's write",
      json.loads(target.read_text(encoding="utf-8"))["port"], 5000)

check("no temporary file is left behind",
      sorted(p.name for p in target.parent.iterdir()), [kp.CONFIG_NAME])


# --- writing when it cannot work ------------------------------------------- #
# A directory standing where the file should be is the portable way to make a
# write fail on any operating system.
blocked = temp_dir() / "in_the_way.json"
blocked.mkdir()
try:
    kp.merge_json(blocked, {"host": "x"})
    check("writing onto a directory fails", "no exception", "OSError")
except OSError:
    check("writing onto a directory raises OSError", "OSError", "OSError")

# A write that cannot complete must leave the previous settings intact rather
# than a truncated file, and must not litter. Each operating system gets the
# scenario that is real for it, so the guarantee is checked on both rather than
# asserted on one and hoped for on the other.
#
# Note that injecting a Path whose replace() fails does not work: merge_json
# normalises its argument with Path(path), which turns any subclass back into a
# plain path. Hence the two real scenarios below.
victim_dir = temp_dir()
victim = victim_dir / kp.CONFIG_NAME
kp.merge_json(victim, {"host": "before"})
before = victim.read_text(encoding="utf-8")

if os.name == "posix":
    # A read-only directory: a container volume mounted read-only, which is
    # exactly the case the service has to survive. Vacuous as root, and CI
    # runners are not root, so the coverage exists where it matters.
    os.chmod(victim_dir, stat.S_IRUSR | stat.S_IXUSR)
    scenario = "a read-only directory"
    try:
        try:
            kp.merge_json(victim, {"host": "after"})
            check(f"{scenario} fails the write", "no exception", "OSError")
        except OSError:
            check(f"{scenario} raises OSError", "OSError", "OSError")
    finally:
        os.chmod(victim_dir, stat.S_IRWXU)
else:
    # On Windows the rename refuses while the destination is held open, which is
    # what a file open in another program looks like.
    scenario = "a destination held open"
    handle = open(victim, "r", encoding="utf-8")
    try:
        kp.merge_json(victim, {"host": "after"})
        check(f"{scenario} fails the write", "no exception", "OSError")
    except OSError:
        check(f"{scenario} raises OSError", "OSError", "OSError")
    finally:
        handle.close()

check("the previous settings are untouched",
      victim.read_text(encoding="utf-8"), before)
check("and no temporary file survives the failure",
      sorted(p.name for p in victim_dir.iterdir()), [kp.CONFIG_NAME])


# --- environment helpers --------------------------------------------------- #
os.environ.pop("KRAMER_TEST_VALUE", None)
check("an unset variable gives the fallback",
      kp.env_default("KRAMER_TEST_VALUE", "8000"), "8000")
os.environ["KRAMER_TEST_VALUE"] = "8080"
check("a set variable wins", kp.env_default("KRAMER_TEST_VALUE", "8000"), "8080")
# A form field left blank in a container UI arrives as an empty string, which
# must not be mistaken for a deliberate value.
os.environ["KRAMER_TEST_VALUE"] = ""
check("an empty variable is not a value",
      kp.env_default("KRAMER_TEST_VALUE", "8000"), "8000")
os.environ.pop("KRAMER_TEST_VALUE", None)

for value, expected in (("1", True), ("true", True), ("TRUE", True),
                        ("yes", True), ("on", True), (" 1 ", True),
                        ("0", False), ("false", False), ("", False),
                        ("maybe", False)):
    os.environ["KRAMER_TEST_FLAG"] = value
    check(f"env_flag({value!r})", kp.env_flag("KRAMER_TEST_FLAG"), expected)
os.environ.pop("KRAMER_TEST_FLAG", None)
check("an absent flag is false", kp.env_flag("KRAMER_TEST_ABSENT"), False)


failed = [line for ok, line in results if not ok]
for ok, line in results:
    print(f"  {'OK  ' if ok else 'FAIL'} {line}")
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
