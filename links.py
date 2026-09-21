r"""Where each game's world file and client actually come from.

This module stores pointers, never payloads: an upstream is a URL and a claim
about it, harvested from what a world file says about itself or curated by
hand in registry.json. Nothing here downloads, unpacks or installs anything -
GitHub release layouts differ enough per project that the adapters would cost
more than they are worth, so the deliverable is an accurate link and a human
does the fetching.

Two links matter per game and they are different things:

  the world file   the .apworld the generator needs, same for everyone
  the client       what the player installs to actually play - which may ship
                   inside the apworld, may be a separate mod, or may not exist
                   because an emulator client covers it

Client state is deliberately four-valued, because "no client is needed" and
"nobody has looked yet" must not render the same:

  bundled   the apworld ships client.py; every player needs the identical file
  none      no separate client - a bundled emulator client covers it
  external  a real client or mod lives somewhere: url, kind, version, note
  unknown   not investigated (the slug is simply absent from registry.json)

    python links.py                 what is known for every installed world
    python links.py --missing       only the ones nobody has investigated
    python links.py --candidates    GitHub URLs each world names, to curate from
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY = os.path.join(HERE, "registry.json")

BUNDLED, NONE, EXTERNAL, UNKNOWN = "bundled", "none", "external", "unknown"


def load(path: str | None = None) -> dict:
    """The registry, or empty maps if it is missing or unreadable.

    A broken registry must not stop the app working: everything it holds is an
    enrichment, and the fallback is honestly saying nothing is known.
    """
    try:
        with open(path or REGISTRY, encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        blob = {}
    for key in ("repos", "clients", "anchors", "no_upstream"):
        blob.setdefault(key, {})
    return blob


def _repo_url(value: str) -> str:
    """Registry repos are stored as owner/name; links need the full URL."""
    if value.startswith("http"):
        return value
    return f"https://github.com/{value}"


def world_upstream(slug: str, registry: dict) -> dict | None:
    """Where the .apworld itself comes from, or None if nowhere known.

    A 'no_upstream' note is not nothing: "distributed via Discord" is a real
    answer and stops someone searching GitHub for a file that is not there.
    """
    if slug in registry["repos"]:
        return {"state": "repo", "repo": registry["repos"][slug],
                "url": _repo_url(registry["repos"][slug])}
    if slug in registry["no_upstream"]:
        return {"state": "no_upstream", "note": registry["no_upstream"][slug]}
    anchor = registry["anchors"].get(slug)
    if anchor and anchor.get("repo"):
        # An anchor's repo is about the engine, not the world - offer it, but
        # never let it masquerade as the world's own upstream.
        return {"state": "anchor", "repo": anchor["repo"],
                "url": _repo_url(anchor["repo"]),
                "note": f"{anchor['name']} {anchor['version']} - {anchor['kind']}"}
    return None


def client(slug: str, registry: dict, ships_client_code: bool | None = None) -> dict:
    """What the player needs installed to play, as far as anyone has looked.

    `ships_client_code` comes from the installed world file, so the bundled
    case is derived rather than stored - a curated 'bundled' entry would go
    stale the moment a world started or stopped shipping client.py.
    """
    entry = registry["clients"].get(slug)
    if entry:
        out = dict(entry)
        out.setdefault("state", EXTERNAL)
        if out.get("repo") and not out.get("url"):
            out["url"] = _repo_url(out["repo"])
        out["ships_client_code"] = bool(ships_client_code)
        return out
    if ships_client_code:
        return {"state": BUNDLED, "ships_client_code": True,
                "note": "the apworld ships client.py - every player needs the "
                        "identical world file"}
    return {"state": UNKNOWN, "ships_client_code": bool(ships_client_code),
            "note": "not investigated"}


def for_slug(slug: str, registry: dict, ships_client_code: bool | None = None) -> dict:
    return {"slug": slug,
            "world": world_upstream(slug, registry),
            "client": client(slug, registry, ships_client_code),
            "anchor": registry["anchors"].get(slug)}


def for_row(row: dict, registry: dict) -> dict:
    """The links for one preflight row, which carries the world filename."""
    world = row.get("world") or ""
    slug = world[: -len(".apworld")] if world.endswith(".apworld") else ""
    if not slug:
        return {"slug": None, "world": None,
                "client": {"state": UNKNOWN, "note": "no world file installed"},
                "anchor": None}
    return for_slug(slug, registry, row.get("must_match"))


def urls(info: dict) -> list[tuple[str, str]]:
    """[(label, url)] for the links a person can actually open."""
    out = []
    w = info.get("world") or {}
    if w.get("url"):
        label = "World file upstream" if w["state"] == "repo" else "Engine / provenance"
        out.append((label, w["url"]))
    c = info.get("client") or {}
    if c.get("url"):
        out.append((c.get("name") or "Client", c["url"]))
    return out


def unknown_slugs(index: dict, registry: dict) -> list[str]:
    """Installed worlds nobody has investigated a client for.

    Explicitly not "worlds with no client": this is the to-do list, and it is
    empty only when someone has actually looked at every one.
    """
    out = []
    for hit in index.values():
        slug = hit["file"][: -len(".apworld")]
        if client(slug, registry, hit["ships_client_code"])["state"] == UNKNOWN:
            out.append(slug)
    return sorted(out)


def lobby_slugs(index: dict, root: str | None = None) -> list[str]:
    """The slugs of the games the lobby is actually playing.

    This is what makes "check the games we are playing first" possible without
    check_upstream knowing anything about a lobby: the caller passes this list
    to --first, and the rest of the catalogue still gets checked after it.
    """
    import lobby as store

    try:
        with store.locked(root) as lb:
            games = {e.get("game") for e in lb.entries if e.get("game")}
    except Exception:
        return []
    out = {index[g]["file"][: -len(".apworld")] for g in games if g in index}
    return sorted(out)


def candidates(path: str) -> list[str]:
    """GitHub repos a world file names inside itself, to curate an entry from.

    This is deliberately a human's starting point, not an answer: a world's
    docs link to BizHawk, to Archipelago core and to the issue tracker as
    readily as to its own client.
    """
    import check_upstream

    try:
        _slug, _manifest, repos, _size = check_upstream.read_world(path)
    except Exception:
        return []
    return repos


# ---------------------------------------------------------------- command line

def main(argv=None) -> int:
    import aplobby as core

    ap = argparse.ArgumentParser(description="What is known about each world's upstreams.")
    ap.add_argument("--ap", default=core.AP_DEFAULT)
    ap.add_argument("--missing", action="store_true",
                    help="only worlds whose client nobody has investigated")
    ap.add_argument("--candidates", action="store_true",
                    help="GitHub repos each world names inside itself")
    ap.add_argument("--lobby-first", action="store_true",
                    help="list the games this lobby is playing before the rest")
    args = ap.parse_args(argv)

    registry = load()
    index = core.index_worlds(args.ap)
    if not index:
        print(f"no worlds installed under {args.ap}")
        return 1

    rows = sorted(index.items(), key=lambda kv: kv[1]["file"])
    playing = set(lobby_slugs(index)) if args.lobby_first else set()
    if playing:
        rows.sort(key=lambda kv: (kv[1]["file"][: -len(".apworld")] not in playing,
                                  kv[1]["file"]))
    if args.missing:
        todo = unknown_slugs(index, registry)
        if playing:
            todo.sort(key=lambda s: (s not in playing, s))
            here = [s for s in todo if s in playing]
            print(f"{len(here)} of them are games this lobby is playing: "
                  + (", ".join(here) or "none") + "\n")
        print(f"{len(todo)} of {len(index)} installed worlds have no client entry:\n")
        for slug in todo:
            print(f"  {slug}")
        print("\nAdd them to registry.json under 'clients' as you investigate;"
              "\nabsent means 'not looked at', which is not the same as 'none needed'.")
        return 0

    for game, hit in rows:
        slug = hit["file"][: -len(".apworld")]
        info = for_slug(slug, registry, hit["ships_client_code"])
        w, c = info["world"], info["client"]
        print(f"{slug:24} {game}")
        if w and w.get("url"):
            print(f"  world    {w['url']}")
        elif w:
            print(f"  world    no upstream - {w.get('note', '')}")
        else:
            print("  world    not investigated")
        bits = [c["state"]]
        if c.get("name"):
            bits.append(c["name"])
        if c.get("version"):
            bits.append(c["version"])
        print(f"  client   {' - '.join(bits)}")
        if c.get("url"):
            print(f"           {c['url']}")
        if c.get("note") and c["state"] != EXTERNAL:
            print(f"           {c['note']}")
        if args.candidates:
            for repo in candidates(hit["path"]):
                print(f"  names    {repo}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
