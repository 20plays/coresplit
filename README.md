# CoreSplit

Keep a game on one CCD while background test and agent workloads run on the
other, so running tests stops causing frame-time spikes. Plus a focus guard that
stops automated tooling from stealing your mouse and keyboard focus mid-game.

Built for a Ryzen 9 9900X (12 cores / 24 threads, two CCDs of 6 cores), but the
topology is discovered from sysfs, so it adapts to other multi-CCD AMD chips.

![CoreSplit window](docs/screenshot.png)

## The core idea

A CPU affinity mask is a **permission, not an exclusion**. If the game is pinned
to CPUs `0-5,12-17`, any process that is *not* pinned away may still run there.
That single fact determines the design:

| Mode | Game | Everything else | Guarantee |
|---|---|---|---|
| **Smart** | `0-5,12-17` | Processes identified as tests/agents → `6-11,18-23` | Best-effort. Relies on classification. |
| **Hard** | `0-5,12-17` | *All* other processes of yours → `6-11,18-23` | Hard separation. Classifies nothing. |

Hard mode never needs to recognise an agent process, so it cannot miss one. It is
the default for auto-engage.

Both modes keep the frame and audio path (`kwin_wayland`, `Xwayland`, `pipewire`,
`wireplumber`) on the game CCD, because moving those would cost more than it
saves.

## CPU layout

On a 9900X the two CCDs are **not** contiguous CPU ranges:

```
CCD 1:  0-5   + SMT siblings 12-17     ← game
CCD 2:  6-11  + SMT siblings 18-23     ← tests / everything else
```

Splitting `0-11` vs `12-23` would split each physical core across the two
workloads, which is exactly what you do not want. CoreSplit groups CPUs by their
L3 cache domain instead of by number, so it gets this right and keeps working if
the hardware changes.

## Install

Requires Linux, KDE Plasma (for the focus guard), Python 3 with PySide6, and
`taskset` from util-linux.

```bash
git clone https://github.com/20plays/coresplit.git
cd coresplit
./install.sh
```

No root, no system packages. Everything lands in `~/.local`.

If PySide6 is missing: `pip install PySide6` or `pacman -S python-pyside6`.

## Usage

```bash
coresplit                     # open the window
coresplit --tray              # start hidden in the tray (used by autostart)
coresplit show                # raise the window (ignored while a game is protected)
coresplit smart|hard|off      # switch mode in a running instance
coresplit focusguard on|off   # toggle the focus guard
coresplit status              # print current state
coresplit reset               # release every pin, even if the app is not running
coresplit detect              # show what the game detector currently sees
coresplit daemon              # headless, no GUI
```

The tray icon is gray when off, amber in Hard mode, blue in Smart mode.

### Automatic engagement

When *Enable automatically when a game starts* is on, the app polls every 1.5 s
and enters the configured mode after a game is seen for 3 consecutive polls. It
leaves after 20 s with no game, which covers alt-tab, loading screens and
launcher handoff.

Detection signals, in priority order:

1. **gamemode** — `com.feralinteractive.GameMode.ListClients`. Most precise, but
   only works for games launched through `gamemoderun`.
2. **install path** — executables under `steamapps/common`, `compatdata`, or
   Heroic/Lutris install dirs, plus known emulators. Steam runtimes and Proton
   directories are excluded, since they are not games.
3. **launcher scope** — descendants of a Steam/Lutris/Heroic systemd scope,
   excluding the launcher's own UI and plumbing.
4. **KWin active window** — off by default; a fallback for games started outside
   a launcher.

## Focus guard

Automated work should never pull focus out of a game.

A KWin script watches window activation. If a window belonging to an **agent**
process is activated while a game is protected, focus is handed straight back to
the game, before the window is ever drawn as focused.

- Only **agent** processes are barred. Alt-tabbing to your browser, terminal or
  file manager still works.
- A window that appears on its own is always bounced.
- Activating a blocked window **twice within three seconds** is allowed through.
  One programmatic focus grab gets blocked; insisting means you actually want it.
- `coresplit show` refuses to raise CoreSplit's own window while a game is
  protected.

Toggle from **Advanced → Behaviour**, the tray menu, or
`coresplit focusguard on|off`. On by default.

The script installs to `~/.local/share/kwin/scripts/coresplit-focus-guard/` and
is enabled through `kwinrc`, so it survives reboots and appears in System
Settings → Window Management → KWin Scripts. State is published into the
`[Script-coresplit-focus-guard]` group of `kwinrc`, which is the only channel
KWin's scripting sandbox can read.

## Getting the most reliable game pinning

Add this to Steam → Properties → Launch Options (the Advanced tab copies the
exact line for your CPU):

```
taskset -c 0-5,12-17 gamemoderun %command%
```

Auto-detection also works, but a launch option removes the few seconds at
startup where the game is not yet pinned.

For a one-off test run outside the app:

```bash
testcpus cargo test
testcpus pytest -n 6
testcpus make -j6
```

`testcpus` (install it from the app's Advanced tab) pins to the test CCD, lowers
priority, and sets `CARGO_BUILD_JOBS`, `RAYON_NUM_THREADS`, `GOMAXPROCS` and
`MAKEFLAGS` to the test core count. Most tools read the affinity mask and see 12
logical CPUs automatically; Python's `os.cpu_count()` does not, which is why the
wrapper sets the variables explicitly.

## Safety behaviour

- Only processes owned by your user are touched. PID 1, root processes and
  CoreSplit itself are never modified.
- Every pinned process records its original affinity and start time. On restore,
  entries whose PID now belongs to a different process are skipped.
- `state.json` is rewritten as pins are applied. If the app is killed with
  `SIGKILL`, the next start restores those pins and reports it.
- On release, an orphan sweep unpins children that inherited a CCD mask but were
  never individually recorded.
- Processes holding file capabilities (`kwin_wayland`, `gsr-kms-server`) refuse
  affinity changes with `EPERM` even for their owner. They are left unpinned and
  reported once rather than retried forever.

## Architecture

```
src/coresplit.py    entry point, IPC control socket, CLI
src/engine.py       topology, process scanning, classification, pinning, state
src/detect.py       game detection signals and hysteresis
src/guard.py        focus guard: KWin script install and state publishing
src/theme.py        palette, typography, stylesheet, icon painting
src/ui.py           window, process table, CPU diagram, tray
```

The engine runs on its own thread and talks to the GUI through Qt signals. A
Unix socket at `$XDG_RUNTIME_DIR/coresplit/control.sock` lets the CLI drive a
running instance; a second launch hands off to the first and exits rather than
starting a competing engine.

## Caveats

- Hard mode parks your whole user session on 6 cores while gaming. That is the
  point, but heavy desktop work (video export, large compiles) will be slower
  until the game exits.
- If a game needs more than 6 cores, use the **Game-heavy 8/4** preset. Tests
  still get whole physical cores.
- `nice` and `ionice` are best-effort. Affinity is the actual guarantee.
- Anti-cheat titles are unaffected by affinity, but if one misbehaves, select it
  in the process table and press **Release**.
- The focus guard needs KDE Plasma. On other desktops the CPU split works and the
  guard toggle reports that it is inactive.

## License

MIT
