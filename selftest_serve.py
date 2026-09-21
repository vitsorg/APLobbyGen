"""Self-test for local hosting: really starts the server, really connects.

Everything here is loopback - no internet - but it is not a mock: the real
ArchipelagoServer loads the real seed, and the check is a TCP connection to
the port it claims to be listening on. Nothing else proves hosting works.

    python selftest_serve.py
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
import zipfile

import serve

HERE = os.path.dirname(os.path.abspath(__file__))
ok = lambda msg: print(f"  ok   {msg}")

PORT = 38399          # not the default, so a server you are already running is safe


def newest_seed() -> str:
    runs = os.path.join(HERE, "runs")
    for d in sorted(os.listdir(runs), reverse=True):
        out = os.path.join(runs, d, "output")
        if not os.path.isdir(out):
            continue
        zips = [f for f in sorted(os.listdir(out)) if f.endswith(".zip")]
        if zips:
            return os.path.join(out, zips[0])
    raise SystemExit("no generated seed found - run a generation first")


def main() -> int:
    seed = newest_seed()
    print(f"seed: {os.path.basename(seed)}\n")
    tmp = tempfile.mkdtemp(prefix="servetest-")
    try:
        # -- unpacking the multidata --------------------------------------
        md = serve.multidata_for(seed, tmp)
        assert os.path.isfile(md) and md.endswith(".archipelago")
        with zipfile.ZipFile(seed) as zf:
            inner = next(n for n in zf.namelist() if n.endswith(".archipelago"))
            assert open(md, "rb").read() == zf.read(inner), "multidata was altered"
        ok("the multidata comes out of the seed zip byte-for-byte")

        stamp = os.path.getmtime(md)
        assert serve.multidata_for(seed, tmp) == md
        assert os.path.getmtime(md) == stamp, "an identical multidata was rewritten"
        ok("extracting twice leaves the file alone, so its .apsave stays valid")

        # -- refusals are sentences ---------------------------------------
        for path, why in ((os.path.join(tmp, "nope.zip"), "missing file"),
                          (__file__, "not a zip")):
            try:
                serve.multidata_for(path, tmp)
                raise AssertionError(f"{why} did not raise")
            except serve.ServeError:
                pass
        ok("a missing or non-seed file raises ServeError with a reason")

        # -- host it for real ---------------------------------------------
        assert not serve.port_in_use(PORT), f"port {PORT} was already busy"
        srv = serve.Server()
        srv.start(seed, PORT, out_dir=tmp, save=False)
        try:
            host, port = srv.wait_until_hosting(timeout=240)
            assert port == PORT, (port, PORT)
            ok(f"the server reports hosting at {host}:{port}")

            with socket.create_connection(("127.0.0.1", PORT), timeout=10):
                pass
            ok("a TCP connection to that port is accepted")

            assert serve.port_in_use(PORT), "the port check disagrees with reality"
            assert f"localhost:{PORT}" in srv.connect_strings()
            ok(f"connect strings offered: {', '.join(srv.connect_strings())}")

            # A second server on the same port must refuse, not race.
            try:
                serve.Server().start(seed, PORT, out_dir=tmp)
                raise AssertionError("a second server took the same port")
            except serve.ServeError as exc:
                assert "in use" in str(exc), exc
            ok("a second server on the same port is refused before launching")

            srv.send("/players")
        finally:
            code = srv.stop()
        assert not srv.running
        ok(f"/exit stopped it cleanly (exit code {code})")
        assert not serve.port_in_use(PORT), "the port was not released"
        ok("the port is free again afterwards")

        # -- no .apsave when saving is off --------------------------------
        saves = [f for f in os.listdir(tmp) if f.endswith(".apsave")]
        assert saves == [], saves
        ok("--no-save really wrote no save file")

        print("\nall checks passed")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
