#!/usr/bin/env python3
"""CoreSplit engine: CPU topology, process scanning, classification, affinity pinning.

Pure Python, no Qt. Both the GUI and the headless daemon drive this class.

The rule that shapes everything here: a CPU affinity mask is a permission, not an
exclusion. A process may run on any CPU in its mask, so the only way to keep a game
clear of other work is to move the other work somewhere else.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (HOME / ".config")) / "coresplit"
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = CONFIG_DIR / "state.json"

try:
    CLOCK_TICKS = os.sysconf("SC_CLK_TCK")
except (ValueError, OSError):  # pragma: no cover - non-Linux
    CLOCK_TICKS = 100

MODES = ("off", "smart", "hard")

# /proc/<pid>/stat reports comm truncated to 15 characters, so a configured name
# like "kwin_wayland_wrapper" arrives as "kwin_wayland_wr". Every name comparison
# has to account for that or the entry silently never matches.
COMM_LENGTH = 15


def comm_matches(comm: str, name: str) -> bool:
    if not comm or not name:
        return False
    return comm == name or comm == name[:COMM_LENGTH]


def name_in(comm: str, names) -> bool:
    return any(comm_matches(comm, name) for name in names)

DEFAULT_TEST_PATTERNS = [
    # rust
    "cargo", "rustc", "rustdoc", "clippy-driver", "rust-analyzer", "nextest",
    "cargo-nextest",
    # c / c++
    "cc1", "cc1plus", "clang", "clang++", "gcc", "g++", "ld", "ld.lld", "lld",
    "mold", "cmake", "ninja", "make", "gmake", "meson", "bazel", "scons",
    # jvm / .net
    "gradle", "mvn", "dotnet", "msbuild", "java",
    # go
    "go", "gopls", "gotestsum",
    # js / ts
    "node", "bun", "deno", "tsc", "esbuild", "swc", "webpack", "vite", "turbo",
    "jest", "vitest", "mocha", "playwright", "cypress", "eslint",
    # python
    "pytest", "tox", "nox", "mypy", "pyright", "ruff", "black", "isort",
    "coverage", "hypothesis",
]

DEFAULT_GAME_PATH_MARKERS = [
    "/steamapps/common/",
    "/compatdata/",
    "/Games/Heroic/",
    "/.wine/drive_c/",
]

DEFAULT_EMULATORS = [
    "retroarch", "dolphin-emu", "pcsx2", "rpcs3", "yuzu", "ryujinx", "cemu",
    "duckstation", "ppsspp", "melonds", "mgba", "snes9x", "mupen64plus",
    "flycast", "xemu",
]

# In the frame/audio path: these belong on the game CCD, not parked on the test CCD.
DEFAULT_PROTECTED = [
    "kwin_wayland", "kwin_wayland_wrapper", "kwin_x11", "Xwayland",
    "pipewire", "pipewire-pulse", "wireplumber",
    "gamemoded", "gamescope", "gamescopereaper", "mangohud",
]

# Some processes hold file capabilities (setcap), which makes them non-dumpable
# and refuses affinity changes with EPERM even for the owning user. Leaving them
# unpinned is correct: they are privileged helpers or the compositor, and both
# are better off unconstrained than half-pinned.
# Note: only the compositor itself, not its plain wrapper script.
DEFAULT_UNPINNABLE = [
    "kwin_wayland", "gsr-kms-server",
]

# Lists where a missing entry is a correctness bug rather than a preference.
# Saved configs are unioned with these defaults so upgrades keep applying new
# safety entries instead of freezing an old copy.
ADDITIVE_KEYS = (
    "protected", "unpinnable", "floating", "test_patterns",
    "emulators", "agent_scopes", "game_path_markers",
)

# Idle session plumbing. Leaving these alone is safer than pinning them.
DEFAULT_FLOATING = [
    "plasmashell", "kded6", "kded5", "kactivitymanagerd", "ksmserver",
    "kglobalaccel", "kaccess", "klipper", "kscreen", "powerdevil",
    "polkit-kde-authentication-agent-1", "xdg-desktop-portal",
    "xdg-desktop-portal-kde", "xdg-desktop-portal-gtk", "dbus-daemon",
    "dbus-broker", "systemd", "gnome-keyring-daemon", "kwalletd6", "kwalletd5",
    "kdeconnectd", "gmenudbusmenuproxy", "xembedsniproxy", "at-spi-bus-launcher",
    "at-spi2-registryd", "klauncher", "upowerd", "xwaylandvideobridge",
    "kscreen_backend_launcher", "kio_http_cache_cleaner",
]

DEFAULT_CONFIG = {
    "game_cpus": "",
    "test_cpus": "",
    "auto_enable": True,
    "auto_mode": "hard",
    "smart_renice": False,
    "smart_ioprio": False,
    # With two games open, the background one gets its own cores on the game
    # side but yields priority to the one being played.
    "favour_foreground": True,
    "agent_scopes": [
        "app-zcode-", "app-opencode-", "app-code-", "app-ai.opencode",
        "app-com.heroicgameslauncher.hgl@",
    ],
    "test_patterns": DEFAULT_TEST_PATTERNS,
    "game_path_markers": DEFAULT_GAME_PATH_MARKERS,
    "emulators": DEFAULT_EMULATORS,
    "protected": DEFAULT_PROTECTED,
    "unpinnable": DEFAULT_UNPINNABLE,
    "floating": DEFAULT_FLOATING,
    "detection": {
        "gamemode": True,
        "paths": True,
        "launcher": True,
        "kwin": False,
    },
    "hysteresis": {"enter_polls": 3, "exit_seconds": 20},
    "poll_interval": 1.5,
    "autostart": False,
    # Never let agent windows take focus away from a running game.
    "focus_guard": True,
}


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

def _deep_merge(base, override):
    out = dict(base)
    for key, value in override.items():
        if value is None:
            # A null in a saved file means "unset", never "clear the default".
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        elif key in ADDITIVE_KEYS and isinstance(value, list) and isinstance(out.get(key), list):
            # Union, defaults first, so new safety entries always take effect
            # while user additions survive.
            merged = list(out[key])
            for item in value:
                if item not in merged:
                    merged.append(item)
            out[key] = merged
        else:
            out[key] = value
    return out


class Config:
    """JSON-backed settings with defaults merged in."""

    def __init__(self, data=None):
        self.data = _deep_merge(DEFAULT_CONFIG, data or {})
        self._enforce_invariants()

    def _enforce_invariants(self):
        """Keep the safety lists consistent.

        `protected` and `unpinnable` overlap only for names that are genuinely
        impossible to pin (the compositor). If a name was moved from one list to
        the other in a newer version, a stale saved copy would otherwise keep it
        in the wrong list forever, so anything that is not a shipped unpinnable
        name but is protected gets dropped from `unpinnable`.
        """
        protected = set(self.data.get("protected") or ())
        shipped = set(DEFAULT_UNPINNABLE)
        self.data["unpinnable"] = [
            name for name in (self.data.get("unpinnable") or ())
            if name in shipped or name not in protected
        ]

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def get(self, key, default=None):
        return self.data.get(key, default)

    def update(self, values):
        self.data = _deep_merge(self.data, values)

    def save(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write(CONFIG_PATH, json.dumps(self.data, indent=2) + "\n")

    def ensure_split(self, topo):
        """Fill the CPU sets from topology if they are missing or invalid."""
        game = parse_cpulist(self.data.get("game_cpus"))
        test = parse_cpulist(self.data.get("test_cpus"))
        all_cpus = set(topo.all_cpus)
        if not game or not test or not (game | test) <= all_cpus or (game & test):
            game, test = topo.default_split()
            self.data["game_cpus"] = format_cpulist(game)
            self.data["test_cpus"] = format_cpulist(test)
            return True
        return False

    @classmethod
    def load(cls):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
                return cls(json.load(handle))
        except (OSError, ValueError):
            return cls()


def _atomic_write(path: Path, text: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# cpu lists
# --------------------------------------------------------------------------- #

def parse_cpulist(spec) -> set[int]:
    cpus: set[int] = set()
    if not spec:
        return cpus
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            try:
                cpus.update(range(int(start), int(end) + 1))
            except ValueError:
                continue
        else:
            try:
                cpus.add(int(part))
            except ValueError:
                continue
    return cpus


def format_cpulist(cpus) -> str:
    ordered = sorted(set(cpus))
    chunks = []
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1] == ordered[end] + 1:
            end += 1
        if end == index:
            chunks.append(str(ordered[index]))
        else:
            chunks.append(f"{ordered[index]}-{ordered[end]}")
        index = end + 1
    return ",".join(chunks)


# --------------------------------------------------------------------------- #
# topology
# --------------------------------------------------------------------------- #

class Topology:
    """CPU layout discovered from sysfs.

    CCDs are found from L3 cache sharing, not by halving CPU numbers: on a 9900X
    that yields 0-5,12-17 and 6-11,18-23, which is what we actually want.
    """

    def __init__(self, all_cpus, ccds, siblings):
        self.all_cpus = sorted(all_cpus)
        self.ccds = [sorted(group) for group in ccds]
        self.siblings = siblings  # cpu -> sorted sibling list

    @classmethod
    def read(cls):
        base = Path("/sys/devices/system/cpu")
        all_cpus = []
        try:
            for entry in base.iterdir():
                name = entry.name
                if name.startswith("cpu") and name[3:].isdigit():
                    all_cpus.append(int(name[3:]))
        except OSError:
            all_cpus = list(range(os.cpu_count() or 1))
        all_cpus.sort()

        siblings = {}
        for cpu in all_cpus:
            try:
                raw = (base / f"cpu{cpu}" / "topology" / "thread_siblings_list").read_text()
                siblings[cpu] = sorted(parse_cpulist(raw.strip()))
            except OSError:
                siblings[cpu] = [cpu]

        domains: dict[str, list[int]] = {}
        for cpu in all_cpus:
            try:
                key = (base / f"cpu{cpu}" / "cache" / "index3" / "shared_cpu_list").read_text().strip()
            except OSError:
                key = ""
            domains.setdefault(key, []).append(cpu)

        ccds = [sorted(group) for group in domains.values() if group]
        ccds.sort(key=lambda group: group[0])

        if len(ccds) < 2:
            # No usable L3 split: fall back to physical-core halves, keeping
            # SMT siblings together.
            cores, seen = [], set()
            for cpu in all_cpus:
                group = tuple(siblings.get(cpu, [cpu]))
                if group in seen:
                    continue
                seen.add(group)
                cores.append(list(group))
            half = max(1, len(cores) // 2)
            ccds = [
                sorted(cpu for group in cores[:half] for cpu in group),
                sorted(cpu for group in cores[half:] for cpu in group),
            ]
            ccds = [group for group in ccds if group]

        return cls(all_cpus, ccds, siblings)

    def physical_cores(self):
        """Sibling groups in a stable order, one entry per physical core."""
        cores, seen = [], set()
        for cpu in self.all_cpus:
            group = tuple(self.siblings.get(cpu, [cpu]))
            if group in seen:
                continue
            seen.add(group)
            cores.append(list(group))
        return cores

    def core_count(self, cpus) -> int:
        """Number of physical cores fully or partly covered by `cpus`."""
        wanted = set(cpus)
        return sum(1 for group in self.physical_cores() if wanted.intersection(group))

    def split_by_core_count(self, game_cores):
        cores = self.physical_cores()
        game, test = set(), set()
        for index, group in enumerate(cores):
            (game if index < game_cores else test).update(group)
        return game, test

    def split_side(self, cpus, parts):
        """Divide `cpus` into `parts` whole-physical-core blocks, so two games
        sharing one side each get their own cores instead of colliding."""
        wanted = set(cpus)
        cores = [group for group in self.physical_cores() if set(group) & wanted]
        groups = [[] for _ in range(max(1, parts))]
        base, extra = divmod(len(cores), len(groups))
        index = 0
        for position, group in enumerate(groups):
            take = base + (1 if position < extra else 0)
            group.extend(cores[index:index + take])
            index += take
        return [set().union(*group) if group else set() for group in groups]

    def default_split(self):
        """Game gets the first CCD, tests get everything else."""
        game = set(self.ccds[0])
        test = set(self.all_cpus) - game
        return game, test

    def smt_split(self, cpus) -> list[int]:
        """CPUs whose SMT sibling is outside `cpus` (a warning signal)."""
        wanted = set(cpus)
        return sorted(
            cpu for cpu in wanted
            if any(sib not in wanted for sib in self.siblings.get(cpu, [cpu]))
        )


# --------------------------------------------------------------------------- #
# /proc readers
# --------------------------------------------------------------------------- #

def _basename(path: str) -> str:
    if not path:
        return ""
    if path.endswith(" (deleted)"):
        path = path[:-10]
    return path.rsplit("/", 1)[-1]


@dataclass
class ProcInfo:
    pid: int
    comm: str
    state: str
    ppid: int
    uid: int
    starttime: int
    threads: int
    nice: int
    cgroup: str
    exe: str
    cmdline: str
    argv: tuple
    ticks: int
    affinity: frozenset
    cpu: float = 0.0


def _read_stat(pid: int):
    """Return (comm, state, ppid, utime+stime, nice, threads, starttime).

    comm can contain spaces and parentheses, so everything is parsed after the
    last ')'. Field N of /proc/pid/stat is rest[N-3].
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    left = data.find(b"(")
    right = data.rfind(b")")
    if left < 0 or right < 0:
        return None
    comm = data[left + 1:right].decode("utf-8", "replace")
    rest = data[right + 2:].split()
    if len(rest) < 20:
        return None
    try:
        return (
            comm,
            rest[0].decode(),
            int(rest[1]),
            int(rest[11]) + int(rest[12]),
            int(rest[16]),
            int(rest[17]),
            int(rest[19]),
        )
    except (ValueError, IndexError):
        return None


