r"""Generate an Archipelago multiworld from the local lobby.

    python aplobby.py import room <url>     add a lobby room's configs
    python aplobby.py import folder <dir>   add a folder of configs
    python aplobby.py list                  show the roster
    python aplobby.py generate              build the seed

The lobby on this machine owns the roster; a remote room is one way to put
configs into it, not the thing that defines it. generate needs no network.

Options for generate:
    --ap DIR         Archipelago install (default C:\ProgramData\Archipelago)
    --out DIR        where to put the run (default ./runs/<timestamp>)
    --dry-run        check the roster and worlds, writing nothing
    --allow-missing  generate anyway when some games have no world installed
    --force          generate although the lobby reports problems
    --timeout SECS   generation timeout (default 1800)

Exit codes: 0 generated, 1 preflight failed, 2 generation failed,
            3 import source unreachable, 4 generated but settings were dropped.

Standard library only.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import html
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

import lobby
import sources

HERE = os.path.dirname(os.path.abspath(__file__))
AP_DEFAULT = r"C:\ProgramData\Archipelago"
LOBBY = "https://ap-lobby.ionium.us"
UA = {"User-Agent": "aplobby-gen"}

sha256 = lambda b: hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------- lobby

def room_id(value: str) -> str:
    """Accept a full room URL or a bare id.

    Raises ValueError, not SystemExit: this is called from a dialog as well as
    from the command line, and a library function should not decide to end the
    process.
    """
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", value)
    if not m:
        raise ValueError(f"could not find a room id in {value!r}")
    return m.group(1)


def fetch(url: str, timeout: int = 30) -> bytes:
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()


def parse_roster(page: str):
    """[(player, game, yaml_id)] from a room page's HTML.

    Split from the fetch so it can be exercised against a saved page with no
    network - every test below this line runs offline.

    The yaml id is a data attribute on each <tr>, which survives markup churn
    better than the anchor href. Raises when nothing parses: an empty room and
    a changed layout look identical from here, and silently returning [] would
    let a markup change generate a seed with nobody in it.
    """
    rows = []
    for tag, body in re.findall("<tr([^>]*)>(.*?)</tr>", page, re.S):
        yid = re.search('data-yaml-id="([0-9a-f-]{36})"', tag)
        if not yid:
            continue
        cells = re.findall("<td[^>]*>(.*?)</td>", body, re.S)
        if len(cells) < 2:
            continue
        clean = lambda s: html.unescape(re.sub("<[^>]+>", " ", s)).strip()
        name = re.search('class="player-name"[^>]*>(.*?)<', body, re.S)
        rows.append((clean(name.group(1)) if name else clean(cells[0]),
                     clean(cells[1]), yid.group(1)))
    if not rows:
        raise RuntimeError("parsed 0 player rows from the room page - the room is "
                           "empty, or its markup changed")
    return rows


def scrape_roster(room: str):
    """Fetch a room page and parse its roster."""
    return parse_roster(fetch(f"{LOBBY}/room/{room}").decode("utf-8", "replace"))


def safe_name(player: str, game: str) -> str:
    keep = lambda s: re.sub(r"[^A-Za-z0-9]+", "", s)
    return f"{keep(player)}_{keep(game)}.yaml"


def yaml_fields(data: bytes):
    """(name, game) from a config, without a YAML parser.

    Only the two top-level scalars are needed, and reading them by hand keeps
    this dependency-free. Handles quoted and unquoted values.
    """
    # utf-8-sig, not utf-8: Windows editors save YAML with a byte-order mark,
    # and a BOM sits between the start of the file and "name:", so "^name:"
    # never matches and a perfectly good config reads as having no name.
    text = data.decode("utf-8-sig", "replace")
    grab = lambda key: next(
        (m.group(1).strip().strip("\"'")
         for m in re.finditer(rf"^{key}:\s*(.+?)\s*$", text, re.M)), None)
    return grab("name"), grab("game")


# ---------------------------------------------------------------- worlds

def world_game(data: bytes):
    """The game string a world file declares, and whether it ships client code."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return None, False
    names = z.namelist()
    client = any(n.lower().endswith("/client.py") for n in names)
    manifest = next((n for n in names if n.endswith("archipelago.json")), None)
    if manifest:
        try:
            game = json.loads(z.read(manifest)).get("game")
            if game:
                return game, client
        except Exception:
            pass
    init = next((n for n in names if n.endswith("__init__.py") and n.count("/") == 1), None)
    if init:
        src = z.read(init).decode("utf-8-sig", "replace")
        for m in re.finditer(r'^\s*game\s*(?::\s*str)?\s*=\s*["\'](.+?)["\']', src, re.M):
            candidate = m.group(1)
            if "/" not in candidate:
                return candidate, client
    return None, client


