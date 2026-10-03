r"""Create the Windows shortcut that launches this app with its own icon.

Points at pythonw.exe (no console window) running aplobby.pyw, with icon.ico
and the project as the working directory. Written through WScript.Shell via
PowerShell so it needs nothing installed - no pywin32.

    python make_shortcut.py                 # Desktop
    python make_shortcut.py --start-menu    # Desktop and Start Menu
    python make_shortcut.py --where         # just print where they would go

The shortcut is what makes Windows show the icon in the taskbar and Start
Menu; the app separately claims an AppUserModelID at startup so the taskbar
groups it as itself instead of under "Python".
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "Archipelago Lobby Generator"
TARGET_SCRIPT = os.path.join(HERE, "aplobby.pyw")
ICON = os.path.join(HERE, "icon.ico")


def pythonw() -> str:
    """The console-less interpreter beside the one running this."""
    candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return candidate if os.path.isfile(candidate) else sys.executable


def desktop_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "Desktop")


def start_menu_dir() -> str:
    return os.path.join(os.environ.get("APPDATA", ""),
                        "Microsoft", "Windows", "Start Menu", "Programs")


def create(folder: str) -> str:
    """Write <folder>/<NAME>.lnk. Returns the path."""
    if not os.path.isdir(folder):
        raise FileNotFoundError(folder)
    link = os.path.join(folder, f"{NAME}.lnk")

    def ps(value: str) -> str:
        """A PowerShell single-quoted literal: only ' needs escaping, by doubling.

        -Command does not hand positional arguments to $args, so the paths are
        embedded. Single quotes mean a path with $ or a backtick in it is still
        taken literally.
        """
        return "'" + value.replace("'", "''") + "'"

    script = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({ps(link)});"
        f"$s.TargetPath = {ps(pythonw())};"
        f'$s.Arguments = {ps(chr(34) + TARGET_SCRIPT + chr(34))};'
        f"$s.WorkingDirectory = {ps(HERE)};"
        f"$s.IconLocation = {ps(ICON)};"
        "$s.Description = 'Generate and host Archipelago multiworlds';"
        "$s.Save()"
    )
    done = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True)
    if done.returncode != 0 or not os.path.isfile(link):
        raise RuntimeError((done.stderr or done.stdout or "no output").strip()[:300])
    return link


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Make the desktop shortcut.")
    ap.add_argument("--start-menu", action="store_true",
                    help="also add it to the Start Menu")
    ap.add_argument("--where", action="store_true",
                    help="print the paths and change nothing")
    args = ap.parse_args(argv)

    for needed in (TARGET_SCRIPT, ICON):
        if not os.path.isfile(needed):
            print(f"missing: {needed}", file=sys.stderr)
            return 1

    targets = [desktop_dir()] + ([start_menu_dir()] if args.start_menu else [])
    if args.where:
        print(f"launcher : {pythonw()} {TARGET_SCRIPT}")
        print(f"icon     : {ICON}")
        for folder in targets:
            print(f"shortcut : {os.path.join(folder, NAME + '.lnk')}")
        return 0

    for folder in targets:
        try:
            print("created", create(folder))
        except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as exc:
            print(f"could not write into {folder}: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
