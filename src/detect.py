#!/usr/bin/env python3
"""Game detection for CoreSplit.

Four independent signals, each able to raise a game on its own:

  1. gamemode D-Bus  - the most precise, but needs `gamemoderun`
  2. install paths   - steamapps/common, compatdata, lutris, heroic, wine
  3. launcher scope  - descendants of a Steam/Lutris/Heroic systemd scope
  4. KWin window     - optional fallback for games launched elsewhere

Detection is deliberately noisy-tolerant: Proton spawns and reaps helpers
constantly, so entering gaming needs several consecutive polls and leaving needs
a long quiet period. Without that the mode would flap during loading screens.

This module never imports engine.py; it works on any object exposing
pid/comm/ppid/exe/cmdline/cgroup.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

HOME = Path.home()

STEAM_CLIENT_ROOTS = [
    HOME / ".local/share/Steam",
    HOME / ".steam/steam",
    HOME / ".steam/root",
]

LAUNCHER_SCOPES = (
    "app-steam-",
    "app-lutris-",
    "app-heroic-",
    "app-com.heroicgameslauncher",
    "app-bottles-",
)

# Launcher plumbing that must never be mistaken for a running game.
CLIENT_NAMES = {
    "steam", "steamwebhelper", "reaper", "steam-runtime-launch-client",
    "pressure-vessel-shim", "pressure-vessel-wrap", "srt-bwrap", "pv-bwrap",
    "xdg-dbus-proxy", "lutris", "heroic", "bottles-cli", "bottles",
    "gamemoderun", "gamemoded", "steamtinkerlaunch", "protonfixes",
    "steamerrorreporter", "gldriverquery", "vulkaninfo", "fossilize_replay",
    "srt-logger", "steam-runtime-launcher-service",
}

# Launchers whose own UI runs as Electron: a launcher-scope match on one of these
# is the launcher window, not a game.
LAUNCHER_UI_NAMES = {"electron", "electron32", "electron34", "heroic", "lutris", "bottles"}

# Steam ships runtimes and tools under steamapps/common/, so a path match there is
# not proof of a game. These directories hold shared runtimes, not titles.
RUNTIME_DIRS = {
    "steamlinuxruntime", "steamlinuxruntime_sniper", "steamlinuxruntime_soldier",
    "steamlinuxruntime_1.0", "steamlinuxruntime_2.0", "steamlinuxruntime_3.0",
    "steamworks common redistributables", "proton", "proton - experimental",
    "proton hotfix", "proton battleye runtime", "proton easyanticheat runtime",
    "steamvr", "steam linux runtime",
}

# Helpers that live inside those runtimes.
RUNTIME_HELPER_NAMES = {
    "pv-adverb", "pv-bwrap", "pv-runtime", "pressure-vessel", "srt-logger",
    "steam-runtime-launcher-service", "steam-runtime-launch-client",
    "reaper", "steam-runtime-system-info", "srt-bwrap",
}

# Wrapper processes that launch a game but are not the game. When one of these is
# the detected root, the display name comes from a descendant instead.
WRAPPER_NAMES = {
    "proton", "umu-run", "wine", "wine64", "wine-preloader", "python3", "python",
    "bash", "sh", "gamemoderun", "steamtinkerlaunch", "python3.9", "python3.11",
    "python3.12", "python3.13",
}

# Directories that belong to launcher tooling rather than to a game install.
TOOL_PATH_PARTS = (
    "/.config/heroic/tools/",
    "/compatibilitytools.d/",
    "/.local/share/lutris/runners/",
    "/.local/share/lutris/runtime/",
    "/steamapps/common/steamlinuxruntime",
    "/steamapps/common/proton",
    "/.local/share/steam/steamapps/common/proton",
)

# A shell whose argv is `-c '<script>'` can contain any game path as text. Matching
# on that text would flag every shell that greps, greps a log, or runs a build
# command mentioning a game. The real game process matches on its own, so shells
# are skipped for path-based detection.
SHELL_NAMES = {"bash", "sh", "dash", "zsh", "fish", "ksh", "mksh", "busybox"}


def _is_shell(proc) -> bool:
    return _name_in(_proc_name(proc), SHELL_NAMES)


class GameInfo:
    __slots__ = ("pid", "name", "exe", "reason", "extra_roots")

    def __init__(self, pid, name, exe, reason, extra_roots=()):
        self.pid = pid
        self.name = name
        self.exe = exe
        self.reason = reason
        self.extra_roots = tuple(extra_roots)

    def __repr__(self):
        return f"GameInfo(pid={self.pid}, name={self.name!r}, reason={self.reason!r})"


def _basename(path: str) -> str:
    if not path:
        return ""
    if path.endswith(" (deleted)"):
        path = path[:-10]
    return path.rsplit("/", 1)[-1]


def _comm_matches(comm: str, name: str) -> bool:
    """Match a configured name against /proc comm, which truncates to 15 chars."""
    if not comm or not name:
        return False
    return comm == name or comm == name[:15]


def _name_in(comm: str, names) -> bool:
    return any(_comm_matches(comm, name) for name in names)


def _proc_name(proc) -> str:
    """Lowercase process name, preferring the exe basename over truncated comm."""
    return (_basename(proc.exe) or proc.comm).lower()


def _steam_title_marker(location: str) -> bool:
    """True when a steamapps path names a real title, not a runtime."""
    for match in re.finditer(r"/steamapps/common/([^/]+)", location):
        if match.group(1).lower() not in RUNTIME_DIRS:
            return True
    return "/compatdata/" in location


def _is_client(proc) -> bool:
    name = _proc_name(proc)
    if _name_in(name, CLIENT_NAMES) or _name_in(name, RUNTIME_HELPER_NAMES):
        return True
    exe = proc.exe or ""
    for root in STEAM_CLIENT_ROOTS:
        prefix = str(root)
        if exe.startswith(prefix + "/") and not exe.startswith(prefix + "/steamapps/"):
            return True
    return False


def _in_runtime_dir(path: str) -> bool:
    """True when the path points into a Steam runtime/tool rather than a title."""
    if not path:
        return False
    lowered = path.lower()
    match = re.search(r"/steamapps/common/([^/]+)", lowered)
    if match and match.group(1) in RUNTIME_DIRS:
        return True
    return any(f"/{name}/" in lowered or lowered.endswith(f"/{name}") for name in RUNTIME_HELPER_NAMES)


def _is_runtime_tool(proc) -> bool:
    """A process belonging to the Steam runtime wrapper, not to a game."""
    for candidate in (proc.exe, proc.cmdline):
        if not candidate:
            continue
        if _in_runtime_dir(candidate):
            return True
        lowered = candidate.lower()
        if any(part in lowered for part in TOOL_PATH_PARTS):
            return True
    return False


def _display_name(proc, procs) -> str:
    """Prefer a real game executable's name over its wrapper's name."""
    base = _basename(proc.exe) or proc.comm
    if not _name_in(base.lower(), WRAPPER_NAMES):
        return _game_name(proc)

    # Wrapper root: look down the process tree for the first plausible game exe.
    children = {}
    for other in procs.values():
        children.setdefault(other.ppid, []).append(other)
    stack = list(children.get(proc.pid, ()))
    seen = set()
    while stack:
        current = stack.pop(0)
        if current.pid in seen:
            continue
        seen.add(current.pid)
        for token in _argv(current):
            if not token.lower().endswith(".exe"):
                continue
            if any(part in token.lower() for part in TOOL_PATH_PARTS):
                continue
            if _name_in(_basename(token).lower(), WRAPPER_NAMES):
                continue
            return _game_name(current)
        stack.extend(children.get(current.pid, ()))
    return _game_name(proc)


def _argv(proc):
    """Argument vector if available, else a naive split of the joined cmdline."""
    argv = getattr(proc, "argv", ())
    return list(argv) if argv else (proc.cmdline or "").split()


def _game_name(proc) -> str:
    """Human name for a game process.

    Wine re-execs everything through `wine64-preloader`, so /proc/pid/exe is
    useless for Proton titles; the real executable path is in cmdline. argv is
    used so paths containing spaces survive intact. Windows-style paths are
    normalised to forward slashes first.
    """
    haystack = f"{proc.exe} {proc.cmdline}".replace("\\", "/")
    for match in re.finditer(r"/steamapps/common/([^/]+)", haystack):
        # SteamLinuxRuntime and Proton live in steamapps/common too; keep
        # scanning until a real title directory turns up.
        if match.group(1).lower() not in RUNTIME_DIRS:
            return match.group(1)
    if match := re.search(r"/compatdata/(\d+)/", haystack):
        return f"app {match.group(1)}"

    for token in _argv(proc):
        if not token.lower().endswith(".exe"):
            continue
        lowered = token.replace("\\", "/").lower()
        if any(part in lowered for part in TOOL_PATH_PARTS):
            continue
        folder = lowered.rsplit("/", 1)[0].rsplit("/", 1)[-1] if "/" in lowered else ""
        if folder and folder not in ("drive_c", "bin", "windows", "system32"):
            return folder
        return lowered.rsplit("/", 1)[-1]

    return _basename(proc.exe) or proc.comm


REASON_PRIORITY = {
    "gamemode": 0,
    "install path": 1,
    "emulator": 1,
    "launcher scope": 2,
    "active window": 3,
}


class Detector:
    def __init__(self, cfg, topo=None):
        self.cfg = cfg
        self.topo = topo
        settings = cfg.get("detection", {})
        self.use_gamemode = settings.get("gamemode", True)
        self.use_paths = settings.get("paths", True)
        self.use_launcher = settings.get("launcher", True)
        self.use_kwin = settings.get("kwin", False)
        hysteresis = cfg.get("hysteresis", {})
        self.enter_polls = max(1, int(hysteresis.get("enter_polls", 3)))
        self.exit_seconds = float(hysteresis.get("exit_seconds", 20))

        self._candidate = None
        self._candidate_polls = 0
        self._last_seen = 0.0
        self._current = None
        self._roots: list = []
        self._found_pids: list = []
        self._names: dict[int, str] = {}
        self._gamemode_cache: set[int] = set()

    # -- public ------------------------------------------------------------ #

    def names(self) -> dict:
        """Display names of the game roots seen on the last update()."""
        return dict(self._names)

    def current(self):
        """The primary game, if one is confirmed."""
        return self._current

    def promote(self, pid):
        """Make `pid` the primary game.

        The engine follows the game being played (the CPU-hungry one); the
        other games stay on the side as extras.
        """
        if self._current is None or pid == self._current.pid:
            return
        if all(item.pid != pid for item in self._roots):
            return
        self._roots = ([item for item in self._roots if item.pid == pid]
                       + [item for item in self._roots if item.pid != pid])
        self._current = self._roots[0]
        self._refresh_extra_roots(self._found_pids)

    def update(self, procs, now):
        # One gamemode query per poll; _match runs for every process.
        self._gamemode_cache = self._gamemode_pids() if self.use_gamemode else set()
        found = self._find(procs)

        if not found:
            if self._current is None:
                self._candidate = None
                self._candidate_polls = 0
                return None
            if now - self._last_seen >= self.exit_seconds:
                self._current = None
                self._candidate = None
                self._candidate_polls = 0
                return None
            return self._current

        self._last_seen = now
        self._roots = [item for item in found if item.pid in procs]
        self._found_pids = [item.pid for item in self._roots]
        self._names = {item.pid: item.name for item in self._roots}

        if self._current is None:
            # Entering gaming needs several stable polls, so Proton's helper
            # churn cannot flap the mode during loading screens.
            root = found[0]
            same = (
                self._candidate is not None
                and self._candidate.pid == root.pid
            )
            if not same:
                self._candidate = root
                self._candidate_polls = 1
            else:
                self._candidate_polls += 1
            if self._candidate_polls >= self.enter_polls:
                self._current = self._candidate
                self._refresh_extra_roots(self._found_pids)
            return self._current

        # A confirmed game keeps its role even when the root ordering flips
        # (gamemode's client order is not stable); every other root rides
        # along as an extra, so all running games land on the game side.
        if self._current.pid not in self._found_pids:
            # The primary exited while another game is still running: the
            # role hands over without an end/start round trip.
            self._current = self._roots[0]
        self._refresh_extra_roots(self._found_pids)
        return self._current

    def _refresh_extra_roots(self, found_pids):
        self._current.extra_roots = tuple(
            pid for pid in found_pids if pid != self._current.pid)

    # -- signals ----------------------------------------------------------- #

    def _find(self, procs):
        matches = {}
        for proc in procs.values():
            info = self._match(proc)
            if info:
                matches[proc.pid] = info

        if not matches:
            return []

        # Keep only roots: a process whose parent is also a match is part of the
        # same game, not a separate one.
        roots = []
        for pid, info in matches.items():
            parent = procs.get(procs[pid].ppid)
            if parent is not None and parent.pid in matches:
                continue
            roots.append(info)
        # A precise signal (gamemode, install path) outranks a loose one (launcher
        # scope), so a launcher window can never shadow the real game.
        roots.sort(key=lambda item: (REASON_PRIORITY.get(item.reason, 9), item.pid))
        for info in roots:
            proc = procs.get(info.pid)
            if proc is not None:
                info.name = _display_name(proc, procs)
        # Wine re-parents the game away from its launcher tree, so one game can
        # surface as several roots; roots naming the same title are one game.
        # (The engine's install-directory fallback still pins every process.)
        unique = []
        seen = set()
        for info in roots:
            key = info.name.lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(info)
        return unique

    def _match(self, proc):
        if _is_client(proc):
            return None

        if self.use_gamemode and proc.pid in self._gamemode_cache:
            return GameInfo(proc.pid, _game_name(proc), proc.exe, "gamemode")

        if self.use_paths and not _is_runtime_tool(proc) and not _is_shell(proc):
            location = f"{proc.exe} {proc.cmdline}".replace("\\", "/")
            for marker in self.cfg["game_path_markers"]:
                if marker and marker in location:
                    return GameInfo(proc.pid, _game_name(proc), proc.exe, "install path")
            if _name_in(_proc_name(proc), {emu.lower() for emu in self.cfg["emulators"]}):
                return GameInfo(proc.pid, proc.comm, proc.exe, "emulator")

        if self.use_launcher:
            for scope in LAUNCHER_SCOPES:
                if scope in proc.cgroup:
                    if _name_in(_proc_name(proc), LAUNCHER_UI_NAMES):
                        return None
                    return GameInfo(proc.pid, _game_name(proc), proc.exe, "launcher scope")
            # Newer Steam runs the client and its games inside one user service
            # (app-steam@<uuid>.service) instead of per-launch scopes. The
            # service also holds the client and all of Steam's plumbing, so a
            # match there needs a real title path, not just any steamapps one.
            if "app-steam@" in proc.cgroup:
                location = f"{proc.exe} {proc.cmdline}".replace("\\", "/")
                if _steam_title_marker(location):
                    return GameInfo(proc.pid, _game_name(proc), proc.exe, "launcher scope")

        if self.use_kwin:
            pid = self._kwin_pid()
            if pid and pid == proc.pid:
                return GameInfo(proc.pid, proc.comm, proc.exe, "active window")

        return None

    # -- external queries -------------------------------------------------- #

    def _gamemode_pids(self):
        """PIDs registered with gamemoded. One cheap query per poll; every
        registered game counts, not just the first client."""
        try:
            result = subprocess.run(
                [
                    "gdbus", "call", "--session",
                    "--dest", "com.feralinteractive.GameMode",
                    "--object-path", "/com/feralinteractive/GameMode",
                    "--method", "com.feralinteractive.GameMode.ListClients",
                ],
                capture_output=True, text=True, timeout=0.4,
            )
        except (OSError, subprocess.SubprocessError):
            return set()
        if result.returncode != 0 or not result.stdout:
            return set()
        # Output looks like: ([(12345, 0), (12346, 0)],)
        return {int(value) for value in re.findall(r"\((\d+),\s*\d+\)", result.stdout)}

    def _kwin_pid(self):
        try:
            result = subprocess.run(
                ["qdbus6", "org.kde.KWin", "/KWin", "org.kde.KWin.queryWindowInfo"],
                capture_output=True, text=True, timeout=0.4,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        match = re.search(r"\bpid:\s*(\d+)", result.stdout)
        return int(match.group(1)) if match else None
