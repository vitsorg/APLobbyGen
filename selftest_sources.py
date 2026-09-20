"""Offline self-test for the import sources.

The remote importer is exercised too, with the network stubbed out by the
saved fixtures - so this whole file runs with no connection.

    python selftest_sources.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import zipfile

import aplobby as core
import lobby
import sources

HERE = os.path.dirname(os.path.abspath(__file__))
ok = lambda msg: print(f"  ok   {msg}")


def newest_players_dir() -> str:
    runs = os.path.join(HERE, "runs")
    for d in sorted(os.listdir(runs), reverse=True):
        p = os.path.join(runs, d, "Players")
        if os.path.isdir(p) and any(f.endswith((".yaml", ".yml")) for f in os.listdir(p)):
            return p
    raise SystemExit("no run directory with configs found")


def main() -> int:
    players = newest_players_dir()
    tmp = tempfile.mkdtemp(prefix="srctest-")
    try:
        # -- folder -------------------------------------------------------
        got = sources.folder(players)
        assert len(got) == 21, len(got)
        assert all(s["kind"] == "folder" and os.path.isabs(s["id"]) for _, s in got)
        assert len({s["id"] for _, s in got}) == len(got), "source ids are not unique"
        ok(f"folder(): {len(got)} configs, every source id distinct and absolute")

        # -- files --------------------------------------------------------
        one = sources.files([os.path.join(players, os.listdir(players)[0])])
        assert len(one) == 1 and one[0][1]["kind"] == "file"
        ok("files(): a single explicit config")

        # -- zip ----------------------------------------------------------
        zpath = os.path.join(tmp, "configs.zip")
        with zipfile.ZipFile(zpath, "w") as zf:
            for data, s in got[:3]:
                zf.writestr(f"nested/dir/{s['name']}", data)
            zf.writestr("readme.txt", b"not a config")
        z = sources.archive(zpath)
        assert len(z) == 3, len(z)
        assert all(s["kind"] == "zip" and "!" in s["id"] for _, s in z)
        assert {d for d, _ in z} == {d for d, _ in got[:3]}, "zip round-trip altered bytes"
        ok("archive(): finds configs at depth, ignores non-yaml, bytes intact")

        # -- refusals are explicit ----------------------------------------
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        for call, label in ((lambda: sources.folder(empty), "empty folder"),
                            (lambda: sources.folder(os.path.join(tmp, "nope")), "missing folder"),
                            (lambda: sources.archive(os.path.join(players, os.listdir(players)[0])), "non-zip"),
                            (lambda: sources.files([os.path.join(tmp, "nope.yaml")]), "missing file")):
            try:
                call()
                raise AssertionError(f"{label} did not raise")
            except sources.SourceError:
                pass
        ok("every unreadable source raises SourceError with a reason")

        # -- room id parsing ----------------------------------------------
        rid = core.room_id("https://ap-lobby.ionium.us/room/abcdef01-2345-6789-abcd-ef0123456789")
        assert rid == "abcdef01-2345-6789-abcd-ef0123456789", rid
        assert core.room_id(rid) == rid, "a bare id should pass through"
        try:
            core.room_id("not a room")
            raise AssertionError("room_id accepted nonsense")
        except ValueError:
            ok("room_id(): parses URL and bare id, raises ValueError not SystemExit")

        # -- the remote importer, with the network stubbed -----------------
        page = open(os.path.join(HERE, "fixtures", "room_synthetic.html"),
                    encoding="utf-8").read().encode()
        payloads = {"11111111-2222-3333-4444-555555555555": b"name: Fernwood\ngame: Pseudoregalia\n",
                    "66666666-7777-8888-9999-aaaaaaaaaaaa": b"name: Quillfeather\ngame: Slay the Spire II\n"}
        real_fetch = core.fetch

        def fake_fetch(url, timeout=30):
            if "/download/" in url:
                return payloads[url.rsplit("/", 1)[-1]]
            return page

        core.fetch = fake_fetch
        try:
            seen = []
            room = sources.ionium_room("abcdef01-2345-6789-abcd-ef0123456789",
                                       progress=lambda i, n, who: seen.append(who))
            assert len(room) == 2, len(room)
            assert [s["id"] for _, s in room] == list(payloads), [s["id"] for _, s in room]
            assert all(s["kind"] == "ionium" and s["room"].startswith("abcdef01") for _, s in room)
            assert seen == ["Fernwood", "Quillfeather"], seen
            ok("ionium_room(): yaml-ids carried through as source ids, progress reported")

            # A genuinely empty room must refuse, not import nothing quietly.
            page = open(os.path.join(HERE, "fixtures", "room_empty.html"),
                        encoding="utf-8").read().encode()
            try:
                sources.ionium_room("abcdef01-2345-6789-abcd-ef0123456789")
                raise AssertionError("an empty room imported quietly")
            except sources.SourceError as exc:
                assert "empty" in str(exc)
                ok("a real empty room refuses with a reason, rather than importing 0")
        finally:
            core.fetch = real_fetch

        # -- end to end: folder -> lobby -> staged run -------------------
        root = os.path.join(tmp, "lobby")
        with lobby.locked(root) as lb:
            for data, src in sources.folder(players):
                lb.upsert(data, src)
            assert len(lb.entries) == 21
            staged = lb.stage(os.path.join(tmp, "Players"))
            assert len(staged) == 21
            ok("end to end: folder -> lobby -> 21 staged configs, no network")

            # And the same import again changes nothing.
            actions = [lb.upsert(d, s)[0] for d, s in sources.folder(players)]
            assert set(actions) == {"unchanged"}, set(actions)
            ok("importing the same folder twice is a no-op")

        print("\nall checks passed")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
