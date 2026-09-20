r"""Light and dark palettes for the tkinter front end.

ttk's native Windows themes ("vista", "winnative") draw their widgets with the
operating system's own renderer, which ignores background and foreground
settings - so a dark mode built on top of them ends up with black text on a
white button. "clam" is drawn by Tk itself and honours every colour we set, so
both palettes are built on clam and the two modes stay symmetrical.

    import theme
    palette = theme.apply(root, "dark")

The preference lives in ui.json beside the app. "auto" follows the Windows
apps-use-light-theme setting and is the default.

    python theme.py        # print the palettes and what auto resolves to
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PREF_FILE = os.path.join(HERE, "ui.json")
MODES = ("auto", "light", "dark")

PALETTES = {
    "light": {
        "bg": "#f4f4f5",        # window and frames
        "surface": "#fbfbfc",   # raised areas: buttons, headings
        "field": "#ffffff",     # things you type in or scroll: entry, text, table
        "fg": "#1b1c1e",
        "muted": "#6b6b6b",     # hints and the Generate-is-off reason
        "border": "#c4c6cb",
        "sel_bg": "#cddffa",
        "sel_fg": "#101114",
        "accent": "#2f6fd0",    # progress bar
        "missing": "#b3261e",   # no installed world for this game
        "match": "#7a5200",     # ships client code: every player needs the file
        "out": "#9a9a9a",       # sitting out of the next seed
    },
    "dark": {
        "bg": "#1f2124",
        "surface": "#2a2d31",
        "field": "#17181b",
        "fg": "#e6e8ec",
        "muted": "#9aa0a6",
        "border": "#3c4046",
        "sel_bg": "#33506f",
        "sel_fg": "#ffffff",
        "accent": "#5a9bea",
        # Lightened from the light-mode versions: the same reds and browns go
        # muddy and unreadable against a dark field.
        "missing": "#ff8a80",
        "match": "#e6b455",
        "out": "#71767c",
    },
}


def system_mode() -> str:
    """What Windows itself is set to, falling back to light when unknown."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        with key:
            light, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return "light" if light else "dark"
    except (ImportError, OSError, FileNotFoundError):
        return "light"


def resolve(mode: str) -> str:
    """Turn a preference into a palette name."""
    if mode == "auto":
        return system_mode()
    return mode if mode in PALETTES else "light"


def load_pref() -> str:
    try:
        with open(PREF_FILE, encoding="utf-8") as fh:
            mode = json.load(fh).get("theme")
    except (OSError, ValueError, AttributeError):
        return "auto"
    return mode if mode in MODES else "auto"


def save_pref(mode: str) -> None:
    """Best effort - a read-only install should not stop the app from running."""
    if mode not in MODES:
        raise ValueError(f"unknown theme mode: {mode!r}")
    data = {}
    try:
        with open(PREF_FILE, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        pass
    data["theme"] = mode
    tmp = PREF_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, PREF_FILE)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def apply(root, mode: str) -> dict:
    """Paint every ttk class, and return the palette for the plain-tk widgets.

    tk.Text, tk.Menu and the Treeview row tags are not ttk and keep their own
    colours, so the caller has to set those from the returned palette.
    """
    from tkinter import ttk

    p = PALETTES[resolve(mode)]
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:                                  # pragma: no cover
        pass

    root.configure(background=p["bg"])
    style.configure(".", background=p["bg"], foreground=p["fg"],
                    fieldbackground=p["field"], bordercolor=p["border"],
                    lightcolor=p["surface"], darkcolor=p["border"],
                    troughcolor=p["field"], insertcolor=p["fg"],
                    focuscolor=p["accent"])
    style.map(".", foreground=[("disabled", p["muted"])])

    style.configure("TFrame", background=p["bg"])
    style.configure("TLabel", background=p["bg"], foreground=p["fg"])
    style.configure("TLabelframe", background=p["bg"], bordercolor=p["border"])
    style.configure("TLabelframe.Label", background=p["bg"], foreground=p["fg"])
    style.configure("TCheckbutton", background=p["bg"], foreground=p["fg"])
    style.map("TCheckbutton",
              background=[("active", p["bg"])],
              indicatorcolor=[("selected", p["accent"]), ("!selected", p["field"])])

    for cls in ("TButton", "TMenubutton"):
        style.configure(cls, background=p["surface"], foreground=p["fg"],
                        bordercolor=p["border"], lightcolor=p["surface"],
                        darkcolor=p["border"], focusthickness=0)
        style.map(cls,
                  background=[("disabled", p["bg"]), ("pressed", p["sel_bg"]),
                              ("active", p["sel_bg"])],
                  foreground=[("disabled", p["muted"])])

    style.configure("TEntry", fieldbackground=p["field"], foreground=p["fg"],
                    insertcolor=p["fg"], bordercolor=p["border"])
    style.map("TEntry", fieldbackground=[("disabled", p["bg"])])

    style.configure("Treeview", background=p["field"], fieldbackground=p["field"],
                    foreground=p["fg"], bordercolor=p["border"], rowheight=21)
    style.map("Treeview",
              background=[("selected", p["sel_bg"])],
              foreground=[("selected", p["sel_fg"])])
    style.configure("Treeview.Heading", background=p["surface"], foreground=p["fg"],
                    relief="flat")
    style.map("Treeview.Heading", background=[("active", p["sel_bg"])])

    style.configure("Vertical.TScrollbar", background=p["surface"],
                    troughcolor=p["bg"], bordercolor=p["border"],
                    arrowcolor=p["fg"])
    style.map("Vertical.TScrollbar", background=[("active", p["sel_bg"])])
    style.configure("Horizontal.TProgressbar", background=p["accent"],
                    troughcolor=p["field"], bordercolor=p["border"],
                    lightcolor=p["accent"], darkcolor=p["accent"])

    # A named style for the grey hint labels, so they follow the palette
    # instead of being frozen at a light-mode grey.
    style.configure("Muted.TLabel", background=p["bg"], foreground=p["muted"])

    # Dialogs (messagebox, filedialog) are drawn by Tk from the option
    # database, not by ttk, so they need telling separately.
    for pattern, value in (("*Dialog.msg.background", p["bg"]),
                           ("*Dialog.msg.foreground", p["fg"]),
                           ("*Menu.background", p["surface"]),
                           ("*Menu.foreground", p["fg"]),
                           ("*Menu.activeBackground", p["sel_bg"]),
                           ("*Menu.activeForeground", p["sel_fg"])):
        root.option_add(pattern, value)
    return p


def main() -> int:
    print(f"preference: {load_pref()}   windows: {system_mode()}")
    for name, p in PALETTES.items():
        print(f"\n{name}")
        for key, value in p.items():
            print(f"  {key:<9} {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
