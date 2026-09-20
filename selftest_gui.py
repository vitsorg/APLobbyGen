"""Smoke test that actually drives the GUI, without a human watching.

Builds the real window, pumps the event loop, and asserts what the user would
otherwise have to see: that a cold start loads the lobby off the UI thread,
that the table and the Generate gate agree with the roster, and that including
or excluding someone round-trips to the store.

    python selftest_gui.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import tkinter as tk

import lobby
import sources

HERE = os.path.dirname(os.path.abspath(__file__))
ok = lambda msg: print(f"  ok   {msg}")


def pump(root, app, seconds=60):
    """Run the event loop until the worker is idle AND the UI has caught up.

    Widget updates are posted to a queue that _drain services on a timer, so
    "the worker finished" happens strictly before "the table is redrawn".
    Waiting only on the worker reads the table one repaint too early.
    """
    deadline = time.time() + seconds
    root.update()
    while (app.busy or app.index is None) and time.time() < deadline:
        root.update()
        time.sleep(0.02)
    if app.busy:
        raise AssertionError("worker never finished")
    while not app.msgs.empty() and time.time() < deadline:
        root.update()
        time.sleep(0.02)
    for _ in range(5):          # let the last _drain tick land
        root.update()
        time.sleep(0.12)


def seed_lobby(root_dir, players_dir):
    with lobby.locked(root_dir) as lb:
        for data, src in sources.folder(players_dir):
            lb.upsert(data, src)
        return len(lb.entries)


def newest_players_dir():
    runs = os.path.join(HERE, "runs")
    for d in sorted(os.listdir(runs), reverse=True):
        p = os.path.join(runs, d, "Players")
        if os.path.isdir(p) and any(f.endswith((".yaml", ".yml")) for f in os.listdir(p)):
            return p
    raise SystemExit("no run directory with configs found")


def main() -> int:
    import aplobby_gui

    tmp = tempfile.mkdtemp(prefix="guitest-")
    try:
        lobby_root = os.path.join(tmp, "lobby")
        n = seed_lobby(lobby_root, newest_players_dir())
        print(f"temp lobby seeded with {n} players\n")

        root = tk.Tk()
        root.withdraw()                      # no window flashes up
        app = aplobby_gui.App(root)
        app.lobby_root = lobby_root          # before the scheduled load fires
        try:
            # -- cold start ------------------------------------------------
            pump(root, app)
            assert len(app.rows) == n, (len(app.rows), n)
            ok(f"cold start loaded {n} players with no room prompt")
            assert app.index, "installed worlds were never indexed"
            ok(f"installed worlds indexed on the worker thread ({len(app.index)} games)")
            assert len(app.tree.get_children()) == n
            ok("every player is rendered in the table")

            # -- the Generate gate names its reason -------------------------
            app._refresh_gen_state()
            state = str(app.gen_btn["state"])
            reason = app.gen_reason.get()
            missing = app._missing()
            if missing:
                assert state == "disabled" and "no installed world" in reason
                ok(f"Generate is off and says why: {reason!r}")
            else:
                assert state == "normal" and reason == "", (state, reason)
                ok("every game resolves, so Generate is on with no caveat")

            # -- sitting someone out round-trips ---------------------------
            slot = app.rows[0]["slot"]
            who = app.rows[0].get("name")
            app.tree.selection_set(slot)
            app.set_selected(False)
            pump(root, app)
            assert app.tree.set(slot, "in") == "-"
            assert app.tree.set(slot, "state") == "sitting out"
            ok(f"{who} sits out: the table shows it")
            with lobby.locked(lobby_root) as lb:
                assert lb.find(slot)["enabled"] is False
                assert len(lb.enabled()) == n - 1
            ok("and the store agrees, so it survives a restart")

            # -- and back in -----------------------------------------------
            app.tree.selection_set(slot)
            app.set_selected(True)
            pump(root, app)
            assert app.tree.set(slot, "in") == "Y"
            with lobby.locked(lobby_root) as lb:
                assert len(lb.enabled()) == n
            ok("including them again restores the roster")

            # -- an empty lobby is a state, not a crash --------------------
            empty = os.path.join(tmp, "empty-lobby")
            app.lobby_root = empty
            app.reload()
            pump(root, app)
            assert app.rows == []
            assert str(app.gen_btn["state"]) == "disabled"
            assert "import" in app.gen_reason.get()
            ok(f"empty lobby: Generate off, reason {app.gen_reason.get()!r}")

            # -- a bad Archipelago path degrades, not explodes --------------
            app.lobby_root = lobby_root
            app.ap_var.set(os.path.join(tmp, "no-such-install"))
            app.reload()
            pump(root, app)
            assert app.index == {}, app.index
            assert len(app.rows) == n
            assert str(app.gen_btn["state"]) == "disabled"
            ok("a wrong Archipelago folder shows every game as missing, no crash")
        finally:
            root.destroy()

        print("\nall checks passed")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
