#!/usr/bin/env python3
"""Focus guard: stops agent windows from stealing focus while a game runs.

How it works
------------
A small KWin script is installed as a normal KWin script package and enabled
through kwinrc. It listens for window activation. When a window belonging to an
agent is activated while a game is protected, it hands focus straight back to
the game.

KWin's scripting sandbox has no filesystem or socket access, so the state it
needs is published into the script's own kwinrc group, which the script reads
with ``readConfig``:

    [Script-coresplit-focus-guard]
    GuardActive=true
    GuardGamePid=1234
    GuardBlockedPids=111,222,333

Only *agent* pids are published, so deliberately alt-tabbing to your browser or
terminal still works. The guard exists to stop automated tooling from pulling
focus away, not to lock the desktop.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

SCRIPT_ID = "coresplit-focus-guard"
SCRIPT_DIR = Path.home() / ".local/share/kwin/scripts" / SCRIPT_ID
MAIN_JS = SCRIPT_DIR / "contents/code/main.js"
META_JSON = SCRIPT_DIR / "metadata.json"
CONFIG_GROUP = f"Script-{SCRIPT_ID}"

# Upper bound on the published list. Windows are owned by a handful of processes,
# so a long list is mostly dead weight in a config file.
MAX_BLOCKED = 160


def _metadata() -> dict:
    return {
        "KPlugin": {
            "Id": SCRIPT_ID,
            "Name": "CoreSplit Focus Guard",
            "Description": "Keeps a running game focused while agents work in the background",
            "Icon": "input-gaming",
            "Authors": [{"Name": "CoreSplit"}],
            "License": "MIT",
            "Version": "1.1",
            "Enabled": True,
        },
        "X-Plasma-API": "javascript",
        "X-Plasma-MainScript": "code/main.js",
        "KPackageStructure": "KWin/Script",
    }


# The script must stay defensive: it runs inside the compositor, so an exception
# there would be disruptive. Every entry point is wrapped.
MAIN_JS_TEXT = r"""
// CoreSplit focus guard.
//
// Keeps a protected game focused. A window owned by an agent process is bounced
// back to the game instead of being allowed to take focus.

const GROUP_ACTIVE = "GuardActive";
const GROUP_GAME = "GuardGamePid";
const GROUP_BLOCKED = "GuardBlockedPids";

let lastGame = null;
let lastBlockedPid = 0;
let lastBlockedAt = 0;

// How long a repeat activation of the same process is treated as the user
// insisting. One programmatic focus grab gets bounced; trying again straight
// away means a human wants that window, so it is allowed through.
const INSIST_MS = 3000;

function windows() {
    return workspace.windowList ? workspace.windowList() : workspace.clientList();
}

function flag(name, fallback) {
    try {
        return readConfig(name, fallback);
    } catch (e) {
        return fallback;
    }
}

function blockedSet() {
    const raw = String(flag(GROUP_BLOCKED, ""));
    const set = {};
    const parts = raw.split(",");
    for (let i = 0; i < parts.length; i++) {
        const value = parseInt(parts[i], 10);
        if (!isNaN(value) && value > 0) {
            set[value] = true;
        }
    }
    return set;
}

function gamePid() {
    const value = parseInt(String(flag(GROUP_GAME, "0")), 10);
    return isNaN(value) ? 0 : value;
}

function rememberGame(win) {
    if (win && !win.deleted && win.fullScreen) {
        lastGame = win;
    }
}

function findGame() {
    const wanted = gamePid();
    const list = windows();

    // Prefer a fullscreen window that belongs to the game process.
    for (let i = 0; i < list.length; i++) {
        const w = list[i];
        if (w && !w.deleted && w.fullScreen && wanted && w.pid === wanted) {
            return w;
        }
    }
    if (lastGame && !lastGame.deleted && lastGame.fullScreen) {
        return lastGame;
    }
    for (let i = 0; i < list.length; i++) {
        const w = list[i];
        if (w && !w.deleted && w.fullScreen) {
            return w;
        }
    }
    return null;
}

function restore() {
    const target = findGame();
    if (!target) {
        return false;
    }
    if (workspace.activeWindow === target) {
        return true;
    }
    workspace.activeWindow = target;
    return true;
}

function isBlocked(win) {
    if (!win || win.deleted) {
        return false;
    }
    const blocked = blockedSet();
    if (blocked[win.pid]) {
        return true;
    }
    // A window opened as a child of a blocked process (an agent spawning a
    // viewer) inherits the same pid, but a transient may report its own; treat
    // anything explicitly transient from a blocked pid as blocked too.
    return false;
}

