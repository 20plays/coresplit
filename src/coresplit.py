#!/usr/bin/env python3
"""CoreSplit — keep a game on one CCD while tests and agents run on the other.

Usage:
    coresplit                 open the GUI
    coresplit --tray          start in the tray (used by autostart)
    coresplit show            raise the window (ignored while a game is protected)
    coresplit focusguard on|off   block agent windows from stealing focus
    coresplit status          print current state as JSON
    coresplit smart|hard|off  switch mode in a running instance
    coresplit reset           release every pin (works without the app running)
    coresplit detect          show what the game detector currently sees
    coresplit daemon          headless mode, no GUI
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import engine as eng
import detect as det

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "coresplit"
SOCKET_PATH = RUNTIME_DIR / "control.sock"


# --------------------------------------------------------------------------- #
# ipc
# --------------------------------------------------------------------------- #

def ipc_send(command: str, timeout: float = 2.0):
    """Send one command to a running instance. Returns the reply dict or None."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(SOCKET_PATH))
            sock.sendall((command.strip() + "\n").encode())
            chunks = []
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
                if len(b"".join(chunks)) > 4_000_000:
                    break
            raw = b"".join(chunks).decode("utf-8", "replace").strip()
            return json.loads(raw) if raw else None
    except (OSError, ValueError):
        return None


