"""Offline self-test for the local lobby store.

No network, no test framework - runs against the real config corpus in the
newest run directory. Every check asserts, so a silent pass is a real pass.

    python selftest_lobby.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile

import lobby
import aplobby as core

HERE = os.path.dirname(os.path.abspath(__file__))
ok = lambda msg: print(f"  ok   {msg}")


def corpus() -> list[tuple[str, bytes]]:
    """The real configs from the newest run that has a Players folder."""
    runs = os.path.join(HERE, "runs")
    for d in sorted(os.listdir(runs), reverse=True):
        p = os.path.join(runs, d, "Players")
        if os.path.isdir(p) and any(f.endswith((".yaml", ".yml")) for f in os.listdir(p)):
            return [(f, open(os.path.join(p, f), "rb").read())
                    for f in sorted(os.listdir(p)) if f.endswith((".yaml", ".yml"))]
    raise SystemExit("no run directory with configs found - cannot self-test")


def main() -> int:
    files = corpus()
    print(f"corpus: {len(files)} real configs\n")
    root = tempfile.mkdtemp(prefix="lobbytest-")
    try:
        # -- import, then re-import ------------------------------------
        with lobby.locked(root) as lb:
            actions = [lb.upsert(data, {"kind": "folder", "id": fn})[0] for fn, data in files]
            assert actions == ["added"] * len(files), actions
            ok(f"{len(files)} configs added")
            assert len(lb.enabled()) == len(files)
            assert lb.problems() == [], lb.problems()
            ok("no problems reported, all enabled by default")
            first = lb.find("p001")
            stamp = first["updated"]

        with lobby.locked(root) as lb:
            actions = [lb.upsert(data, {"kind": "folder", "id": fn})[0] for fn, data in files]
            assert set(actions) == {"unchanged"}, set(actions)
            ok("re-import of identical bytes is entirely unchanged")
            assert lb.find("p001")["updated"] == stamp
            assert lb.find("p001")["history"] == []
            ok("unchanged import touched neither timestamp nor history")

        # -- a changed config -------------------------------------------
        fn0, data0 = files[0]
        with lobby.locked(root) as lb:
            before = lb.find("p001")["sha256"]
            action, e = lb.upsert(data0 + b"\n# edited\n", {"kind": "folder", "id": fn0})
            assert action == "updated", action
            assert e["slot"] == "p001", e["slot"]
            ok("changed bytes update in place, slot unchanged")
            kept = os.path.join(root, "history", f"{before}.yaml")
            assert os.path.isfile(kept), kept
            assert open(kept, "rb").read() == data0
            ok("previous version retained in history, byte-for-byte")
            assert [h["sha256"] for h in e["history"]] == [before]

        # -- a player renames themselves --------------------------------
        fn1, data1 = files[1]
        with lobby.locked(root) as lb:
            slot_before = lb.find("p002")["slot"]
            name_before = lb.find("p002")["name"]
            renamed = data1.replace(f"name: {name_before}".encode(),
                                    b"name: TotallyNewHandle", 1)
            assert renamed != data1, "corpus config did not contain its own name line"
            # Same stable source id, different name: one person, not two.
            action, e = lb.upsert(renamed, {"kind": "ionium", "id": "yaml-id-002"})
            # First sighting of this id falls through to name+game, which no
            # longer matches, so this is a genuine new slot - seed the id first.
            assert action == "added"
            lb.remove(e["slot"])

            lb.upsert(data1, {"kind": "ionium", "id": "yaml-id-002"})
            action, e = lb.upsert(renamed, {"kind": "ionium", "id": "yaml-id-002"})
            assert action == "updated", action
            assert e["slot"] == slot_before, (e["slot"], slot_before)
            assert name_before in e["names_seen"] and "TotallyNewHandle" in e["names_seen"]
            ok("rename under a stable source id keeps the slot and both names")

        # -- two different people, same name and game -------------------
        with lobby.locked(root) as lb:
            twin = files[2][1]
            a = lb.upsert(twin, {"kind": "ionium", "id": "twin-A"})[1]
            b = lb.upsert(twin + b"\n# distinct\n", {"kind": "ionium", "id": "twin-B"})[1]
            assert a["slot"] != b["slot"], "two stable ids collapsed into one slot"
            ok("same name+game under different source ids stay separate slots")
            probs = lb.problems()
            assert any("distinct names" in p for p in probs), probs
            ok("duplicate names surface as a preflight problem, not silent corruption")
            lb.remove(b["slot"])

        # -- staging ------------------------------------------------------
        with lobby.locked(root) as lb:
            staged_dir = os.path.join(root, "_stage")
            names = lb.stage(staged_dir)
            assert len(names) == len(lb.enabled()) == len(os.listdir(staged_dir))
            fns = [r["yaml"] for r in names]
            assert len(fns) == len(set(fns)), "stage produced a duplicate filename"
            assert all(r["slot"] and r["yaml_sha256"] for r in names)
            ok(f"staged {len(names)} configs, all filenames distinct, slots carried")

            # Byte fidelity: the generator reads these, so a staging bug that
            # altered them would be both catastrophic and invisible.
            live = {e["slot"]: open(os.path.join(root, "players", f"{e['slot']}.yaml"),
                                    "rb").read() for e in lb.enabled()}
            staged = {open(os.path.join(staged_dir, r["yaml"]), "rb").read() for r in names}
            assert staged == set(live.values()), "staged bytes differ from the lobby's"
            ok("staged configs are byte-identical to the lobby's copies")

            off = lb.enabled()[0]["slot"]
            lb.set_enabled(off, False)
            shutil.rmtree(staged_dir)
            names2 = lb.stage(staged_dir)
            assert len(names2) == len(names) - 1
            ok("a disabled slot is excluded from staging but kept in the lobby")
            lb.set_enabled(off, True)
            shutil.rmtree(staged_dir)

        # -- crash: manifest lost, blobs survive --------------------------
        with lobby.locked(root) as lb:
            count = len(lb.entries)
        os.unlink(os.path.join(root, "lobby.json"))
        os.unlink(os.path.join(root, "lobby.json.bak"))
        with lobby.locked(root) as lb:
            assert len(lb.entries) == count, (len(lb.entries), count)
            assert len(lb.adopted) == count
            ok(f"manifest deleted: all {count} orphan configs adopted back")

        # -- crash: manifest survives, one blob lost ----------------------
        with lobby.locked(root) as lb:
            victim = lb.entries[0]["slot"]
        os.unlink(os.path.join(root, "players", f"{victim}.yaml"))
        with lobby.locked(root) as lb:
            assert lb.find(victim)["missing"] is True
            assert any("missing" in p for p in lb.problems())
            ok("a lost config file is flagged, not ignored")
            try:
                lb.stage(os.path.join(root, "_stage2"))
                raise AssertionError("stage() generated a short roster")
            except lobby.LobbyError:
                ok("stage() refuses rather than quietly dropping a player")
            lb.remove(victim)

        # -- torn manifest falls back to the backup -----------------------
        # The backup is deliberately one mutation behind: it is the manifest as
        # it stood before the most recent write. Recovery therefore restores
        # that state, not the newest one, and says so rather than pretending.
        bak = json.load(open(os.path.join(root, "lobby.json.bak"), encoding="utf-8"))
        expected = len(bak["players"])
        open(os.path.join(root, "lobby.json"), "wb").write(b'{"schema": "aplo')
        with lobby.locked(root) as lb:
            assert len(lb.entries) == expected, (len(lb.entries), expected)
            assert lb.problems_at_load and "recovered" in lb.problems_at_load
            ok("torn manifest recovered from the backup, one step behind, and says so")
            # And reconcile still catches anything the stale manifest got wrong.
            assert all(os.path.isfile(os.path.join(root, "players", f"{e['slot']}.yaml"))
                       or e.get("missing") for e in lb.entries)
            ok("entries the stale manifest over-claims are flagged missing")

        # -- the lock actually excludes -----------------------------------
        with lobby.locked(root):
            try:
                with lobby.locked(root):
                    raise AssertionError("two writers held the lobby at once")
            except lobby.LobbyLocked as exc:
                assert "pid" in str(exc)
                ok("a second writer is refused and told which pid holds it")
        assert not os.path.isfile(os.path.join(root, ".lock"))
        ok("lock released on exit")

        # -- the manifest is valid, readable JSON -------------------------
        doc = json.load(open(os.path.join(root, "lobby.json"), encoding="utf-8"))
        assert doc["schema"] == lobby.SCHEMA
        assert all(re.match(r"p\d{3}$", p["slot"]) for p in doc["players"])
        ok("manifest is well-formed and every slot id is opaque")

        print("\nall checks passed")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
