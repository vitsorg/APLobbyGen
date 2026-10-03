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
    assert kinds <= {opts.BOOL, opts.RANGE, opts.CHOICE}, kinds
    assert kinds == {opts.BOOL, opts.RANGE, opts.CHOICE}, f"only saw {kinds}"
    ok(f"every option is typed as one of {sorted(kinds)}")

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
        assert opts.read_config(out, "EarthBound") == {
            "giygas_required": True, "sanctuaries_required": 7}
        ok("and the new values read back exactly")

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
            assert len(dlg.vars) == len(spec), (len(dlg.vars), len(spec))
            ok(f"the form built {len(dlg.vars)} widgets from the template")
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
