#!/usr/bin/env python3
"""Standalone regression checks for multi-game, boost mode and thread pinning.

Runs the real Detector and Engine._apply against synthetic process tables,
with affinity/priority syscalls recorded instead of applied. The final check
spawns a real child process to prove that escaped game threads are swept back.

    python3 tests/multigame_test.py
"""

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import engine as eng
from detect import Detector

fails = []


def check(cond, message):
    print(("PASS " if cond else "FAIL ") + message, flush=True)
    if not cond:
        fails.append(message)


# -- synthetic 9900X topology ----------------------------------------------- #

class FakeTopo:
    def __init__(self):
        self.all_cpus = list(range(24))
        self.ccds = [list(range(0, 6)) + list(range(12, 18)),
                     list(range(6, 12)) + list(range(18, 24))]
        self.siblings = {cpu: sorted([cpu, (cpu + 12) % 24]) for cpu in range(24)}

    def physical_cores(self):
        return [[cpu, cpu + 12] for cpu in range(6)] + [[cpu - 12, cpu] for cpu in range(18, 24)]

    def core_count(self, cpus):
        wanted = set(cpus)
        return sum(1 for group in self.physical_cores() if wanted.intersection(group))

    def split_by_core_count(self, game_cores):
        cores = self.physical_cores()
        game, test = set(), set()
        for index, group in enumerate(cores):
            (game if index < game_cores else test).update(group)
        return game, test

    def split_side(self, cpus, parts):
        return eng.Topology.split_side(self, cpus, parts)

    def smt_split(self, cpus):
        wanted = set(cpus)
        return sorted(cpu for cpu in wanted
                      if any(sib not in wanted for sib in self.siblings.get(cpu, [cpu])))

    def default_split(self):
        game = set(self.ccds[0])
        return game, set(self.all_cpus) - game


def proc(pid, comm, ppid, exe, cmdline, cgroup, cpu=0.0, uid=None):
    return eng.ProcInfo(pid=pid, comm=comm, state="S", ppid=ppid,
                        uid=os.getuid() if uid is None else uid,
                        starttime=pid * 37, threads=4, nice=0, cgroup=cgroup,
                        exe=exe, cmdline=cmdline, argv=tuple(cmdline.split()),
                        ticks=pid * 11, affinity=frozenset(range(24)), cpu=cpu)


STEAM = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-steam-12345678.scope"
GSCOPE_DL = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-gamescope-111.scope"
GSCOPE_TF = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-gamescope-222.scope"

DL_EXE = "/home/u/.steam/steam/steamapps/common/Dying Light Enhanced Edition/DyingLightGame_x64"
TF_EXE = "/home/u/.steam/steam/steamapps/common/The Finals/AlphaPause/Binaries/TheFinals"


def two_game_procs(dl_cpu=2.0, tf_cpu=0.5):
    return {
        100: proc(100, "steam", 1, "/usr/bin/steam", "steam", STEAM),
        200: proc(200, "gamescope", 100, "/usr/bin/gamescope",
                  f"gamescope --steam -- {DL_EXE}", STEAM),
        201: proc(201, "DyingLightGame", 200, DL_EXE, DL_EXE, GSCOPE_DL, cpu=dl_cpu),
        300: proc(300, "gamescope", 100, "/usr/bin/gamescope",
                  f"gamescope --steam -- {TF_EXE}", STEAM),
        301: proc(301, "TheFinals", 300, TF_EXE, TF_EXE, GSCOPE_TF, cpu=tf_cpu),
        400: proc(400, "cargo", 1, "/usr/bin/cargo", "cargo test",
                  "0::/app-zcode.service"),
    }