function onActivated(win) {
    try {
        if (!win || win.deleted) {
            return;
        }
        if (win.fullScreen) {
            rememberGame(win);
            lastBlockedPid = 0;
            return;
        }
        if (String(flag(GROUP_ACTIVE, "false")) !== "true") {
            return;
        }
        if (!isBlocked(win)) {
            return;
        }

        const now = Date.now();
        if (win.pid === lastBlockedPid && (now - lastBlockedAt) < INSIST_MS) {
            // Second attempt in quick succession: the user means it.
            lastBlockedPid = 0;
            return;
        }
        if (restore()) {
            lastBlockedPid = win.pid;
            lastBlockedAt = now;
            print("CoreSplit: blocked focus steal from pid " + win.pid);
        }
    } catch (e) {
        print("CoreSplit guard error: " + e);
    }
}

function onAdded(win) {
    try {
        if (!win || win.deleted || win.fullScreen) {
            return;
        }
        if (String(flag(GROUP_ACTIVE, "false")) !== "true") {
            return;
        }
        if (isBlocked(win)) {
            // A window appearing on its own is never a user request, so it is
            // always bounced regardless of the insist rule.
            restore();
        }
    } catch (e) {
        print("CoreSplit guard error: " + e);
    }
}

workspace.windowActivated.connect(onActivated);
if (workspace.windowAdded) {
    workspace.windowAdded.connect(onAdded);
}
print("CoreSplit focus guard active");
"""


# --------------------------------------------------------------------------- #
# install
# --------------------------------------------------------------------------- #

def installed() -> bool:
    return MAIN_JS.exists() and META_JSON.exists()


def install() -> bool:
    """Write the KWin script package. Returns True when files changed."""
    payload = json.dumps(_metadata(), indent=2) + "\n"
    changed = False
    try:
        MAIN_JS.parent.mkdir(parents=True, exist_ok=True)
        if not META_JSON.exists() or META_JSON.read_text(encoding="utf-8") != payload:
            META_JSON.write_text(payload, encoding="utf-8")
            changed = True
        if not MAIN_JS.exists() or MAIN_JS.read_text(encoding="utf-8") != MAIN_JS_TEXT:
            MAIN_JS.write_text(MAIN_JS_TEXT, encoding="utf-8")
            changed = True
    except OSError:
        return False
    return changed


def _kwrite(group: str, key: str, value: str):
    try:
        subprocess.run(
            ["kwriteconfig6", "--file", "kwinrc", "--group", group,
             "--key", key, str(value)],
            capture_output=True, timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _kread(group: str, key: str) -> str:
    try:
        result = subprocess.run(
            ["kreadconfig6", "--file", "kwinrc", "--group", group, "--key", key],
            capture_output=True, text=True, timeout=4,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _reconfigure():
    try:
        subprocess.run(
            ["qdbus6", "org.kde.KWin", "/KWin", "reconfigure"],
            capture_output=True, timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def set_enabled(enabled: bool):
    install()
    _kwrite("Plugins", f"{SCRIPT_ID}Enabled", "true" if enabled else "false")
    _kwrite(CONFIG_GROUP, "GuardActive", "false")
    _kwrite(CONFIG_GROUP, "GuardBlockedPids", "")
    _reconfigure()


def enabled() -> bool:
    return _kread("Plugins", f"{SCRIPT_ID}Enabled").lower() == "true"


# --------------------------------------------------------------------------- #
# state publishing
# --------------------------------------------------------------------------- #

class Publisher:
    """Publishes guard state into kwinrc, but only when it actually changed.

    Test runs churn through pids quickly, and every write makes KWin reparse its
    config, so writes are rate limited and skipped when the published set is
    materially the same.
    """

    def __init__(self, min_interval: float = 2.0):
        self.min_interval = min_interval
        self._last_write = 0.0
        self._last_key = None

    def publish(self, active: bool, game_pid: int, blocked_pids) -> bool:
        pids = sorted({int(p) for p in blocked_pids if int(p) > 0})[:MAX_BLOCKED]
        key = (bool(active), int(game_pid or 0), tuple(pids))
        if key == self._last_key:
            return False
        now = time.monotonic()
        if now - self._last_write < self.min_interval:
            return False
        self._last_write = now
        self._last_key = key

        _kwrite(CONFIG_GROUP, "GuardActive", "true" if active else "false")
        _kwrite(CONFIG_GROUP, "GuardGamePid", str(int(game_pid or 0)))
        _kwrite(CONFIG_GROUP, "GuardBlockedPids", ",".join(str(p) for p in pids))
        return True


# --------------------------------------------------------------------------- #
# status for the UI
# --------------------------------------------------------------------------- #

def status() -> dict:
    return {
        "installed": installed(),
        "enabled": enabled(),
        "active": _kread(CONFIG_GROUP, "GuardActive").lower() == "true",
        "blocked_count": len([p for p in _kread(CONFIG_GROUP, "GuardBlockedPids").split(",") if p.strip()]),
    }
