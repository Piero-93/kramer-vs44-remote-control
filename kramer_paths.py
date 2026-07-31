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
kramer_paths.py - where the settings live, and which version this is.

Shared by the CLI, the GUI and the web service. It must never import tkinter:
kramer_server.py imports this module, and a headless container has no business
requiring Tk.

Everything here exists because of packaging. Two problems, both invisible until
the program stops running from a checkout:

  - A frozen executable unpacks itself into a temporary directory that is
    deleted on exit, so anything written next to __file__ vanishes when the
    program closes. Silently, which is the dangerous part.
  - A container has no writable directory beside the program at all, and has to
    be told where the settings should go.

The settings file is shared between the GUI and the service, and each of them
owns different keys in it, so writing is always a read-modify-write and lives
here rather than in both.
"""

import json
import os
import sys
from pathlib import Path

VERSION = "0.1.0"

CONFIG_NAME = "kramer_gui_config.json"
CONFIG_ENV = "KRAMER_CONFIG"
APP_DIR_NAME = "kramer-vs44"


# --------------------------------------------------------------------------- #
# Where things are
# --------------------------------------------------------------------------- #

def frozen():
    """True when running from a bundled executable rather than from source."""
    return bool(getattr(sys, "frozen", False))


def program_dir():
    """The directory the user believes the program lives in.

    For a frozen binary that is where the executable sits, NOT the temporary
    directory it unpacked itself into. That distinction is the whole reason this
    module exists: settings written to the unpack directory are deleted when the
    program exits."""
    if frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(*parts):
    """A read-only file shipped alongside the program, such as the web page.

    A bundler unpacks those into sys._MEIPASS; running from source they sit next
    to this module. This is the only place in the project that knows about that
    difference."""
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base else Path(__file__).resolve().parent
    return root.joinpath(*parts)


def user_config_dir(platform=None, env=None):
    """The per-user settings directory, following each platform's convention.

    A pure function of the platform name and the environment, both injectable,
    so either branch can be checked from either operating system. Without that
    the Linux branch would never run on the developer's Windows machine and the
    Windows branch would never run in CI, and an untested branch is where this
    would quietly rot.

    Nothing is created here; see ensure_parent()."""
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    if platform == "win32":
        base = env.get("APPDATA") or Path.home() / "AppData" / "Roaming"
    else:
        base = env.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / APP_DIR_NAME


def config_path(override=None):
    """Where the settings file lives. Highest precedence first:

      1. an explicit path, from --config
      2. the KRAMER_CONFIG environment variable
      3. a settings file already sitting next to the program - portable mode.
         This is also what keeps a development checkout behaving exactly as it
         always has, with nothing to migrate and no settings left behind
      4. the per-user directory

    The returned path need not exist yet; every caller already copes with that.
    """
    if override:
        return Path(override).expanduser()
    from_env = os.environ.get(CONFIG_ENV)
    if from_env:
        return Path(from_env).expanduser()
    beside = program_dir() / CONFIG_NAME
    if beside.exists():
        return beside
    return user_config_dir() / CONFIG_NAME


def ensure_parent(path):
    """Create the directory holding path, if it is missing.

    Raises OSError when that is impossible - a read-only volume, for instance.
    Callers are expected to report it rather than hide it."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Reading and writing the settings file
# --------------------------------------------------------------------------- #

def read_json(path, on_error=None):
    """The object stored at path, or {} when there is nothing usable there.

    A missing file is normal and silent. A corrupt or unreadable one is reported
    through on_error, if given, and then ignored: a bad settings file must not
    stop the program from starting."""
    try:
        # utf-8-sig, not utf-8: a Windows editor - Notepad, or PowerShell's
        # Set-Content -Encoding UTF8 - writes a byte-order mark, and json.loads
        # refuses it. Anyone hand-editing this file on Windows would otherwise
        # silently get the defaults back. The suffix is harmless on a file that
        # has no BOM, and writing still produces plain UTF-8 without one.
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        if on_error:
            on_error(e)
        return {}
    return data if isinstance(data, dict) else {}


def merge_json(path, updates):
    """Merge updates into the JSON object at path and write it back atomically.

    Only the given keys are touched. The GUI and the service share this file and
    own different keys in it, so neither may discard what it does not recognise.

    The write goes to a sibling temporary file which is then renamed over the
    target, so an interrupted write cannot leave a truncated settings file
    behind. Renaming is atomic only within one filesystem, which is why the
    temporary file is a sibling rather than in the system temp directory.

    Raises OSError when the location cannot be written. That is a real
    possibility - a read-only volume in a container - and each caller decides
    what to do about it: the service reports it over HTTP, the GUI logs it."""
    path = Path(path)
    data = read_json(path)
    data.update(updates)
    ensure_parent(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return data


# --------------------------------------------------------------------------- #
# Command-line plumbing shared by every entry point
# --------------------------------------------------------------------------- #

def add_common_arguments(parser):
    """--config and --version, which are identical in all three programs."""
    parser.add_argument("--config", metavar="PATH",
                        help="settings file to use; by default a file next to "
                             "the program if there is one, otherwise the "
                             f"per-user directory (environment: {CONFIG_ENV})")
    parser.add_argument("--version", action="version",
                        version=f"kramer-vs44 {VERSION}")


def env_default(name, fallback=None):
    """An environment variable's value, or fallback when it is unset or empty.

    Meant to be passed as an argparse default, which makes a command-line flag
    win over the environment with no extra code. Note that argparse applies its
    `type` conversion to string defaults, so returning the raw string is correct
    even for numeric options."""
    value = os.environ.get(name)
    return value if value not in (None, "") else fallback


def env_flag(name):
    """An environment variable read as a boolean switch, for store_true options."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")
