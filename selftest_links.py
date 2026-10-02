"""Offline self-test for the upstream pointers.

No network: every assertion is about what the registry says and what the
installed world files declare about themselves.

    python selftest_links.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import aplobby as core
import links

HERE = os.path.dirname(os.path.abspath(__file__))
ok = lambda msg: print(f"  ok   {msg}")


def main() -> int:
    reg = links.load()
    assert reg["repos"] and reg["clients"], "the registry lost its maps"
    ok(f"registry loads: {len(reg['repos'])} world repos, "
       f"{len(reg['clients'])} client entries, {len(reg['anchors'])} anchors")

    # -- the four states stay distinguishable --------------------------
    known = links.client("pseudoregalia", reg, ships_client_code=False)
    assert known["state"] == links.EXTERNAL and known["url"].startswith("https://github.com/")
    ok(f"a curated client resolves to a URL: {known['url']}")

    derived = links.client("no-such-world-anywhere", reg, ships_client_code=True)
    assert derived["state"] == links.BUNDLED, derived
    ok("a world that ships client.py reports 'bundled' with nothing curated")

    silent = links.client("no-such-world-anywhere", reg, ships_client_code=False)
    assert silent["state"] == links.UNKNOWN, silent
    assert silent["state"] != links.NONE
    ok("an uninvestigated world reports 'unknown', never 'none'")

    # That distinction is the whole point, so assert the contrast directly.
    reg2 = json.loads(json.dumps(reg))
    reg2["clients"]["fake_none"] = {"state": "none", "note": "emulator client"}
    assert links.client("fake_none", reg2, False)["state"] == links.NONE
    assert links.client("fake_absent", reg2, False)["state"] == links.UNKNOWN
    ok("'none needed' and 'nobody looked' are different answers")

    # -- bundled is derived, never stored ------------------------------
    assert not any(e.get("state") == links.BUNDLED for e in reg["clients"].values()), \
        "a 'bundled' entry was curated - it must be derived from the world file"
    ok("no curated entry claims 'bundled': that state comes from the apworld")

    # -- every curated entry carries its evidence ----------------------
    for slug, entry in reg["clients"].items():
        assert entry.get("evidence"), f"{slug} has no evidence field"
        assert entry.get("url") or entry.get("repo"), f"{slug} points nowhere"
    ok(f"all {len(reg['clients'])} client entries carry evidence and a pointer")

    # -- an anchor's repo never poses as the world's own upstream ------
    anchored = links.world_upstream("sm_map_rando", reg)
    assert anchored and anchored["state"] in ("no_upstream", "anchor"), anchored
    assert anchored["state"] != "repo", "an engine repo was sold as the world upstream"
    ok(f"sm_map_rando resolves to '{anchored['state']}', not a world upstream")

    # -- a broken registry degrades to honest ignorance -----------------
    with tempfile.TemporaryDirectory() as tmp:
        bad = os.path.join(tmp, "registry.json")
        open(bad, "w", encoding="utf-8").write("{ this is not json")
        empty = links.load(bad)
        assert empty["repos"] == {} and empty["clients"] == {}
        assert links.client("anything", empty, False)["state"] == links.UNKNOWN
    ok("an unreadable registry says 'unknown', it does not crash the app")

    # -- rows from preflight resolve to links ---------------------------
    row = {"game": "Pseudoregalia", "world": "pseudoregalia.apworld", "must_match": False}
    info = links.for_row(row, reg)
    assert info["slug"] == "pseudoregalia"
    pairs = links.urls(info)
    assert pairs and all(u.startswith("http") for _l, u in pairs), pairs
    ok(f"a preflight row yields openable links: {[l for l, _u in pairs]}")

    missing = links.for_row({"game": "Nothing", "world": None}, reg)
    assert missing["client"]["state"] == links.UNKNOWN and missing["world"] is None
    ok("a row with no installed world offers nothing rather than guessing")

    # -- client code detection, compiled or not ------------------------
    import io, zipfile
    def fake_world(member: str) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("w/archipelago.json", json.dumps({"game": "Test Game"}))
            zf.writestr(member, b"x")
        return buf.getvalue()

    for member, expect, why in (("w/client.py", True, "source client"),
                                ("w/Client.pyc", True, "compiled client"),
                                ("w/CLIENT.PY", True, "shouty client"),
                                ("w/regions.py", False, "no client at all")):
        game, ships = core.world_game(fake_world(member))
        assert game == "Test Game", game
        assert ships is expect, f"{why}: got {ships}, wanted {expect}"
    ok("client code is detected as .py or .pyc, any case - a compiled client counts")

    # -- base ROMs declared by host.yaml --------------------------------
    import tempfile as _tf
    with _tf.TemporaryDirectory() as fake_ap:
        host_yaml = [
            "general_options:",
            "  x: 1",
            "earthbound_options:",
            "  # File name of the EarthBound US ROM",
            '  rom_file: "EarthBound.sfc"',
            "sni_options:",
            '  sni_path: "SNI"',
            "outer_wilds_options:",
            "  something: else",
            "",
        ]
        with open(os.path.join(fake_ap, "host.yaml"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(host_yaml))
        roms = links.host_roms(fake_ap)
        assert roms == {"earthbound": "EarthBound.sfc"}, roms
        ok("host.yaml parse finds rom_file blocks and ignores options without one")

        st = links.rom_status("earthbound", roms, fake_ap)
        assert st["rom"] == "EarthBound.sfc" and st["found"] is None
        open(os.path.join(fake_ap, "EarthBound.sfc"), "wb").write(b"rom")
        st = links.rom_status("earthbound", roms, fake_ap)
        assert st["found"], st
        ok("a missing ROM reports missing, and is found once it is in place")
        assert links.rom_status("outer_wilds", roms, fake_ap) is None
        ok("a world that needs no ROM says nothing rather than 'missing'")

    # -- against the real install --------------------------------------
    index = core.index_worlds(core.AP_DEFAULT)
    if index:
        todo = links.unknown_slugs(index, reg)
        assert len(todo) < len(index), "nothing at all is known"
        ok(f"{len(index) - len(todo)} of {len(index)} installed worlds have a "
           f"client answer; {len(todo)} still to investigate")

        playing = links.lobby_slugs(index)
        assert all(isinstance(s, str) for s in playing)
        ok(f"the lobby is playing {len(playing)} installed games, for --first")

        # The harvester must not carry sentence punctuation into a repo name.
        for hit in index.values():
            for repo in links.candidates(hit["path"]):
                assert not repo.endswith((".", ",", ";", ")")), repo
                assert repo.count("/") == 1, repo
        ok("harvested repo names are clean: no trailing punctuation")

    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