def _read_cgroup(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cgroup", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("0::"):
                    return line[3:].strip()
    except OSError:
        pass
    return ""


def _read_exe(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return ""


def _read_cmdline(pid: int):
    """Return (joined cmdline, argv tuple).

    argv is kept separately because game paths routinely contain spaces, and
    joining then splitting on whitespace shreds them.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read(8192)
    except OSError:
        return "", ()
    parts = [chunk.decode("utf-8", "replace") for chunk in raw.split(b"\0") if chunk]
    return " ".join(parts).strip(), tuple(parts)


def read_process(pid: int) -> ProcInfo | None:
    """Read one process. Used by the focus guard, which must answer per window."""
    stat = _read_stat(pid)
    if stat is None:
        return None
    comm, state, ppid, ticks, nice, threads, starttime = stat
    try:
        uid = os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None
    cmdline, argv = _read_cmdline(pid)
    return ProcInfo(
        pid=pid, comm=comm, state=state, ppid=ppid, uid=uid,
        starttime=starttime, threads=threads, nice=nice,
        cgroup=_read_cgroup(pid), exe=_read_exe(pid),
        cmdline=cmdline, argv=argv, ticks=ticks,
        affinity=frozenset(), cpu=0.0,
    )


def scan_processes(exclude_pid: int, previous=None) -> dict[int, ProcInfo]:
    """Snapshot every process in /proc.

    `previous` maps pid -> (ticks, timestamp) and is used for CPU percentages.
    """
    procs: dict[int, ProcInfo] = {}
    now = time.monotonic()
    try:
        entries = list(os.scandir("/proc"))
    except OSError:
        return procs

    for entry in entries:
        name = entry.name
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == exclude_pid:
            continue
        stat = _read_stat(pid)
        if stat is None:
            continue
        comm, state, ppid, ticks, nice, threads, starttime = stat
        try:
            uid = os.stat(f"/proc/{pid}").st_uid
        except OSError:
            continue
        try:
            affinity = frozenset(os.sched_getaffinity(pid))
        except (OSError, PermissionError):
            affinity = frozenset()
        cpu = 0.0
        if previous and pid in previous:
            last_ticks, last_time = previous[pid]
            elapsed = now - last_time
            if elapsed > 0.05:
                cpu = max(0.0, (ticks - last_ticks) / CLOCK_TICKS / elapsed * 100.0)
        cmdline, argv = _read_cmdline(pid)
        procs[pid] = ProcInfo(
            pid=pid,
            comm=comm,
            state=state,
            ppid=ppid,
            uid=uid,
            starttime=starttime,
            threads=threads,
            nice=nice,
            cgroup=_read_cgroup(pid),
            exe=_read_exe(pid),
            cmdline=cmdline,
            argv=argv,
            ticks=ticks,
            affinity=affinity,
            cpu=cpu,
        )
    return procs


def set_affinity(pid: int, cpus) -> int:
    """Pin every thread of `pid`. Returns the number of threads updated.

    sched_setaffinity acts on a single TID, so a process with many threads needs
    each one touched. Threads created afterwards inherit the mask.
    """
    mask = set(cpus)
    tids = [pid]
    try:
        tids = [int(tid) for tid in os.listdir(f"/proc/{pid}/task")]
    except OSError:
        pass
    updated = 0
    for tid in tids:
        try:
            os.sched_setaffinity(tid, mask)
            updated += 1
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return updated


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #

@dataclass
class PinnedEntry:
    start: int
    orig: list
    role: str
    nice: int | None = None  # original nice, set when we raised it for a background game


class EngineState:
    def __init__(self):
        self.mode = "off"
        self.pinned: dict[int, PinnedEntry] = {}
        self.manual_roles: dict[int, str] = {}
        self.roles: dict[int, str] = {}
        self.game = None
        self.game_set: set[int] = set()
        self.logs: list[str] = []
        # Boot-relative tick when pinning began. Children forked after this point
        # inherit a CCD mask at fork time and may never be scanned before the
        # restore, so they need a sweep of their own.
        self.engaged_ticks: int = 0


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #

class Engine:
    """Owns all state on a single background thread.

    Callers talk to it through `request()`; results come back via callbacks so
    the Qt layer can marshal them onto the GUI thread.
    """

    def __init__(self, cfg: Config, on_snapshot=None, on_log=None):
        self.cfg = cfg
        self.topo = Topology.read()
        self._ensure_split()
        self.on_snapshot = on_snapshot or (lambda snap: None)
        self.on_log = on_log or (lambda line: None)
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = EngineState()
        self._prev: dict[int, tuple[int, float]] = {}
        self._last_snapshot: dict = {}
        self._auto_engaged = False
        self._manual = False
        self._dirty = False
        self._publisher = None
        self._guard_pids: set[int] = set()
        self._warned: set[int] = set()
        self._promote_streak = 0
        self._slice_sig = None
        from detect import Detector  # local import keeps detect.py free of cycles
        self._detector = Detector(cfg, self.topo)

    # -- setup ------------------------------------------------------------- #

    def _ensure_split(self):
        if self.cfg.ensure_split(self.topo):
            self.cfg.save()

    @property
    def game_mask(self) -> set[int]:
        return parse_cpulist(self.cfg["game_cpus"])

    @property
    def test_mask(self) -> set[int]:
        return parse_cpulist(self.cfg["test_cpus"])

    # -- public api -------------------------------------------------------- #

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="coresplit-engine", daemon=True)
        self._thread.start()

    def stop(self, timeout=6.0):
        self._stop.set()
        self._q.put(("shutdown", {}))
        if self._thread:
            self._thread.join(timeout)

    def request(self, name, **kwargs):
        self._q.put((name, kwargs))

    def set_mode(self, mode, manual=True):
        self.request("set_mode", mode=mode, manual=manual)

    def status(self) -> dict:
        return self._last_snapshot or self._build_snapshot({})

    def attach_publisher(self, publisher):
        """Publish guard state into kwinrc after each tick."""
        self._publisher = publisher

    def _update_guard_pids(self, procs):
        """Collect the pids whose windows must never take focus from the game.

        This uses the agent classifier, not the CPU role. In hard mode every
        process is a "test" process for CPU purposes, but only real agent work
        should be barred from focus: deliberately alt-tabbing to a browser or a
        terminal has to keep working.
        """
        if not self.guard_active():
            self._guard_pids = set()
            return
        blocked = set()
        for proc in procs.values():
            if proc.uid != os.getuid() or proc.pid == os.getpid():
                continue
            if proc.pid in self._state.game_set:
                continue
            if self._is_test(proc, procs):
                blocked.add(proc.pid)
        self._guard_pids = blocked

    def _publish_guard(self):
        if self._publisher is None:
            return
        active = self.guard_active()
        game_pid = 0
        if active and self._state.game is not None:
            game_pid = self._state.game.pid
        pids = self._guard_pids if active else ()
        try:
            self._publisher.publish(active, game_pid, pids)
        except Exception:
            pass

    # -- focus guard ------------------------------------------------------- #

    def guard_active(self) -> bool:
        """True while a game is protected from focus stealing."""
        state = self._state
        return bool(self.cfg["focus_guard"]) and state.mode != "off" and state.game is not None

    def is_blocked_pid(self, pid: int) -> bool:
        """True when this process owns a window that must not take focus.

        Uses the agent classifier rather than the CPU role, so only automated
        work is barred; the user's own browser and terminal stay usable.
        """
        if not self.guard_active():
            return False
        if pid <= 0:
            return True  # IsActive probe
        if pid == os.getpid():
            return False
        if pid in set(self._state.game_set):
            return False
        proc = read_process(pid)
        if proc is None or proc.uid != os.getuid():
            return False
        return self._is_test(proc, {pid: proc}) is not None

    # -- thread loop ------------------------------------------------------- #

    def _run(self):
        try:
            self._restore_stale()
            while not self._stop.is_set():
                started = time.monotonic()
                self._drain()
                if self._stop.is_set():
                    break
                try:
                    self._tick()
                except Exception as exc:  # keep the loop alive
                    self._log(f"engine error: {exc!r}")
                interval = max(0.2, float(self.cfg["poll_interval"]))
                self._stop.wait(max(0.05, interval - (time.monotonic() - started)))
        finally:
            self._restore_all("shutdown")
            self._save_state()
            self._emit_snapshot()

    def _drain(self):
        while True:
            try:
                name, kwargs = self._q.get_nowait()
            except queue.Empty:
                return
            if name == "shutdown":
                self._stop.set()
                return
            if name == "set_mode":
                self._set_mode(kwargs.get("mode", "off"), kwargs.get("manual", True))
            elif name == "reset":
                self._set_mode("off", manual=True)
                self._restore_all("reset")
            elif name == "manual_pin":
                for pid in kwargs.get("pids", []):
                    self._state.manual_roles[int(pid)] = kwargs.get("role", "test")
                self._dirty = True
            elif name == "manual_release":
                for pid in kwargs.get("pids", []):
                    self._state.manual_roles.pop(int(pid), None)
                    entry = self._state.pinned.pop(int(pid), None)
                    if entry:
                        self._restore_entry(int(pid), entry)
                self._dirty = True
            elif name == "refresh":
                pass
            elif name == "apply_config":
                self._detector = type(self._detector)(self.cfg, self.topo)
                self._dirty = True

    def _tick(self):
        procs = scan_processes(os.getpid(), self._prev)
        self._prev = {pid: (p.ticks, time.monotonic()) for pid, p in procs.items()}

        game = self._detector.update(procs, time.monotonic())
        if game and not self._state.game:
            self._on_game_start(game)
        elif not game and self._state.game:
            self._on_game_end()
        elif game:
            self._state.game = game
        if self._state.game is not None:
            self._follow_foreground(procs)

        self._apply(procs)
        self._update_guard_pids(procs)
        self._publish_guard()
        self._emit_snapshot(procs)
        if self._dirty:
            self._save_state()
            self._dirty = False

    # -- mode -------------------------------------------------------------- #

    def _set_mode(self, mode, manual=True):
        if mode not in MODES:
            mode = "off"
        if manual:
            self._manual = True
            self._auto_engaged = False
        if mode == self._state.mode and not self._state.pinned:
            return
        if mode == "off":
            self._restore_all("mode off")
        else:
            if self._state.mode != mode:
                self._restore_all(f"switch to {mode}")
        if mode != "off" and not self._state.engaged_ticks:
            self._state.engaged_ticks = _boot_ticks()
        self._state.mode = mode
        self._state.manual_roles.clear()
        self._warned.clear()
        self._log(f"mode -> {mode}" + ("" if manual else " (auto)"))
        self._dirty = True

    def _on_game_start(self, game):
        self._state.game = game
        self._log(f"game detected: {game.name} (pid {game.pid}, {game.reason})")
        self._manual = False
        if self.cfg["auto_enable"]:
            self._auto_engaged = True
            self._set_mode(self.cfg["auto_mode"], manual=False)

    def _on_game_end(self):
        name = getattr(self._state.game, "name", "game")
        self._state.game = None
        self._state.game_set = set()
        self._log(f"game exited: {name}")
        self._manual = False
        if self._auto_engaged:
            self._auto_engaged = False
            self._set_mode("off", manual=False)

    def _follow_foreground(self, procs):
        """With several games running, promote the one being played.

        A played game burns CPU; a background one mostly idles. When another
        game's tree out-burns the primary by a wide margin for a few polls, it
        becomes the primary: it keeps the un-reniced half and the old primary
        starts yielding to it.
        """
        game = self._state.game
        extras = [pid for pid in getattr(game, "extra_roots", ()) if pid in procs]
        if not extras:
            self._promote_streak = 0
            return
        primary_cpu = self._tree_cpu(procs, [game.pid])
        rival, rival_cpu = None, 0.0
        for pid in extras:
            cpu = self._tree_cpu(procs, [pid])
            if cpu > rival_cpu:
                rival, rival_cpu = pid, cpu
        if rival_cpu > primary_cpu * 3 + 20.0:
            self._promote_streak += 1
            if self._promote_streak >= 2:
                name = self._detector.names().get(rival, f"pid {rival}")
                self._detector.promote(rival)
                self._state.game = self._detector.current()
                self._promote_streak = 0
                self._log(f"{name} looks like the game being played; promoting it")
                self._slice_sig = None
        else:
            self._promote_streak = 0

    def _tree_cpu(self, procs, roots) -> float:
        return sum(proc.cpu for proc in procs.values()
                   if proc.pid in self._game_subtree(procs, roots))

    # -- classification ---------------------------------------------------- #

    def _game_subtree(self, procs, roots) -> set[int]:
        if not roots:
            return set()
        children: dict[int, list[int]] = {}
        for proc in procs.values():
            children.setdefault(proc.ppid, []).append(proc.pid)
        seen: set[int] = set()
        stack = [pid for pid in roots if pid in procs]
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            stack.extend(children.get(pid, ()))

        # Detached wine/proton helpers can lose the parent link, so also match
        # anything whose executable lives in the game's install directory.
        dirs = set()
        for pid in roots:
            proc = procs.get(pid)
            if not proc:
                continue
            marker = _install_root(proc.exe or proc.cmdline)
            if marker:
                dirs.add(marker)
        if dirs:
            for proc in procs.values():
                if proc.pid in seen:
                    continue
                location = proc.exe or proc.cmdline
                if location and any(location.startswith(d) for d in dirs):
                    seen.add(proc.pid)
        return seen

    def _is_test(self, proc, procs) -> str | None:
        for scope in self.cfg["agent_scopes"]:
            if scope and scope in proc.cgroup:
                return f"cgroup {scope}"
        ancestor = self._ancestor_scope(proc, procs)
        if ancestor:
            return f"ancestor in {ancestor}"

        names = {proc.comm.lower(), _basename(proc.exe).lower()}
        patterns = {p.lower() for p in self.cfg["test_patterns"]}
        for name in names:
            if name and name_in(name, patterns):
                return f"process {name}"
        for token in proc.cmdline.split()[:6]:
            if _basename(token).lower() in patterns:
                return f"cmdline {_basename(token)}"
        if "-m pytest" in proc.cmdline or "unittest" in proc.cmdline:
            return "pytest"
        return None

    def _ancestor_scope(self, proc, procs):
        pid = proc.ppid
        for _ in range(12):
            if pid <= 1:
                return None
            parent = procs.get(pid)
            if parent is None:
                return None
            for scope in self.cfg["agent_scopes"]:
                if scope and scope in parent.cgroup:
                    return scope
            pid = parent.ppid
        return None

    def _apply(self, procs):
        state = self._state
        if state.mode == "off" and not state.manual_roles:
            self._restore_missing(procs, desired={})
            return

        roots = []
        if state.game is not None and state.mode != "off":
            roots.append(state.game.pid)
            roots.extend(
                pid for pid in getattr(state.game, "extra_roots", ()) if pid in procs)
        # One subtree per root, so each running game can get its own cores.
        per_root = [(pid, self._game_subtree(procs, [pid])) for pid in roots]
        game_set = set()
        for _pid, tree in per_root:
            game_set |= tree
        state.game_set = game_set

        # Several games on one side: slice the side into whole-physical-core
        # blocks, one per game, so a background game cannot steal cycles from
        # the one being played.
        game_masks: dict[int, set] = {}
        background: set[int] = set()
        if len(per_root) > 1:
            slices = self.topo.split_side(self.game_mask, len(per_root))
            primary_tree = per_root[0][1]
            for index, (_pid, tree) in enumerate(per_root):
                if index > 0:
                    background |= tree
                for pid in tree:
                    game_masks[pid] = slices[index]
            self._log_slice_layout(per_root, slices, primary_tree)

        desired: dict[int, str] = {}
        protected = set(self.cfg["protected"])
        floating = set(self.cfg["floating"])
        unpinnable = set(self.cfg["unpinnable"])
        my_uid = os.getuid()

        if state.mode == "smart":
            for proc in procs.values():
                if proc.pid in game_set:
                    desired[proc.pid] = "game"
                elif self._is_test(proc, procs):
                    desired[proc.pid] = "test"
        elif state.mode == "hard":
            for proc in procs.values():
                if proc.uid != my_uid:
                    continue
                if name_in(proc.comm, unpinnable):
                    continue
                if proc.pid in game_set:
                    desired[proc.pid] = "game"
                elif name_in(proc.comm, protected):
                    desired[proc.pid] = "game"
                elif name_in(proc.comm, floating):
                    continue
                else:
                    desired[proc.pid] = "test"

        for pid, role in state.manual_roles.items():
            if pid in procs:
                desired[pid] = role

        game_mask, test_mask = self.game_mask, self.test_mask
        newly: dict[str, list[ProcInfo]] = {"game": [], "test": []}
        for pid, role in desired.items():
            proc = procs.get(pid)
            if proc is None:
                continue
            if role == "game":
                want = game_masks.get(pid, game_mask)
            else:
                want = test_mask
            entry = state.pinned.get(pid)
            if entry is not None and entry.nice is not None and pid not in background:
                # No longer a background game (became primary, or its role
                # changed): give back the original nice value.
                self._set_nice(pid, entry.nice)
                entry.nice = None
            if entry is None:
                if not proc.affinity:
                    continue
                entry = PinnedEntry(start=proc.starttime, orig=sorted(proc.affinity), role=role)
                state.pinned[pid] = entry
                self._dirty = True
                self._pin(proc, want, role, background=pid in background)
                newly[role].append(proc)
            elif entry.start != proc.starttime:
                # PID reused by a different process; drop the stale record.
                state.pinned.pop(pid, None)
                continue
            elif entry.role != role:
                entry.role = role
                self._dirty = True
                self._pin(proc, want, role, background=pid in background)
                newly[role].append(proc)
            elif proc.affinity != want:
                self._pin(proc, want, role, background=pid in background)

        self._log_pins(newly)
        self._restore_missing(procs, desired)
        state.roles = dict(desired)

    def _log_pins(self, newly):
        """Games get a line each; background work is summarised to stay readable."""
        for proc in newly["game"]:
            self._log(f"pid {proc.pid} {proc.comm} -> game CCD")
        tests = newly["test"]
        if not tests:
            return
        names = sorted({proc.comm for proc in tests})
        preview = ", ".join(names[:6]) + ("…" if len(names) > 6 else "")
        self._log(f"pinned {len(tests)} background process(es) to test CCD ({preview})")

    def _log_slice_layout(self, per_root, slices, primary_tree):
        """One line when the per-game core layout changes, not every tick."""
        names = self._detector.names() if hasattr(self, "_detector") else {}
        signature = tuple(
            (pid, tuple(sorted(slices[index])))
            for index, (pid, _tree) in enumerate(per_root)
        )
        if getattr(self, "_slice_sig", None) == signature:
            return
        self._slice_sig = signature
        parts = []
        for index, (pid, tree) in enumerate(per_root):
            name = names.get(pid) or (self._state.game.name if self._state.game else f"pid {pid}")
            role = "foreground" if tree == primary_tree else "background"
            parts.append(f"{name} ({role}) -> {format_cpulist(slices[index])}")
        self._log(f"{len(per_root)} games share the game side: " + "; ".join(parts))

    def _set_nice(self, pid, value):
        try:
            os.setpriority(os.PRIO_PROCESS, pid, value)
        except OSError:
            pass

    def _pin(self, proc, mask, role, background=False):
        if not mask:
            return
        updated = set_affinity(proc.pid, mask)
        if updated == 0:
            # Most failures are a process that exited between the scan and the
            # pin. Only complain about ones that are still alive, and only once.
            if proc.pid in self._warned or _read_stat(proc.pid) is None:
                return
            self._warned.add(proc.pid)
            self._log(f"cannot pin pid {proc.pid} ({proc.comm}); leaving it unconstrained")
            return
        if role != "test":
            # A background game keeps its own cores but yields priority to the
            # one actually being played.
            if background and self.cfg["favour_foreground"] and proc.nice < 5:
                entry = self._state.pinned.get(proc.pid)
                if entry is not None and entry.nice is None:
                    entry.nice = proc.nice
                self._set_nice(proc.pid, 5)
            return
        if self.cfg["smart_ioprio"]:
            _set_ioprio(proc.pid, 3, 0)
        if self.cfg["smart_renice"] and proc.nice < 19:
            try:
                os.setpriority(os.PRIO_PROCESS, proc.pid, 19)
            except OSError:
                pass

    def _restore_missing(self, procs, desired):
        for pid in list(self._state.pinned):
            if pid in desired:
                continue
            entry = self._state.pinned.pop(pid)
            proc = procs.get(pid)
            if proc is not None and proc.starttime != entry.start:
                continue
            self._restore_entry(pid, entry)
            self._dirty = True

    def _restore_entry(self, pid, entry):
        proc = None
        try:
            stat = _read_stat(pid)
            if stat and stat[6] == entry.start:
                proc = True
        except Exception:
            proc = None
        if entry.nice is not None:
            self._set_nice(pid, entry.nice)
            entry.nice = None
        if proc and entry.orig:
            set_affinity(pid, entry.orig)

    def _restore_all(self, reason=""):
        if not self._state.pinned and not self._state.engaged_ticks:
            return
        count = len(self._state.pinned)
        for pid, entry in list(self._state.pinned.items()):
            self._restore_entry(pid, entry)
        orphans = self._sweep_orphans()
        self._state.pinned.clear()
        self._state.roles.clear()
        self._state.game_set = set()
        self._state.engaged_ticks = 0
        self._warned.clear()
        self._dirty = True
        message = f"released {count} processes"
        if orphans:
            message += f" and {orphans} inherited pin(s)"
        if reason:
            message += f" ({reason})"
        self._log(message)

    def _sweep_orphans(self) -> int:
        """Unpin processes that inherited a CCD mask but were never recorded.

        A child forked while its parent was pinned inherits the narrow mask. If it
        is reaped or rescanned too late it never lands in the pinned table, so it
        would otherwise stay on one CCD forever. Processes we deliberately leave
        alone (desktop plumbing, unpinnable helpers) are skipped so the sweep can
        never fight with an intentional decision.
        """
        if not self._state.engaged_ticks:
            return 0
        masks = (self.game_mask, self.test_mask)
        full = set(self.topo.all_cpus)
        exempt = set(self.cfg["protected"]) | set(self.cfg["floating"]) | set(self.cfg["unpinnable"])
        my_uid = os.getuid()
        swept = 0
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == os.getpid():
                continue
            try:
                if os.stat(f"/proc/{pid}").st_uid != my_uid:
                    continue
                affinity = set(os.sched_getaffinity(pid))
            except (OSError, PermissionError):
                continue
            if affinity not in masks:
                continue
            stat = _read_stat(pid)
            if stat is None or stat[6] < self._state.engaged_ticks:
                continue
            if name_in(stat[0], exempt):
                continue
            set_affinity(pid, full)
            swept += 1
        return swept

    # -- persistence ------------------------------------------------------- #

    def _save_state(self):
        payload = {
            "version": 1,
            "mode": self._state.mode,
            "game_cpus": self.cfg["game_cpus"],
            "test_cpus": self.cfg["test_cpus"],
            "entries": {
                str(pid): {"start": entry.start, "orig": entry.orig, "role": entry.role}
                for pid, entry in self._state.pinned.items()
            },
        }
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            _atomic_write(STATE_PATH, json.dumps(payload))
        except OSError as exc:
            self._log(f"could not save state: {exc}")

    def _restore_stale(self):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return
        entries = payload.get("entries") or {}
        if not entries:
            return
        restored = 0
        for pid_text, entry in entries.items():
            try:
                pid = int(pid_text)
                start = int(entry["start"])
                orig = [int(cpu) for cpu in entry["orig"]]
            except (ValueError, KeyError, TypeError):
                continue
            stat = _read_stat(pid)
            if stat is None or stat[6] != start:
                continue
            set_affinity(pid, orig)
            restored += 1
        if restored:
            self._log(f"recovered {restored} pinned processes from a previous session")
        try:
            STATE_PATH.unlink()
        except OSError:
            pass

    # -- output ------------------------------------------------------------ #

    def _log(self, line):
        stamp = time.strftime("%H:%M:%S")
        text = f"[{stamp}] {line}"
        self._state.logs.append(text)
        del self._state.logs[:-400]
        try:
            self.on_log(text)
        except Exception:
            pass

    def _build_snapshot(self, procs):
        state = self._state
        my_uid = os.getuid()
        counts = {"game": 0, "test": 0, "other": 0, "pinned": len(state.pinned)}
        rows = []
        for pid, proc in procs.items():
            role = state.roles.get(pid, "other")
            counts[role] = counts.get(role, 0) + 1
            rows.append({
                "pid": pid,
                "name": proc.comm,
                "cpu": round(proc.cpu, 1),
                "role": role,
                "affinity": format_cpulist(proc.affinity) if proc.affinity else "?",
                "scope": _short_scope(proc.cgroup),
                "threads": proc.threads,
                "own": proc.uid == my_uid,
            })
        rows.sort(key=lambda row: row["pid"])
        game = state.game
        return {
            "mode": state.mode,
            "auto": bool(self.cfg["auto_enable"]),
            "auto_mode": self.cfg["auto_mode"],
            "game": None if game is None else {
                "pid": game.pid,
                "name": game.name,
                "reason": game.reason,
            },
            "games": self._game_names(),
            "game_cpus": self.cfg["game_cpus"],
            "test_cpus": self.cfg["test_cpus"],
            "all_cpus": format_cpulist(self.topo.all_cpus),
            "ccds": [format_cpulist(group) for group in self.topo.ccds],
            "counts": counts,
            "procs": rows,
            "logs": list(state.logs[-200:]),
        }

    def _game_names(self):
        """Display names of every game currently on the game side."""
        game = self._state.game
        if game is None:
            return []
        names = [game.name]
        known = self._detector.names() if hasattr(self, "_detector") else {}
        for pid in getattr(game, "extra_roots", ()):
            name = known.get(pid)
            if name and name not in names:
                names.append(name)
        return names

    def _emit_snapshot(self, procs=None):
        if procs is None:
            procs = {}
        snapshot = self._build_snapshot(procs)
        self._last_snapshot = snapshot
        try:
            self.on_snapshot(snapshot)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _boot_ticks() -> int:
    """Current boot-relative tick count, in the same units as /proc stat field 22."""
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            uptime = float(handle.read().split()[0])
        return int(uptime * CLOCK_TICKS)
    except (OSError, ValueError, IndexError):
        return 0


def _short_scope(cgroup: str) -> str:
    if not cgroup:
        return ""
    parts = [p for p in cgroup.split("/") if p]
    for part in reversed(parts):
        if part.endswith(".scope") or part.endswith(".service"):
            return part
    return parts[-1] if parts else ""


def _install_root(path: str) -> str:
    """Best-effort install directory for a game executable."""
    if not path:
        return ""
    match = re.search(r"(.*?/steamapps/common/[^/]+)", path)
    if match:
        return match.group(1)
    match = re.search(r"(.*?/compatdata/\d+/pfx)", path)
    if match:
        return match.group(1)
    return ""


def _set_ioprio(pid: int, ioprio_class: int, data: int):
    """Best-effort IOPRIO_CLASS_IDLE (3). Reversible; ignored if unsupported."""
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        SYS_ioprio_set = 251  # x86_64
        libc.syscall(SYS_ioprio_set, 1, pid, (ioprio_class << 13) | data)
    except Exception:
        pass


def testcpus_script(cfg: Config, topo: Topology) -> str:
    cpus = cfg["test_cpus"]
    cores = topo.core_count(parse_cpulist(cpus)) or 1
    template = """#!/usr/bin/env bash
# Generated by CoreSplit: run CPU-heavy work on the test CCD at low priority.
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-__N__}"
export RAYON_NUM_THREADS="${RAYON_NUM_THREADS:-__N__}"
export GOMAXPROCS="${GOMAXPROCS:-__N__}"
export MAKEFLAGS="${MAKEFLAGS:--j__N__}"
exec taskset -c __CPUS__ ionice -c 3 nice -n 19 "$@"
"""
    return template.replace("__N__", str(cores)).replace("__CPUS__", cpus)


def steam_launch_option(cfg: Config) -> str:
    return f"taskset -c {cfg['game_cpus']} gamemoderun %command%"


def run_test_load(cfg: Config, topo: Topology, seconds: int = 10):
    """Spawn busy loops on the test CPUs, then kill them. Blocking."""
    cpus = cfg["test_cpus"]
    cores = topo.core_count(parse_cpulist(cpus)) or 1
    procs = []
    try:
        for _ in range(cores):
            procs.append(subprocess.Popen(
                ["taskset", "-c", cpus, "bash", "-c", "while :; do :; done"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ))
        time.sleep(seconds)
    finally:
        for proc in procs:
            try:
                proc.terminate()
            except OSError:
                pass
        for proc in procs:
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except OSError:
                    pass
    return len(procs)


def reset_standalone() -> int:
    """Restore every pin recorded in state.json without a running engine."""
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return 0
    restored = 0
    for pid_text, entry in (payload.get("entries") or {}).items():
        try:
            pid = int(pid_text)
            start = int(entry["start"])
            orig = [int(cpu) for cpu in entry["orig"]]
        except (ValueError, KeyError, TypeError):
            continue
        stat = _read_stat(pid)
        if stat is None or stat[6] != start:
            continue
        set_affinity(pid, orig)
        restored += 1
    try:
        STATE_PATH.unlink()
    except OSError:
        pass
    return restored
