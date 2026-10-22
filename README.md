# CoreSplit

![CoreSplit window](docs/screenshot.png)

One CCD for the game, one for everything else — so tests, builds and AI agents
stop causing frame-time spikes. Includes a focus guard that stops agent windows
from stealing focus mid-game.

## Problem

A CPU affinity mask is a permission, not an exclusion. Pinning the game to a
CCD doesn't keep anything else off it: every unpinned process is free to wander
onto the game's cores, which is exactly where background tests and agents cause
frame-time spikes. And when an agent steals mouse focus mid-fight, frame time
stops mattering anyway.

## Solution

CoreSplit watches for a game (GameMode, install paths, launcher scopes) and
sweeps everything else onto the other CCD until the game exits.

- **Hard** — all of your processes move off the game CCD. Classifies nothing,
  misses nothing. The default.
- **Smart** — only processes recognised as tests or agents move.
- **Focus guard** — a KWin script bounces agent windows before they are ever
  drawn as focused. Your own alt-tab still works; insisting twice within three
  seconds gets you through.

Cores are grouped by L3 cache domain, not by number, so the 9900X's
non-contiguous layout (0-5 + 12-17 / 6-11 + 18-23) splits correctly and keeps
working if the hardware changes.

## Implementation

Python 3 + PySide6, no root, no system packages. An engine thread scans
`/proc`, classifies processes, and applies affinity masks directly; every pin
records its original mask and start time to `state.json`, so a killed session
restores cleanly and reused PIDs are never restored onto the wrong process.
The GUI talks to the engine over Qt signals; a Unix socket at
`$XDG_RUNTIME_DIR/coresplit/control.sock` lets the CLI drive a running
instance.

## Install

Requires Linux, KDE Plasma (for the focus guard), Python 3 with PySide6, and
`taskset` from util-linux.

```bash
git clone https://github.com/20plays/coresplit.git
cd coresplit
./install.sh
```

No root; everything lands in `~/.local`. Missing PySide6:
`pip install PySide6` or `pacman -S python-pyside6`.

## Usage

```bash
coresplit                     # open the window (lives in the tray on close)
coresplit smart|hard|off      # switch mode in a running instance
coresplit focusguard on|off   # toggle the focus guard
coresplit status              # print current state
coresplit detect              # show what the game detector sees
coresplit reset               # release every pin, even if the app is dead
```

`coresplit --tray` starts hidden (used by autostart); `coresplit daemon` runs
headless. For the most reliable pinning, the app's Settings page copies the
exact `taskset -c … gamemoderun %command%` line for your CPU to paste into a
game's Steam launch options, and installs a `testcpus` wrapper for one-off
runs: `testcpus cargo test`.

## License

MIT