class IpcServer:
    """Tiny line-protocol control socket. Runs on its own thread."""

    def __init__(self, handler):
        self._handler = handler
        self._sock = None
        self._thread = None
        self._stop = False

    def start(self):
        import threading
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        # Never steal a socket from a live instance: unlinking it would leave the
        # first process running with no way to reach it. A socket file that exists
        # but refuses connections is stale, and only then is it removed.
        if SOCKET_PATH.exists():
            if ipc_send("status", timeout=0.4) is not None:
                raise OSError("another instance is running")
            try:
                SOCKET_PATH.unlink()
            except OSError:
                pass
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(SOCKET_PATH))
        os.chmod(SOCKET_PATH, 0o600)
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        self._thread = threading.Thread(target=self._serve, name="coresplit-ipc", daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                with conn:
                    conn.settimeout(3.0)
                    raw = b""
                    while not raw.endswith(b"\n"):
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        raw += chunk
                    command = raw.decode("utf-8", "replace").strip()
                    reply = self._handler(command) if command else {}
                    conn.sendall((json.dumps(reply) + "\n").encode())
            except (OSError, ValueError):
                continue

    def stop(self):
        self._stop = True
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        try:
            SOCKET_PATH.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# gui
# --------------------------------------------------------------------------- #

def run_gui(config, start_hidden=False):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    import ui
    import guard

    app = QApplication(sys.argv)
    ui.apply_theme(app)
    app.setApplicationName("CoreSplit")
    app.setApplicationDisplayName("CoreSplit")
    app.setDesktopFileName("coresplit")
    app.setQuitOnLastWindowClosed(False)

    bridge = ui.Bridge()
    engine = eng.Engine(config, on_snapshot=bridge.snapshot.emit, on_log=bridge.log.emit)
    window = ui.MainWindow(engine, bridge, config)
    window.setWindowIcon(ui.make_icon("off"))
    tray = ui.TrayController(app, window, engine, config)

    # Focus guard: a KWin script that CoreSplit feeds through kwinrc.
    guard_ready = False
    if config["focus_guard"]:
        guard.set_enabled(True)
        guard_ready = guard.installed() and guard.enabled()
        engine.attach_publisher(guard.Publisher())
    window.set_guard_status(guard_ready)

    def handle_command(command):
        parts = command.split()
        verb = parts[0].lower() if parts else "status"
        if verb in eng.MODES:
            engine.set_mode(verb, manual=True)
            return {"ok": True, "mode": verb}
        if verb == "reset":
            engine.request("reset")
            return {"ok": True, "mode": "off"}
        if verb == "show":
            # Never pull focus away from a protected game, even for our own
            # window. The tray is the user-initiated path.
            if engine.guard_active():
                return {"ok": True, "suppressed": True,
                        "message": "focus guard is protecting the game"}
            bridge.show_requested.emit()
            return {"ok": True}
        if verb == "focusguard":
            value = parts[1].lower() if len(parts) > 1 else "on"
            enabled = value in ("on", "true", "1", "yes")
            config["focus_guard"] = enabled
            config.save()
            guard.set_enabled(enabled)
            if enabled:
                engine.attach_publisher(guard.Publisher())
            else:
                engine.attach_publisher(None)
            window.set_guard_status(enabled and guard.enabled())
            return {"ok": True, "focus_guard": enabled}
        if verb == "hide":
            window.hide()
            return {"ok": True}
        if verb == "quit":
            engine.stop()
            tray.tray.hide()
            app.quit()
            return {"ok": True}
        return {"ok": True, "status": engine.status()}

    server = IpcServer(handle_command)
    try:
        server.start()
    except OSError:
        # Another instance already owns the control socket. Two engines pinning at
        # once would fight over the same processes, so hand the request over and
        # step aside instead of running a second copy.
        if ipc_send("show"):
            print("CoreSplit is already running; raised its window.")
            return 0
        print("Another CoreSplit instance owns the control socket but is not "
              "responding. Remove it and retry:\n"
              f"  rm -f {SOCKET_PATH}", file=sys.stderr)
        return 1

    bridge.snapshot.connect(lambda snap: tray.update(snap["mode"]))
    bridge.show_requested.connect(window.raise_window, Qt.QueuedConnection)

    engine.start()
    if start_hidden:
        tray.tray.showMessage(
            "CoreSplit", "Running in the tray. Gaming mode engages automatically.",
            tray.tray.icon(), 4000,
        )
    else:
        window.show()

    app.aboutToQuit.connect(lambda: (engine.stop(), server.stop()))
    try:
        return app.exec()
    finally:
        engine.stop()
        server.stop()


# --------------------------------------------------------------------------- #
# headless
# --------------------------------------------------------------------------- #

def run_daemon(config):
    engine = eng.Engine(config, on_log=lambda line: print(line, flush=True))
    engine.start()
    print("coresplit daemon running; Ctrl-C to stop", flush=True)

    stop = False

    def _handle(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    try:
        while not stop:
            time.sleep(0.5)
    finally:
        engine.stop()
    return 0


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #

def print_status(snapshot):
    if not snapshot:
        print("CoreSplit is not running.")
        return 1
    game = snapshot.get("game")
    print(f"mode        {snapshot['mode']}")
    print(f"auto        {'on' if snapshot['auto'] else 'off'} "
          f"(engages {snapshot['auto_mode']})")
    print(f"game cpus   {snapshot['game_cpus']}")
    print(f"test cpus   {snapshot['test_cpus']}")
    counts = snapshot["counts"]
    print(f"pinned      {counts.get('pinned', 0)} "
          f"({counts.get('game', 0)} game, {counts.get('test', 0)} test)")
    if game:
        print(f"game        {game['name']} (pid {game['pid']}, via {game['reason']})")
    else:
        print("game        none detected")
    return 0


def main(argv):
    args = [arg for arg in argv[1:] if not arg.startswith("--")]
    flags = {arg for arg in argv[1:] if arg.startswith("--")}
    command = args[0].lower() if args else ""

    config = eng.Config.load()
    topo = eng.Topology.read()
    if config.ensure_split(topo):
        config.save()

    if command in ("", "gui"):
        return run_gui(config, start_hidden="--tray" in flags)

    if command == "daemon":
        return run_daemon(config)

    if command == "reset":
        reply = ipc_send("reset")
        if reply:
            print("All pins released.")
            return 0
        restored = eng.reset_standalone()
        print(f"Released {restored} pinned process(es) from a previous session.")
        return 0

    if command == "status":
        reply = ipc_send("status")
        if reply and "status" in reply:
            return print_status(reply["status"])
        print("CoreSplit is not running.")
        return 1

    if command == "detect":
        reply = ipc_send("status")
        if reply and "status" in reply:
            game = reply["status"].get("game")
            if game:
                print(f"{game['name']} (pid {game['pid']}, via {game['reason']})")
            else:
                print("No game detected.")
            return 0
        procs = eng.scan_processes(os.getpid())
        detector = det.Detector(config)
        info = detector.update(procs, time.monotonic())
        if info:
            print(f"{info.name} (pid {info.pid}, via {info.reason})")
            return 0
        print("No game detected.")
        return 1

    if command == "show":
        reply = ipc_send("show")
        if reply:
            if reply.get("suppressed"):
                print("Focus guard is protecting the game; not raising the window.")
            return 0
        print("CoreSplit is not running. Start it with: coresplit", file=sys.stderr)
        return 1

    if command == "focusguard":
        value = args[1].lower() if len(args) > 1 else "on"
        enabled = value in ("on", "true", "1", "yes")
        reply = ipc_send(f"focusguard {value}")
        if reply:
            print(f"Focus guard {'enabled' if enabled else 'disabled'}.")
            return 0
        import guard
        config["focus_guard"] = enabled
        config.save()
        guard.set_enabled(enabled)
        print(f"Focus guard {'enabled' if enabled else 'disabled'} "
              "(applies immediately; CoreSplit is not running).")
        return 0

    if command in eng.MODES:
        reply = ipc_send(command)
        if reply:
            print(f"Mode set to {reply.get('mode', command)}.")
            return 0
        print("CoreSplit is not running. Start it first, or run the GUI.", file=sys.stderr)
        return 1

    print(__doc__.strip())
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