def make_engine():
    """An Engine without its thread, wired to recording stubs."""
    engine = eng.Engine.__new__(eng.Engine)
    engine.cfg = eng.Config()
    engine.cfg["game_cpus"] = "0-5,12-17"
    engine.cfg["test_cpus"] = "6-11,18-23"
    engine.cfg["favour_foreground"] = True
    engine.cfg["game_priority"] = True
    engine.topo = FakeTopo()
    engine._state = eng.EngineState()
    engine._state.mode = "hard"
    engine._warned = set()
    engine._promote_streak = 0
    engine._slice_sig = None
    engine._detector = Detector(engine.cfg, engine.topo)
    engine._dirty = False
    engine.logs = []
    engine._log = lambda line: engine.logs.append(line)
    engine.pins = {}       # pid -> mask from set_affinity
    engine.nices = []      # (pid, value) from os.setpriority

    def record_affinity(pid, mask):
        engine.pins[pid] = set(mask)
        return len(mask)

    engine._saved_affinity = eng.set_affinity
    engine._saved_setpriority = os.setpriority
    eng.set_affinity = record_affinity
    os.setpriority = lambda which, who, prio: engine.nices.append((who, prio))
    eng._set_ioprio = lambda *a, **k: None
    return engine


def teardown(engine):
    eng.set_affinity = engine._saved_affinity
    os.setpriority = engine._saved_setpriority