def ap_version(ap_dir: str):
    """The installed Archipelago version, or None if it cannot be read.

    Shared by both lock writers so they cannot disagree about it, which they
    did: the CLI recorded it and the GUI silently omitted it.
    """
    try:
        blob = json.load(open(os.path.join(ap_dir, "manifest.json"), encoding="utf-8"))
        return ".".join(map(str, blob["version"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def index_worlds(ap_dir: str):
    """{game name: {...}} across custom_worlds and the bundled worlds."""
    index = {}
    for folder, source in ((os.path.join(ap_dir, "custom_worlds"), "custom"),
                           (os.path.join(ap_dir, "lib", "worlds"), "core")):
        if not os.path.isdir(folder):
            continue
        for fn in sorted(os.listdir(folder)):
            if not fn.endswith(".apworld"):
                continue
            path = os.path.join(folder, fn)
            data = open(path, "rb").read()
            game, client = world_game(data)
            if not game:
                continue
            # custom_worlds wins: it is what the generator prefers too
            if game in index and source == "core":
                continue
            index[game] = {"file": fn, "path": path, "source": source,
                           "bytes": len(data), "sha256": sha256(data),
                           "ships_client_code": client}
    return index


# ---------------------------------------------------------------- preflight

def preflight(players, ap_dir):
    """Resolve each staged player's game to an installed world file.

    `players` are the records stage() produced; each gains 'world' and
    'must_match'. Returns (index, used, missing).

    One implementation, used by both the command line and the GUI - they used
    to each decide this for themselves and had already drifted apart.
    """
    index = index_worlds(ap_dir)
    used, missing = {}, []
    for p in players:
        hit = index.get(p.get("game"))
        p["world"] = hit["file"] if hit else None
        p["must_match"] = bool(hit and hit["ships_client_code"])
        if hit:
            used[p["game"]] = hit
        else:
            missing.append(p)
    return index, used, missing


def run_generation(players_dir, output_dir, ap_dir, timeout, log_path):
    """Invoke the generator. Returns (seed_zip or None, log text, exit code)."""
    exe = os.path.join(ap_dir, "ArchipelagoGenerate.exe")
    if not os.path.isfile(exe):
        raise FileNotFoundError(exe)
    with open(log_path, "wb") as log:
        proc = subprocess.run([exe, "--player_files_path", players_dir,
                               "--outputpath", output_dir],
                              stdout=log, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, timeout=timeout)
    text = open(log_path, encoding="utf-8", errors="replace").read()
    zips = sorted(f for f in os.listdir(output_dir) if f.endswith(".zip"))
    return (os.path.join(output_dir, zips[0]) if zips else None), text, proc.returncode


def extract_spoiler(seed_zip, out_dir):
    """Lift the spoiler out of the seed so it survives without unpacking."""
    try:
        zf = zipfile.ZipFile(seed_zip)
        inner = next((n for n in zf.namelist() if n.endswith("_Spoiler.txt")), None)
        if not inner:
            return None
        path = os.path.join(out_dir, os.path.basename(inner))
        with open(path, "wb") as fh:
            fh.write(zf.read(inner))
        return path
    except (OSError, zipfile.BadZipFile):
        return None


def warnings_from(log_text):
    return [l.strip() for l in log_text.splitlines()
            if "not a valid option" in l or "Invalid or missing manifest" in l]


def run_sources(players):
    """Where this roster came from, coarsely - one record per origin.

    Per-file ids belong in the lobby, not in a run lock: what a run wants to
    record is 'these configs came from that room', not twenty-one paths.
    """
    seen, out = set(), []
    for p in players:
        s = p.get("source") or {}
        key = (s.get("kind"), s.get("room") or s.get("archive") or "")
        if key in seen:
            continue
        seen.add(key)
        rec = {"kind": s.get("kind")}
        for extra in ("room", "archive", "run"):
            if s.get(extra):
                rec[extra] = s[extra]
        out.append(rec)
    return out


def write_lock(path, *, players, used, warnings, ap_dir, seed_zip, spoiler, excluded):
    """Record exactly what went into this seed, beside the seed."""
    lock = {
        "schema": "aplobby-run/2",
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "archipelago_version": ap_version(ap_dir),
        "sources": run_sources(players),
        "seed_zip": os.path.basename(seed_zip) if seed_zip else None,
        "seed_sha256": sha256(open(seed_zip, "rb").read()) if seed_zip else None,
        "spoiler": os.path.basename(spoiler) if spoiler else None,
        "players": [{k: v for k, v in p.items() if k != "must_match"} for p in players],
        "excluded": excluded,
        "worlds": {g: {k: v for k, v in w.items() if k != "path"} for g, w in used.items()},
        "warnings": warnings,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(lock, fh, indent=2, ensure_ascii=False)
    return lock


def new_run_dir(base=None):
    """A fresh runs/<timestamp> directory, created only when it is needed.

    Made with mkdir rather than makedirs(exist_ok=True) so two generations
    started in the same second cannot land in one directory - the old room-id
    prefix hid that by accident.
    """
    base = base or os.path.join(HERE, "runs")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    for n in range(1, 50):
        path = os.path.join(base, stamp if n == 1 else f"{stamp}-{n}")
        try:
            os.makedirs(path)
            return path
        except FileExistsError:
            continue
    raise RuntimeError(f"could not make a run directory under {base}")


# ---------------------------------------------------------------- commands

def cmd_import(args) -> int:
    """Pull configs from a source into the local lobby."""
    try:
        if args.what == "room":
            got = sources.ionium_room(args.target,
                                      progress=lambda i, n, who:
                                      print(f"  {i:>3}/{n}  {who}"))
        elif args.what == "folder":
            got = sources.folder(args.target)
        elif args.what == "zip":
            got = sources.archive(args.target)
        else:
            got = sources.files(args.targets)
    except (sources.SourceError, ValueError) as exc:
        print(f"could not read that source: {exc}")
        return 3

    tally = {"added": 0, "updated": 0, "unchanged": 0}
    with lobby.locked(args.lobby) as lb:
        for data, src in got:
            action, entry = lb.upsert(data, src)
            tally[action] += 1
            if action != "unchanged":
                print(f"  {action:9} {entry['slot']}  {entry.get('name') or '?'}"
                      f"  ({entry.get('game') or 'no game: line'})")
        print(f"\n{tally['added']} added, {tally['updated']} updated, "
              f"{tally['unchanged']} unchanged - {len(lb.entries)} in the lobby")
        for p in lb.problems():
            print(f"  ! {p}")
    return 0


def cmd_list(args) -> int:
    """Show the lobby, and whether each game has a world installed."""
    with lobby.locked(args.lobby) as lb:
        if not lb.entries:
            print("the lobby is empty - import a room, a folder or a zip")
            return 0
        index = index_worlds(args.ap) if args.ap else {}
        print(f"{'slot':6} {'':1} {'player':22} {'game':30} world")
        for e in sorted(lb.entries, key=lambda x: x["slot"]):
            hit = index.get(e.get("game"))
            mark = " " if e.get("enabled", True) else "-"
            world = hit["file"] if hit else ("" if not index else "NOT INSTALLED")
            flag = "!" if e.get("missing") else " "
            print(f"{e['slot']:6} {mark}{flag}{(e.get('name') or '?')[:22]:22} "
                  f"{(e.get('game') or '?')[:30]:30} {world}")
        off = [e for e in lb.entries if not e.get("enabled", True)]
        print(f"\n{len(lb.enabled())} enabled" + (f", {len(off)} sitting out" if off else ""))
        for p in lb.problems():
            print(f"  ! {p}")
    return 0


def cmd_slot(args) -> int:
    """enable / disable / remove one slot."""
    with lobby.locked(args.lobby) as lb:
        try:
            if args.action == "remove":
                e = lb.remove(args.slot)
                print(f"removed {e['slot']} ({e.get('name')}) - its history is kept")
            else:
                e = lb.set_enabled(args.slot, args.action == "enable")
                print(f"{e['slot']} ({e.get('name')}) is now "
                      f"{'in' if e['enabled'] else 'sitting out'}")
        except lobby.LobbyError as exc:
            print(exc)
            return 1
    return 0


def cmd_generate(args) -> int:
    """Generate a seed from the local lobby. No network involved."""
    with lobby.locked(args.lobby) as lb:
        if not lb.enabled():
            print("nothing to generate - the lobby has no enabled players")
            return 1
        problems = lb.problems()
        for p in problems:
            print(f"  ! {p}")
        if problems and not args.force:
            print("\nFix those, or pass --force to generate anyway.")
            return 1

        # Preflight off the roster itself. Nothing is written until we know
        # the run is actually going ahead - the old code created a directory
        # first and left one behind every time it stopped early.
        players = lb.records()
        excluded = [{"slot": e["slot"], "player": e.get("name")}
                    for e in lb.entries if not e.get("enabled", True)]
        print(f"roster {len(players)} players"
              + (f", {len(excluded)} sitting out" if excluded else ""))

        index, used, missing = preflight(players, args.ap)
        print(f"worlds {len(index)} games installed\n")
        for p in sorted(players, key=lambda x: (x["game"] is None, x["game"] or "")):
            if p["world"]:
                print(f"  ok  {'*' if p['must_match'] else ' '} "
                      f"{(p['game'] or '?')[:38]:38} {p['world']}")
            else:
                print(f"  --    {(p['game'] or '?')[:38]:38} NO WORLD INSTALLED")

        if missing and not args.allow_missing:
            print(f"\n{len(missing)} game(s) have no installed world:")
            for p in missing:
                print(f"  {p.get('player')}  {p.get('game')}")
            print("\nInstall them into custom_worlds, or pass --allow-missing.")
            return 1

        if args.dry_run:
            print("\ndry run - nothing written")
            return 0

        out = args.out or new_run_dir()
        players_dir = os.path.join(out, "Players")
        output_dir = os.path.join(out, "output")
        os.makedirs(output_dir, exist_ok=True)
        print(f"out    {out}")
        staged = {r["slot"]: r["yaml"] for r in lb.stage(players_dir)}
        for p in players:
            p["yaml"] = staged[p["slot"]]

    print("\ngenerating...")
    try:
        seed_zip, log_text, code = run_generation(
            players_dir, output_dir, args.ap, args.timeout,
            os.path.join(out, "generate.log"))
    except FileNotFoundError as exc:
        print(f"generator not found: {exc}")
        return 2

    if not seed_zip:
        print(f"generation failed (exit {code}) - see {os.path.join(out, 'generate.log')}")
        for line in log_text.splitlines():
            if re.search(r"Exception|Error|invalid", line):
                print("  " + line.strip()[:160])
        return 2

    warn = warnings_from(log_text)
    spoiler = extract_spoiler(seed_zip, out)
    write_lock(os.path.join(out, "run.lock.json"), players=players, used=used,
               warnings=warn, ap_dir=args.ap, seed_zip=seed_zip,
               spoiler=spoiler, excluded=excluded)

    print(f"\nseed   {seed_zip}")
    print(f"       {os.path.getsize(seed_zip):,} bytes")
    print(f"lock   {os.path.join(out, 'run.lock.json')}")
    if spoiler:
        print(f"spoiler {spoiler}")
        print(f"        {os.path.getsize(spoiler):,} bytes - full playthrough, do not share")
    must = sorted(w["file"] for w in used.values() if w["ships_client_code"])
    if must:
        print(f"\nplayers of these must have the identical world file: {', '.join(must)}")

    # A dropped option is not a crash: the seed generates and looks fine, but a
    # player quietly does not get the game they configured. That has happened
    # four times in one room, so treat it as a failure unless waived.
    dropped = [w for w in warn if "not a valid option" in w]
    if dropped:
        print(f"\n{len(dropped)} setting(s) were silently dropped - a config expects a "
              "newer world than the one installed:")
        for w in dropped[:10]:
            print("  " + w[:160])
        if not args.warn_ok:
            print("\nThe seed is usable, but those players are not getting what they "
                  "asked for. Update the world and re-run, or pass --warn-ok.")
            return 4
    return 0


# ---------------------------------------------------------------- entry point

def build_parser():
    ap = argparse.ArgumentParser(
        prog="aplobby",
        description="Generate an Archipelago multiworld from the local lobby.")
    ap.add_argument("--lobby", default=None,
                    help="lobby folder (default ./lobby)")
    sub = ap.add_subparsers(dest="cmd")

    imp = sub.add_parser("import", help="add configs to the lobby")
    imps = imp.add_subparsers(dest="what", required=True)
    for what, meta in (("room", "room URL or id"), ("folder", "folder of .yaml files"),
                       ("zip", "zip containing .yaml files")):
        q = imps.add_parser(what)
        q.add_argument("target", metavar=meta)
    q = imps.add_parser("file")
    q.add_argument("targets", nargs="+", metavar="config.yaml")
    imp.set_defaults(func=cmd_import)

    ls = sub.add_parser("list", help="show the lobby")
    ls.add_argument("--ap", default=AP_DEFAULT)
    ls.set_defaults(func=cmd_list)

    for action in ("enable", "disable", "remove"):
        s = sub.add_parser(action, help=f"{action} one slot")
        s.add_argument("slot")
        s.set_defaults(func=cmd_slot, action=action)

    gen = sub.add_parser("generate", help="generate a seed from the lobby")
    gen.add_argument("--ap", default=AP_DEFAULT)
    gen.add_argument("--out")
    gen.add_argument("--dry-run", action="store_true")
    gen.add_argument("--allow-missing", action="store_true")
    gen.add_argument("--force", action="store_true",
                     help="generate even though the lobby reports problems")
    gen.add_argument("--timeout", type=int, default=1800)
    gen.add_argument("--warn-ok", action="store_true",
                     help="exit 0 even when a config had settings silently dropped")
    gen.set_defaults(func=cmd_generate)
    return ap


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Compatibility: this tool used to take a bare room URL and do everything.
    # Keep that working rather than erroring on muscle memory.
    known = {"import", "list", "generate", "enable", "disable", "remove", "-h", "--help"}
    if argv and argv[0] not in known and not argv[0].startswith("-"):
        try:
            room_id(argv[0])
        except ValueError:
            pass
        else:
            print(f"note: '{argv[0]}' read as 'import room' followed by 'generate'\n")
            rc = main(["import", "room", argv[0]])
            return rc if rc else main(["generate"] + argv[1:])

    args = build_parser().parse_args(argv)
    if not getattr(args, "cmd", None):
        build_parser().print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
