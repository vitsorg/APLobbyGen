r"""Launch the local tracker bridge against a seed this app is hosting.

The bridge lives in its own project (ArchipelagoRaceTracker). It connects to a
running Archipelago server as a spectator slot, rebuilds the real logic graph
from the installed apworlds, and serves a dashboard on localhost - which is the
thing that answers "what can I actually check right now".

This module only starts, stops and points it at the right room. It owns none of
the tracker's behaviour, and it never installs or modifies that project.

Slots come from the run lock of the seed being hosted, so the tracker always
watches the game that is actually up rather than whatever was typed last time -
the failure that left a bridge hammering a server for a slot that no longer
existed.

    python tracker.py --run runs\20261001-213148 --ap-port 38281

Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BRIDGE = r"C:\Projects\ArchipelagoRaceTracker\bridge"
DEFAULT_HTTP_PORT = 8081
SCRIPT = "bridge_server.py"


class TrackerError(Exception):
    pass


def find_bridge(root: str | None = None) -> tuple[str, str]:
    """(python, script) for the bridge, or a refusal naming what is missing.

    The bridge has its own virtualenv with its own dependency set; using this
    app's interpreter instead would fail on imports that are deliberately not
    installed here.
    """
    root = root or DEFAULT_BRIDGE
    script = os.path.join(root, SCRIPT)
    if not os.path.isfile(script):
        raise TrackerError(f"no {SCRIPT} under {root}")
    venv = os.path.join(root, "venv", "Scripts", "python.exe")
    if not os.path.isfile(venv):
        raise TrackerError(f"the bridge has no venv at {venv} - create it first")
    return venv, script


def slots_from_lock(run_dir: str) -> list[str]:
    """["Name:Game", ...] for every player in that run's lock.

    The lock is the authority on who is in a seed, so the tracker cannot be
    pointed at a slot the seed does not contain.
    """
    path = os.path.join(run_dir, "run.lock.json")
    try:
        with open(path, encoding="utf-8") as fh:
            lock = json.load(fh)
    except (OSError, ValueError) as exc:
        raise TrackerError(f"cannot read {path}: {exc}")
    slots = [f"{p['player']}:{p['game']}" for p in lock.get("players", [])
             if p.get("player") and p.get("game")]
    if not slots:
        raise TrackerError(f"{path} lists no players")
    return slots


def sync_worlds(run_dir: str, bridge_root: str | None = None,
                ap_dir: str | None = None) -> list[str]:
    """Copy the apworlds this seed used into the bridge's own checkout.

    The bridge builds its logic graph from ITS custom_worlds, not from the
    Archipelago install, so a game whose apworld is missing there connects fine
    and then reports no reachability at all - "live feed still works, Blockers
    can't". That is the one thing the tracker is wanted for, so the files are
    matched by content before every run.

    Returns the names copied. Copying is additive; nothing is deleted.
    """
    import shutil
    bridge_root = bridge_root or DEFAULT_BRIDGE
    dest_dir = os.path.join(bridge_root, "ap-src", "custom_worlds")
    if not os.path.isdir(dest_dir):
        raise TrackerError(f"the bridge has no custom_worlds at {dest_dir}")
    try:
        with open(os.path.join(run_dir, "run.lock.json"), encoding="utf-8") as fh:
            worlds = json.load(fh).get("worlds", {})
    except (OSError, ValueError) as exc:
        raise TrackerError(f"cannot read the run lock: {exc}")

    import aplobby as core
    ap_dir = ap_dir or core.AP_DEFAULT
    copied = []
    for game, w in worlds.items():
        src = os.path.join(ap_dir,
                           "custom_worlds" if w.get("source") == "custom"
                           else os.path.join("lib", "worlds"),
                           w["file"])
        if not os.path.isfile(src):
            continue
        dest = os.path.join(dest_dir, w["file"])
        if os.path.isfile(dest) and core.sha256(open(dest, "rb").read()) == w.get("sha256"):
            continue
        shutil.copyfile(src, dest)
        copied.append(w["file"])
    return copied


def bytecode_mismatch(run_dir: str, bridge_root: str | None = None,
                      ap_dir: str | None = None) -> list[dict]:
    """Worlds the bridge cannot import because their .pyc targets another Python.

    Several apworlds ship only compiled modules. A .pyc carries a magic number
    tied to the exact Python that produced it, so a world built by the frozen
    3.13 Archipelago cannot be imported by the bridge's 3.12 venv at all. The
    bridge survives it - the live feed still works - but reachability silently
    goes away, which is the one thing a tracker is wanted for.

    Reporting this before launch turns a confusing empty panel into a sentence.
    """
    import binascii
    bridge_root = bridge_root or DEFAULT_BRIDGE
    python, _script = find_bridge(bridge_root)
    out = subprocess.run(
        [python, "-c",
         "import importlib.util,binascii,sys;"
         "print(binascii.hexlify(importlib.util.MAGIC_NUMBER).decode());"
         "print('.'.join(map(str,sys.version_info[:3])))"],
        capture_output=True, text=True, timeout=60)
    want, bridge_ver = (out.stdout.strip().splitlines() + ["", "?"])[:2]

    import zipfile
    import aplobby as core
    ap_dir = ap_dir or core.AP_DEFAULT
    try:
        with open(os.path.join(run_dir, "run.lock.json"), encoding="utf-8") as fh:
            worlds = json.load(fh).get("worlds", {})
    except (OSError, ValueError):
        return []

    bad = []
    for game, w in worlds.items():
        src = os.path.join(ap_dir,
                           "custom_worlds" if w.get("source") == "custom"
                           else os.path.join("lib", "worlds"), w["file"])
        if not os.path.isfile(src):
            continue
        try:
            z = zipfile.ZipFile(src)
            pyc = next((n for n in z.namelist() if n.endswith(".pyc")), None)
            if not pyc:
                continue                      # ships source: any Python can read it
            got = binascii.hexlify(z.read(pyc)[:4]).decode()
        except Exception:
            continue
        if want and got != want:
            bad.append({"game": game, "file": w["file"], "magic": got,
                        "bridge_magic": want, "bridge_python": bridge_ver})
    return bad


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return False
        except OSError:
            return True


class Bridge:
    """A running tracker bridge, with its output on a callback."""

    def __init__(self, root: str | None = None):
        self.root = root or DEFAULT_BRIDGE
        self.proc: subprocess.Popen | None = None
        self.lines: list[str] = []
        self.url: str | None = None
        self.slots: list[str] = []
        self.synced: list[str] = []
        self._on_line = None

    def start(self, run_dir: str, ap_port: int, http_port: int = DEFAULT_HTTP_PORT,
              ap_host: str = "127.0.0.1", on_line=None, slots=None):
        if self.running:
            raise TrackerError("the tracker is already running")
        python, script = find_bridge(self.root)
        if port_in_use(http_port):
            raise TrackerError(f"port {http_port} is already in use - another "
                               "tracker is probably still running")
        self.slots = slots or slots_from_lock(run_dir)
        self.synced = sync_worlds(run_dir, self.root)
        self.lines, self._on_line = [], on_line

        cmd = [python, script, "--ap-host", ap_host, "--ap-port", str(ap_port),
               "--http-port", str(http_port)]
        for slot in self.slots:
            cmd += ["--slot", slot]
        env = dict(os.environ, SKIP_REQUIREMENTS_UPDATE="1")
        self.proc = subprocess.Popen(
            cmd, cwd=self.root, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.url = f"http://127.0.0.1:{http_port}/"
        threading.Thread(target=self._pump, daemon=True).start()
        return self

    def _pump(self):
        for line in self.proc.stdout:
            line = line.rstrip()
            self.lines.append(line)
            if self._on_line:
                try:
                    self._on_line(line)
                except Exception:
                    pass

    def wait_until_serving(self, http_port: int = DEFAULT_HTTP_PORT,
                           timeout: float = 120):
        """Block until the dashboard answers, not merely until the process exists.

        The bridge builds a real logic graph from the installed apworld before
        it serves anything, which takes a while on a big world.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc is None or self.proc.poll() is not None:
                tail = "\n".join(self.lines[-8:])
                raise TrackerError(f"the tracker exited before serving:\n{tail}")
            try:
                with socket.create_connection(("127.0.0.1", http_port), timeout=2):
                    return self.url
            except OSError:
                time.sleep(0.5)
        raise TrackerError(f"the tracker did not serve within {timeout:g}s")

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout: float = 10) -> int | None:
        if self.proc is None:
            return None
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        code = self.proc.poll()
        self.proc = None
        self.url = None
        return code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the tracker against a hosted seed.")
    ap.add_argument("--run", required=True, help="the run directory being hosted")
    ap.add_argument("--ap-port", type=int, default=38281)
    ap.add_argument("--ap-host", default="127.0.0.1")
    ap.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT)
    ap.add_argument("--bridge", default=DEFAULT_BRIDGE)
    args = ap.parse_args(argv)

    br = Bridge(args.bridge)
    try:
        br.start(args.run, args.ap_port, args.http_port, args.ap_host,
                 on_line=lambda l: print(l, flush=True))
        url = br.wait_until_serving(args.http_port)
    except TrackerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"\ntracking {', '.join(br.slots)}")
    print(f"dashboard: {url}")
    print("Ctrl-C to stop\n", flush=True)
    try:
        while br.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        br.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
