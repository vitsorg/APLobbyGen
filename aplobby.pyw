r"""Desktop entry point: starts the window with no console behind it.

A .pyw runs under pythonw.exe, which has no console at all - so an exception
that would normally print a traceback goes nowhere and the app simply vanishes
with no explanation. That is the one thing this file exists to prevent: the
failure is written next to the app and shown in a dialog, so a crash launched
from a shortcut is still diagnosable.

Double-click it, or point a shortcut at it. `python aplobby_gui.py` still works
and is better while developing, because then the console shows everything.
"""
from __future__ import annotations

import datetime
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
CRASH_LOG = os.path.join(HERE, "crash.log")


def main() -> int:
    sys.path.insert(0, HERE)          # a shortcut can start from anywhere
    try:
        import aplobby_gui
        aplobby_gui.main()
        return 0
    except Exception:
        report = (f"{datetime.datetime.now().isoformat(timespec='seconds')}\n"
                  f"{traceback.format_exc()}\n")
        try:
            with open(CRASH_LOG, "a", encoding="utf-8") as fh:
                fh.write(report)
        except OSError:
            pass
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Archipelago Lobby Generator",
                "The app could not start.\n\n"
                + traceback.format_exc(limit=3)
                + f"\nWritten to {CRASH_LOG}")
            root.destroy()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