def main():
    # -- detector: both games confirmed, extras fresh ----------------------- #
    det = Detector(eng.Config(), FakeTopo())
    procs = two_game_procs()
    game = None
    for i in range(4):
        game = det.update(procs, 1000.0 + i)
    check(game is not None and game.pid == 200, "detector confirms a primary game")
    check(300 in getattr(game, "extra_roots", ()), "the second game rides as an extra root")
    check(set(det.names()) == {200, 300}, "detector knows both games' names")

    # -- detector: root-order flip does not reset or lose games ------------- #
    det._gamemode_cache = {300}  # the finals outranks via gamemode on this poll
    game = det.update(procs, 1005.0)
    check(game.pid == 200, "primary stays confirmed when ordering flips")
    check(300 in game.extra_roots, "extras still include the other game after a flip")

    # -- detector: primary exits, the other game takes over ----------------- #
    procs.pop(200)
    procs.pop(201)
    game = det.update(procs, 1006.0)
    check(game is not None and game.pid == 300, "the surviving game becomes primary")

    # -- engine: slices, game set, background nice --------------------------- #
    engine = make_engine()
    procs = two_game_procs(dl_cpu=2.0, tf_cpu=0.5)  # DL plays harder -> primary
    for i in range(4):
        game = engine._detector.update(procs, 1000.0 + i)
    engine._state.game = game
    engine._apply(procs)

    check(engine._state.game_set == {200, 201, 300, 301},
          "both games' processes are on the game side")
    check(engine._state.roles.get(400) == "test", "the test process stays on the test side")

    dl_mask = engine.pins.get(201, set())
    tf_mask = engine.pins.get(301, set())
    game_cpus = eng.parse_cpulist("0-5,12-17")
    check(dl_mask and dl_mask <= game_cpus, "DL pinned inside the game side")
    check(tf_mask and tf_mask <= game_cpus, "The Finals pinned inside the game side")
    check(dl_mask | tf_mask == game_cpus and not (dl_mask & tf_mask),
          "the two games get disjoint slices covering the side")
    check(len(dl_mask) == 6 and len(tf_mask) == 6, "even 6/6 split for 6/6 physical cores")
    check(all(engine.pins[p] == dl_mask for p in (200, 201)),
          "DL's whole tree shares its slice")
    check(all(engine.pins[p] == tf_mask for p in (300, 301)),
          "The Finals' whole tree shares its slice")

    reniced = {pid for pid, value in engine.nices if value == 5}
    check(reniced == {300, 301}, f"only the background game is reniced ({reniced})")
    check((400, 19) in engine.nices, "parked work is reniced for the game's sake")
    check(any(pid == 201 and value == -5 for pid, value in engine.nices),
          "the game itself gets the best-effort priority raise")
    check(any("share the game side" in line for line in engine.logs),
          "the slice layout is logged once")

    # -- engine: the played game gets promoted ------------------------------ #
    engine.nices.clear()
    engine._promote_streak = 0
    procs = two_game_procs(dl_cpu=2.0, tf_cpu=420.0)  # finals is being played now
    for _ in range(3):
        engine._follow_foreground(procs)
    check(engine._state.game.pid == 300,
          "the CPU-hungry game is promoted to primary")
    engine._apply(procs)
    restored = {pid for pid, value in engine.nices if value == 0}
    reniced = {pid for pid, value in engine.nices if value == 5}
    check(reniced == {200, 201}, f"the background game is now reniced ({reniced})")
    check(restored == {300, 301}, f"the promoted game gets its nice back ({restored})")

    # -- engine: single game keeps the whole side, plus the boost ----------- #
    engine2 = make_engine()
    procs = two_game_procs()
    for pid in (300, 301):
        procs.pop(pid)
    for i in range(4):
        game = engine2._detector.update(procs, 1000.0 + i)
    engine2._state.game = game
    engine2._apply(procs)
    check(engine2.pins.get(201, set()) == eng.parse_cpulist("0-5,12-17"),
          "a single game keeps the whole game side")
    check((400, 19) in engine2.nices,
          "even with one game, parked work runs at the lowest priority")
    check(any(pid == 201 and value == -5 for pid, value in engine2.nices),
          "even with one game, the game gets the best-effort raise")

    # -- engine: the boost switch turns all of it off ----------------------- #
    engine4 = make_engine()
    engine4.cfg["game_priority"] = False
    procs = two_game_procs()
    for i in range(4):
        game = engine4._detector.update(procs, 1000.0 + i)
    engine4._state.game = game
    engine4._apply(procs)
    check(not any(value == 19 for _pid, value in engine4.nices),
          "no renice with the boost switch off")
    check(not any(value == -5 for _pid, value in engine4.nices),
          "no game raise with the boost switch off")

    # -- detector: real The Finals shape under the steam @service ------------ #
    SVC = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-steam@d2121b.service"
    tf = {
        500: proc(500, "steam", 1, "/usr/bin/steam", "steam", SVC),
        501: proc(501, "reaper", 500,
                  "/home/u/.local/share/Steam/ubuntu12_32/reaper",
                  "reaper SteamLaunch AppId=2073850 -- /home/u/.steam/steamapps/common/The Finals/Discovery.exe", SVC),
        502: proc(502, "python3", 501, "/usr/bin/python3.13",
                  "/home/u/.steam/steamapps/common/Proton - Experimental/proton waitforexitandrun "
                  "/home/u/media/SAMSUNG/SteamLibrary/steamapps/common/The Finals/Discovery.exe", SVC),
        503: proc(503, "GameThread", 502,
                  "/home/u/.steam/steamapps/common/Proton - Experimental/files/lib/wine64-preloader",
                  "S:\\steamapps\\common\\The Finals\\Discovery\\Binaries\\Win64\\Discovery.exe", SVC, cpu=400.0),
        504: proc(504, "wineserver", 1,
                  "/home/u/.steam/steamapps/common/Proton - Experimental/files/bin/wineserver",
                  "wineserver", SVC),
    }
    det2 = Detector(eng.Config(), FakeTopo())
    game = None
    for i in range(4):
        game = det2.update(tf, 2000.0 + i)
    check(game is not None and game.name == "The Finals",
          f"the real Proton/steam-service shape is detected ({game and game.name})")
    check(len(getattr(game, "extra_roots", ()) or tuple()) == 0,
          "wine helpers do not become extra roots")
    check(det2.names().get(game.pid) == "The Finals" if game else False,
          "the display name is the title, not the runtime")
    engine3 = make_engine()
    engine3._state.game = det2.current()
    engine3._apply(tf)
    check(engine3._state.game_set >= {502, 503, 504},
          "wineserver and the game process are adopted game-side")
    game_mask = eng.parse_cpulist("0-5,12-17")
    test_mask = eng.parse_cpulist("6-11,18-23")
    check(all(engine3.pins.get(p) == game_mask for p in (502, 503, 504)),
          "the game tree holds the whole side")
    check(engine3.pins.get(500) in (None, test_mask),
          "the steam client itself is not treated as game")

    # -- engine: boost mode prioritises without pinning --------------------- #
    engine5 = make_engine()
    engine5._state.mode = "boost"
    procs = two_game_procs()
    for i in range(4):
        game = engine5._detector.update(procs, 1000.0 + i)
    engine5._state.game = game
    engine5._apply(procs)
    check(engine5.pins == {} and len(engine5._state.pinned) == 0,
          "boost mode pins nothing")
    check(engine5._state.game_set == {200, 201, 300, 301},
          "boost still tracks the game side")
    check((400, 19) in engine5.nices and (100, 19) in engine5.nices,
          "everything except the games yields, even the steam client")
    check(not any(pid in (200, 201) and value == 19 for pid, value in engine5.nices),
          "the played game's tree is never reniced to 19")
    check(any(pid == 300 and value == 5 for pid, value in engine5.nices),
          "a background game still yields to the played one in boost")
    check("boost: the game outranks everything" in " ".join(engine5.logs),
          "boost engagement is logged once")

    engine5.nices.clear()
    engine5._state.game = None
    engine5._apply(procs)
    check(not engine5._state.boost_nice and not engine5._state.boost_active,
          "boost releases everything when the game exits")
    check(any(value == 0 for _pid, value in engine5.nices),
          "original nices are restored on game exit")

    # -- engine: boost releases on a mode switch too ------------------------ #
    engine6 = make_engine()
    engine6._state.mode = "boost"
    procs = two_game_procs()
    for i in range(4):
        game = engine6._detector.update(procs, 1000.0 + i)
    engine6._state.game = game
    engine6._apply(procs)
    engine6.nices.clear()
    engine6._state.mode = "hard"
    engine6._restore_all("switch to hard")
    check(not engine6._state.boost_nice, "switching modes releases the boost")
    check(any(value == 0 for _pid, value in engine6.nices),
          "nices restored on mode switch")

    # -- engine: single-game boost is the headline feature ------------------ #
    engine7 = make_engine()
    engine7._state.mode = "boost"
    procs = two_game_procs()
    for pid in (300, 301):
        procs.pop(pid)
    for i in range(4):
        game = engine7._detector.update(procs, 1000.0 + i)
    engine7._state.game = game
    engine7._apply(procs)
    check(engine7.pins == {}, "single-game boost pins nothing")
    check((400, 19) in engine7.nices,
          "even with one game, everything else yields in boost")
    check(any(pid == 201 and value == -5 for pid, value in engine7.nices),
          "the game gets the best-effort raise in boost")

    # -- threads: game-side affinity drift is swept back -------------------- #
    child_code = (
        "import threading, time, os\n"
        "def worker():\n"
        "    while True:\n"
        "        time.sleep(0.3)\n"
        "        os.sched_setaffinity(0, set(range(24)))\n"
        "threading.Thread(target=worker, daemon=True).start()\n"
        "print(os.getpid(), flush=True)\n"
        "time.sleep(20)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", child_code],
                             stdout=subprocess.PIPE, text=True)
    child_pid = int(child.stdout.readline().strip())

    def child_threads():
        return [int(t) for t in os.listdir(f"/proc/{child_pid}/task")]

    engine8 = make_engine()
    eng.set_affinity = engine8._saved_affinity  # real syscalls for this one
    procs = two_game_procs()
    procs[child_pid] = proc(child_pid, "GameThread", 201, DL_EXE, DL_EXE, GSCOPE_DL)
    for i in range(4):
        game = engine8._detector.update(procs, 1000.0 + i)
    engine8._state.game = game
    engine8._apply(procs)
    time.sleep(0.8)  # the worker re-widens itself every 0.3s
    drifted = [t for t in child_threads() if set(os.sched_getaffinity(t)) - game_cpus]
    check(drifted, "fixture: the game's own thread escaped the game side")
    engine8._thread_sweep = 3
    engine8._apply(procs)
    escaped = [t for t in child_threads() if set(os.sched_getaffinity(t)) - game_cpus]
    check(not escaped, f"the sweep pulls escaped game threads back ({escaped})")
    child.kill()
    child.wait()

    for e in (engine, engine2, engine3, engine4, engine5, engine6, engine7, engine8):
        teardown(e)
    print(f"FAILURES: {len(fails)}", flush=True)
    for f in fails:
        print("  -", f, flush=True)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
