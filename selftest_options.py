"""Self-test for reading a game's options and editing a config with them.

Offline: it reads Archipelago's generated templates and rewrites configs in a
temp lobby. No network, no generation.

    python selftest_options.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import tkinter as tk

import aplobby as core
import lobby
import options as opts

HERE = os.path.dirname(os.path.abspath(__file__))
ok = lambda msg: print(f"  ok   {msg}")


def main() -> int:
    games = opts.available(core.AP_DEFAULT)
    assert games, "no templates found - run Archipelago's Generate Template Options"
    ok(f"{len(games)} installed games have a template")

    # -- parsing a real one -------------------------------------------
    spec = opts.parse("EarthBound", core.AP_DEFAULT)
    assert len(spec) > 30, len(spec)
    by_key = {o["key"]: o for o in spec}
    ok(f"EarthBound parses to {len(spec)} options")

    kinds = {o["kind"] for o in spec}
    assert kinds == {opts.BOOL, opts.RANGE, opts.CHOICE, opts.COLLECTION}, kinds
    ok(f"every option is typed as one of {sorted(kinds)}")

    # A list or mapping option ([] / {} in the template) is not a setting with
    # values to pick from. Writing a scalar for one produced "local_items: None"
    # and failed generation outright.
    lists = [o for o in spec if o["kind"] == opts.COLLECTION]
    assert {"local_items", "start_inventory", "plando_items"} <= {o["key"] for o in lists}
    assert all(not o["editable"] and o["default"] is None for o in lists), lists[:2]
    ok(f"{len(lists)} list/mapping options are marked uneditable with no default")

    editable = [o for o in spec if o["editable"]]
    assert all(o["default"] is not None for o in editable),         [o["key"] for o in editable if o["default"] is None]
    ok(f"every one of the {len(editable)} editable options has a usable default")

    g = by_key["giygas_required"]
    assert g["kind"] == opts.BOOL and g["default"] is True, g
    s = by_key["sanctuaries_required"]
    assert s["kind"] == opts.RANGE and (s["min"], s["max"]) == (1, 8), s
    assert s["default"] == 4, s
    ok("a toggle, and a range that carries its own bounds (1..8, default 4)")

    # A range whose default is a NAMED alias must resolve to the number, or a
    # numeric widget cannot show it.
    pb = by_key["progression_balancing"]
    assert pb["kind"] == opts.RANGE and pb["default"] == 50, pb
    ok("a named default resolves to its number (normal -> 50)")

    # Sections come from the headings, and must not drift by one.
    assert by_key["accessibility"]["section"] == "Game Options", by_key["accessibility"]
    assert by_key["giygas_required"]["section"] == "Goal Settings"
    ok("options sit under the heading they were printed below")

    # -- every installed template parses -------------------------------
    bad = []
    for game in games:
        try:
            got = opts.parse(game, core.AP_DEFAULT)
            if not got:
                bad.append((game, "no options"))
        except Exception as exc:
            bad.append((game, f"{type(exc).__name__}: {exc}"))
    assert not bad, bad[:5]
    ok(f"all {len(games)} templates parse without special-casing a single game")

    # -- a weight table is not a choice --------------------------------
    # R.E.P.O's filler_item_weights is sixteen items each with a weight, all of
    # them used. Read as a choice it collapsed to its highest entry, and the
    # generator rejected the result: "Cannot Convert from non-dictionary, got
    # <class 'str'>". A generated template gives a real choice exactly ONE
    # non-zero weight, so more than one means a table.
    if "R.E.P.O" in games:
        repo = {o["key"]: o for o in opts.parse("R.E.P.O", core.AP_DEFAULT)}
        fw = repo.get("filler_item_weights")
        assert fw is not None, sorted(repo)
        assert fw["kind"] == opts.COLLECTION and not fw["editable"], fw
        assert fw["default"] is None, fw
        ok("a weight table is left alone, not collapsed to its top entry")
        assert repo["pellys_required"]["editable"], repo["pellys_required"]
        ok("a real range beside it is still editable")

    # -- read / write a config ----------------------------------------
    tmp = tempfile.mkdtemp(prefix="opttest-")
    try:
        cfg = ("name: Tester\n"
               "description: a config\n"
               "game: EarthBound\n"
               "requires:\n"
               "  version: 0.6.6\n"
               "EarthBound:\n"
               "  progression_balancing: normal\n"
               "  giygas_required: false\n").encode()
        assert opts.read_config(cfg, "EarthBound") == {
            "progression_balancing": "normal", "giygas_required": False}
        ok("a config's own values are read back as scalars")

        out = opts.write_config(cfg, "EarthBound",
                                {"giygas_required": True, "sanctuaries_required": 7})
        text = out.decode("utf-8")
        assert "name: Tester" in text and "description: a config" in text
        assert "requires:" in text and "version: 0.6.6" in text
        ok("writing settings leaves name, description and requires untouched")
        back = opts.read_config(out, "EarthBound")
        assert back["giygas_required"] is True and back["sanctuaries_required"] == 7
        # A merge, so an option that was there and was NOT edited stays.
        assert back["progression_balancing"] == "normal", back
        ok("new values read back exactly, and untouched ones are still there")

        rnd = opts.write_config(cfg, "EarthBound", {"starting_character": "random"})
        assert opts.read_config(rnd, "EarthBound")["starting_character"] == "random"
        ok("'random' round-trips through the config writer as a plain value")

        # The merge: anything this form does not understand must survive.
        rich = "\n".join([
            "name: Tester",
            "game: EarthBound",
            "EarthBound:",
            "  giygas_required: false",
            "  local_items: []",
            "  start_inventory:",
            "    Bomb: 1",
            "  sanctuaries_required:",
            "    4: 50",
            "    8: 10",
            "",
        ]).encode()
        merged = opts.write_config(rich, "EarthBound",
                                   {"giygas_required": True,
                                    "sanctuaries_required": 7}).decode()
        for keep in ("local_items: []", "start_inventory:", "Bomb: 1"):
            assert keep in merged, (keep, merged)
        ok("saving a toggle leaves plando, start_inventory and lists untouched")
        assert "4: 50" not in merged and "sanctuaries_required: 7" in merged
        ok("but an option you DID edit loses its old weighted block")

        bom = opts.BOM + cfg
        assert opts.write_config(bom, "EarthBound", {"giygas_required": True}
                                 ).startswith(opts.BOM)
        ok("a byte-order mark survives the rewrite")

        # -- the form, driven without a human --------------------------
        import optionsdlg
        root = tk.Tk()
        root.withdraw()
        try:
            dlg = optionsdlg.OptionsDialog(root, "EarthBound",
                                           {"sanctuaries_required": 6},
                                           core.AP_DEFAULT)
            root.update()
            assert len(dlg.vars) == len(editable), (len(dlg.vars), len(editable))
            assert len(dlg.untouched) == len(lists)
            ok(f"the form built {len(dlg.vars)} widgets and skipped "
               f"{len(dlg.untouched)} list option(s)")
            assert not any(v is None for v in dlg.collect().values())
            ok("nothing it would save is None - the bug that failed generation")
            assert dlg.vars["sanctuaries_required"].get() == "6"
            ok("it shows the config's current value, not the template default")

            dlg.vars["giygas_required"].set(False)
            dlg.vars["sanctuaries_required"].set("8")
            got = dlg.collect()
            assert got["giygas_required"] is False and got["sanctuaries_required"] == 8
            ok("edits come back typed: a bool stays bool, a range becomes int")

            dlg.reset()
            assert dlg.vars["sanctuaries_required"].get() == "4"
            ok("reset restores the template defaults")

            # The exact bug the form shipped with: a config carrying a named
            # alias loaded into a numeric widget and refused to save.
            dlg2 = optionsdlg.OptionsDialog(root, "EarthBound",
                                            {"progression_balancing": "normal"},
                                            core.AP_DEFAULT)
            root.update()
            assert dlg2.collect()["progression_balancing"] == 50
            ok("a config set to a named alias ('normal') saves as its number")

            # random is valid for every option; the template only lists it for
            # ranges, so it has to be offered rather than parroted.
            sc = dlg2.by_key["starting_character"]
            assert "random" in sc["rolls"], sc
            dlg2.vars["starting_character"].set("random")
            dlg2.vars["giygas_required"].set("random")
            got2 = dlg2.collect()
            assert got2["starting_character"] == "random", got2["starting_character"]
            assert got2["giygas_required"] == "random", got2["giygas_required"]
            ok("random is selectable for a choice AND a toggle, and survives saving")

            dlg2.vars["sanctuaries_required"].set("99")
            try:
                dlg2.collect()
                raise AssertionError("a range accepted a value outside its bounds")
            except ValueError as exc:
                assert "outside" in str(exc), exc
            ok("a range refuses a number outside its own bounds")
            dlg2.destroy()

            dlg.vars["sanctuaries_required"].set("not a number")
            try:
                dlg.collect()
                raise AssertionError("a non-numeric range was accepted")
            except ValueError:
                ok("a range refuses text rather than writing a broken config")
            dlg.destroy()
        finally:
            root.destroy()

        # -- the edit survives a trip through the lobby ----------------
        root_dir = os.path.join(tmp, "lobby")
        with lobby.locked(root_dir) as lb:
            _, entry = lb.upsert(cfg, {"kind": "test", "id": "opt"})
            slot = entry["slot"]
            edited = opts.write_config(cfg, "EarthBound", {"sanctuaries_required": 8})
            action, _ = lb.replace(slot, edited, {"kind": "settings-form", "id": slot})
            assert action == "updated", action
            live = open(lb._blob(slot), "rb").read()
            assert opts.read_config(live, "EarthBound")["sanctuaries_required"] == 8
            assert len(lb.find(slot)["history"]) == 1
        ok("saving through the lobby updates in place and keeps the old version")

        print("\nall checks passed")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
