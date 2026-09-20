"""Ways to get player configs into the local lobby.

Every importer returns the same thing - a list of (bytes, source) pairs - so
the lobby does not care where a config came from, and adding a new source
means adding one function here and nothing anywhere else.

The `source` dict always carries:

    kind  which importer produced it
    id    a *stable* identifier for this config within that kind

`id` is what makes re-importing safe. The lobby matches on it before it looks
at names, so a player who renames themselves is recognised as the same person
rather than arriving as a duplicate. For a room that id is the yaml-id already
present on each row; for a file it is the path it came from.

A remote room is one importer among several here, deliberately: it is a way to
put configs in, not the thing that defines the lobby.

Standard library only.
"""
from __future__ import annotations

import io
import os
import urllib.error
import zipfile

import aplobby as core

LOBBY = "https://ap-lobby.ionium.us"

YAML_EXT = (".yaml", ".yml")


class SourceError(Exception):
    """An import source could not be read, with a reason worth showing."""


def _is_yaml(name: str) -> bool:
    return name.lower().endswith(YAML_EXT)


# ------------------------------------------------------------------- local

def files(paths) -> list[tuple[bytes, dict]]:
    """Explicit config files, in the order given."""
    out = []
    for p in paths:
        p = os.path.abspath(p)
        if not os.path.isfile(p):
            raise SourceError(f"not a file: {p}")
        out.append((open(p, "rb").read(),
                    {"kind": "file", "id": p, "name": os.path.basename(p)}))
    return out


def folder(path: str) -> list[tuple[bytes, dict]]:
    """Every config directly inside a folder.

    Not recursive: an Archipelago Players directory is flat, and walking into
    subfolders would quietly pick up backups and old runs.
    """
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise SourceError(f"not a folder: {path}")
    names = sorted(n for n in os.listdir(path)
                   if _is_yaml(n) and os.path.isfile(os.path.join(path, n)))
    if not names:
        raise SourceError(f"no .yaml files directly inside {path}")
    return [(open(os.path.join(path, n), "rb").read(),
             {"kind": "folder", "id": os.path.join(path, n), "name": n})
            for n in names]


def archive(path: str) -> list[tuple[bytes, dict]]:
    """Every config inside a zip, at any depth."""
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise SourceError(f"not a file: {path}")
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise SourceError(f"not a readable zip: {path} ({exc})")
    members = [m for m in zf.namelist()
               if _is_yaml(m) and not m.endswith("/") and "__MACOSX/" not in m]
    if not members:
        raise SourceError(f"no .yaml files inside {path}")
    return [(zf.read(m), {"kind": "zip", "id": f"{path}!{m}",
                          "archive": path, "name": os.path.basename(m)})
            for m in sorted(members)]


# ------------------------------------------------------------------ remote

def ionium_room(value: str, progress=None) -> list[tuple[bytes, dict]]:
    """Every config in a lobby room.

    `value` may be a full room URL or a bare id. `progress(i, total, player)`
    is called as each config arrives, so a caller can show something.

    The room's own yaml-id is carried through as the source id. It is the only
    identifier here that survives a player renaming themselves, which is what
    lets a re-import update that player in place instead of duplicating them.
    """
    room = core.room_id(value)
    try:
        roster = core.scrape_roster(room)
    except (urllib.error.URLError, OSError) as exc:
        raise SourceError(f"could not reach the lobby: {exc}")
    except RuntimeError as exc:
        raise SourceError(str(exc))

    out = []
    for i, (player, game, yid) in enumerate(roster, 1):
        try:
            data = core.fetch(f"{LOBBY}/room/{room}/download/{yid}")
        except (urllib.error.URLError, OSError) as exc:
            raise SourceError(f"could not download {player}'s config: {exc}")
        out.append((data, {"kind": "ionium", "id": yid, "room": room,
                           "name": player, "game": game}))
        if progress:
            progress(i, len(roster), player)
    return out


# ------------------------------------------------------------------ display

def describe(source: dict) -> str:
    """One short line naming where a config came from, for a person to read."""
    kind = source.get("kind")
    if kind == "ionium":
        return f"lobby room {str(source.get('room'))[:8]}"
    if kind == "zip":
        return f"zip {os.path.basename(source.get('archive', ''))}"
    if kind in ("folder", "file"):
        return source.get("name") or os.path.basename(source.get("id", ""))
    if kind == "run-import":
        return f"earlier run {source.get('run', '')}"
    if kind == "adopted":
        return "adopted from the lobby folder"
    return kind or "unknown"
