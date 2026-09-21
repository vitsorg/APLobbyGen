r"""Host a generated seed on this machine, for local play and testing.

This is the local counterpart to publish.py: same seed, no upload. It drives
the ArchipelagoServer that ships with the install, so the rules, the console
commands and the save file are exactly what a real room gives you - the only
difference is that the multidata never leaves the machine.

    python serve.py runs\<dir>\output\AP_123.zip
    python serve.py --port 38282 --password hunter2 AP_123.archipelago

The server binds every interface, so other machines on the same network can
join at the address it prints. Patch files are not served: a player on another
machine still needs their own file out of the seed zip.

Standard library only.
"""
from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import threading
import time
import zipfile

AP_DEFAULT = r"C:\ProgramData\Archipelago"
DEFAULT_PORT = 38281
EXE = "ArchipelagoServer.exe"

# What the server prints once it is actually up. Waiting for this rather than
# for the process to merely exist matters: loading the data packages for a
# 20-game seed takes ten seconds or more, and a client that connects before
# then is refused.
HOSTING = re.compile(r"Hosting game at (\S+):(\d+)")
LISTENING = re.compile(r"server listening on (\S+)")


class ServeError(Exception):
    pass


def multidata_for(seed: str, out_dir: str | None = None) -> str:
    """The .archipelago file to host, extracted from a seed zip if need be.

    Accepts either the zip the generator produced or an already-extracted
    multidata file, because both are things a person reasonably has to hand.
    """
    if not os.path.isfile(seed):
        raise ServeError(f"no such file: {seed}")
    if seed.lower().endswith(".archipelago"):
        return os.path.abspath(seed)
    try:
        with zipfile.ZipFile(seed) as zf:
            inner = [n for n in zf.namelist() if n.lower().endswith(".archipelago")]
            if not inner:
                raise ServeError(f"{os.path.basename(seed)} contains no .archipelago "
                                 "file - is it a seed zip?")
            if len(inner) > 1:
                raise ServeError(f"{os.path.basename(seed)} contains {len(inner)} "
                                 ".archipelago files; unpack it and pick one")
            out_dir = out_dir or os.path.dirname(os.path.abspath(seed))
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, os.path.basename(inner[0]))
            data = zf.read(inner[0])
            # Re-extracting would orphan the .apsave beside it, so leave an
            # identical copy alone and keep the save file meaningful.
            if not (os.path.isfile(path) and os.path.getsize(path) == len(data)):
                with open(path, "wb") as fh:
                    fh.write(data)
            return path
    except zipfile.BadZipFile:
        raise ServeError(f"{os.path.basename(seed)} is neither a zip nor a "
                         ".archipelago file")


def port_in_use(port: int, host: str = "0.0.0.0") -> bool:
    """Check before launching, so a clash is a sentence and not a stack trace."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return False
        except OSError:
            return True


class Server:
    """A running ArchipelagoServer, with its output on a queue you can read.

    Stopping is /exit first and terminate only as a fallback: the server
    flushes its save file on a clean exit, and killing it loses whatever
    happened since the last autosave.
    """

    def __init__(self, ap_dir: str = AP_DEFAULT):
        self.ap_dir = ap_dir
        self.proc: subprocess.Popen | None = None
        self.lines: list[str] = []
        self.address: tuple[str, int] | None = None
        self.multidata: str | None = None
        self._on_line = None
        self._reader: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self, seed: str, port: int = DEFAULT_PORT, *, out_dir=None,
              password=None, server_password=None, save=True, on_line=None):
        """Launch the server. Returns at once - call wait_until_hosting()."""
        if self.running:
            raise ServeError("this server is already running")
        exe = os.path.join(self.ap_dir, EXE)
        if not os.path.isfile(exe):
            raise ServeError(f"{exe} not found - is this the Archipelago folder?")
        if port_in_use(port):
            raise ServeError(f"port {port} is already in use - another server is "
                             "probably still running")

        self.multidata = multidata_for(seed, out_dir)
        self.lines, self.address, self._on_line = [], None, on_line
        cmd = [exe, self.multidata, "--port", str(port)]
        if password:
            cmd += ["--password", password]
        if server_password:
            cmd += ["--server_password", server_password]
        if not save:
            cmd += ["--disable_save"]
        self.proc = subprocess.Popen(
            cmd, cwd=self.ap_dir,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        return self

    def _pump(self):
        for line in self.proc.stdout:
            line = line.rstrip()
            self.lines.append(line)
            m = HOSTING.search(line)
            if m:
                self.address = (m.group(1), int(m.group(2)))
            if self._on_line:
                try:
                    self._on_line(line)
                except Exception:      # a bad callback must not kill the reader
                    pass

    def wait_until_hosting(self, timeout: float = 180):
        """Block until the server says it is hosting. Returns (host, port)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.address:
                return self.address
            if self.proc is None or self.proc.poll() is not None:
                tail = "\n".join(self.lines[-6:])
                raise ServeError(f"the server exited before it started hosting:\n{tail}")
            time.sleep(0.1)
        raise ServeError(f"the server did not report hosting within {timeout:g}s")

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def send(self, command: str):
        """Type a console command at the server, e.g. '/players'."""
        if not self.running:
            raise ServeError("the server is not running")
        self.proc.stdin.write(command.rstrip("\n") + "\n")
        self.proc.stdin.flush()

    def stop(self, timeout: float = 15) -> int | None:
        """Ask the server to exit, then insist. Returns its exit code."""
        if self.proc is None:
            return None
        if self.running:
            try:
                self.send("/exit")
            except (ServeError, OSError, ValueError):
                pass
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        code = self.proc.poll()
        self.proc = None
        self.address = None
        return code

    # -- what to tell people ----------------------------------------------

    def connect_strings(self) -> list[str]:
        """The addresses to hand out, most private first.

        The server announces one interface; this machine may have several, and
        on this network the announced one is usually the useful one.
        """
        if not self.address:
            return []
        host, port = self.address
        out = [f"localhost:{port}"]
        if host not in ("localhost", "127.0.0.1", "0.0.0.0"):
            out.append(f"{host}:{port}")
        return out


# ---------------------------------------------------------------- command line

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Host a seed on this machine.")
    ap.add_argument("seed", help="the seed zip, or a .archipelago file")
    ap.add_argument("--ap", default=AP_DEFAULT, help="Archipelago install folder")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--password", help="password players must give to join")
    ap.add_argument("--server-password", dest="server_password",
                    help="password for admin console commands")
    ap.add_argument("--no-save", action="store_true",
                    help="do not write a .apsave - throwaway test runs")
    args = ap.parse_args(argv)

    srv = Server(args.ap)
    try:
        srv.start(args.seed, args.port, password=args.password,
                  server_password=args.server_password, save=not args.no_save,
                  on_line=lambda l: print(l, flush=True))
        srv.wait_until_hosting()
    except ServeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("\n" + "-" * 60)
    for s in srv.connect_strings():
        print(f"  connect at  {s}")
    print("  players still need their own patch file from the seed zip")
    print("  type server commands below, or Ctrl-C to stop")
    print("-" * 60 + "\n", flush=True)

    try:
        for line in sys.stdin:                     # pass the console through
            if not srv.running:
                break
            srv.send(line)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nstopping the server...")
        srv.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
