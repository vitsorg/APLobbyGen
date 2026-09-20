"""Pull an Archipelago lobby room's YAMLs, check the worlds exist, generate the seed.

    python aplobby.py <room url or id>

Runs the whole pipeline: scrape the roster, download every config, resolve each
game to an installed world file, generate, and write a lock manifest recording
exactly what went in. Standard library only.

Options:
    --ap DIR         Archipelago install (default C:\\ProgramData\\Archipelago)
    --out DIR        where to put the run (default ./runs/<room>-<timestamp>)
    --dry-run        pull and check, but do not generate
    --allow-missing  generate anyway when some games have no world installed
    --timeout SECS   generation timeout (default 1800)

Exit codes: 0 generated, 1 preflight failed, 2 generation failed,
            3 lobby unreachable, 4 generated but settings were silently dropped.
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

LOBBY = "https://ap-lobby.ionium.us"
UA = {"User-Agent": "aplobby-gen"}

# Options every world accepts; they never indicate a missing world.
sha256 = lambda b: hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------- lobby

def room_id(value: str) -> str:
    """Accept a full room URL or a bare id."""
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", value)
    if not m:
        raise SystemExit(f"could not find a room id in {value!r}")
    return m.group(1)


def fetch(url: str, timeout: int = 30) -> bytes:
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()


def scrape_roster(room: str):
    """[(player, game, yaml_id)] from the room page.

    The yaml id is a data attribute on each <tr>, which survives markup churn
    better than the anchor href. Raises when nothing parses, so a layout change
    can never look like an empty room.
    """
    page = fetch(f"{LOBBY}/room/{room}").decode("utf-8", "replace")
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
        raise RuntimeError("parsed 0 rows from the room page - markup likely changed")
    return rows


def safe_name(player: str, game: str) -> str:
    keep = lambda s: re.sub(r"[^A-Za-z0-9]+", "", s)
    return f"{keep(player)}_{keep(game)}.yaml"


def yaml_fields(data: bytes):
    """(name, game) from a config, without a YAML parser.

    Only the two top-level scalars are needed, and reading them by hand keeps
    this dependency-free. Handles quoted and unquoted values.
    """
    text = data.decode("utf-8", "replace")
    grab = lambda key: next(
        (m.group(1).strip().strip("\"'")
         for m in re.finditer(rf"^{key}:\s*(.+?)\s*$", text, re.M)), None)
    return grab("name"), grab("game")


def collect_extras(extras_dir: str, players_dir: str):
    """Copy hand-added configs into the run, after the lobby pull.

    Anything in extras/ joins every run. A file whose name collides with a
    pulled config is kept under a suffixed name rather than overwriting it -
    the lobby's copy is the one the room actually validated.
    """
    if not os.path.isdir(extras_dir):
        return []
    added = []
    for fn in sorted(os.listdir(extras_dir)):
        if not fn.lower().endswith((".yaml", ".yml")):
            continue
        src = os.path.join(extras_dir, fn)
        data = open(src, "rb").read()
        name, game = yaml_fields(data)
        dest_name = fn
        if os.path.exists(os.path.join(players_dir, dest_name)):
            stem, ext = os.path.splitext(fn)
            dest_name = f"{stem}_extra{ext}"
        with open(os.path.join(players_dir, dest_name), "wb") as fh:
            fh.write(data)
        added.append({"player": name or f"(unnamed: {fn})", "game": game,
                      "yaml": dest_name, "yaml_bytes": len(data),
                      "yaml_sha256": sha256(data), "source": "extras"})
    return added


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
        src = z.read(init).decode("utf-8", "replace")
        for m in re.finditer(r'^\s*game\s*(?::\s*str)?\s*=\s*["\'](.+?)["\']', src, re.M):
            candidate = m.group(1)
            if "/" not in candidate:
                return candidate, client
    return None, client


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


# ---------------------------------------------------------------- pipeline

def main() -> int:
    ap = argparse.ArgumentParser(description="Pull a lobby room and generate its seed.")
    ap.add_argument("room", help="room URL or id")
    ap.add_argument("--ap", default=r"C:\ProgramData\Archipelago")
    ap.add_argument("--out")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-missing", action="store_true")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--extras", help="folder of hand-added configs to include "
                    "(default ./extras)")
    ap.add_argument("--no-extras", action="store_true",
                    help="ignore the extras folder for this run")
    ap.add_argument("--warn-ok", action="store_true",
                    help="exit 0 even when a config had settings silently dropped")
    args = ap.parse_args()

    room = room_id(args.room)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "runs", f"{room[:8]}-{stamp}")
    players_dir = os.path.join(out, "Players")
    output_dir = os.path.join(out, "output")
    os.makedirs(players_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    print(f"room   {room}")
    print(f"out    {out}\n")

    # 1. roster ---------------------------------------------------------
    try:
        roster = scrape_roster(room)
    except (urllib.error.URLError, RuntimeError) as exc:
        print(f"could not read the room: {exc}")
        return 3
    print(f"roster {len(roster)} players")

    # 2. configs --------------------------------------------------------
    entries = []
    for player, game, yid in roster:
        data = fetch(f"{LOBBY}/room/{room}/download/{yid}")
        fn = safe_name(player, game)
        with open(os.path.join(players_dir, fn), "wb") as fh:
            fh.write(data)
        entries.append({"player": player, "game": game, "yaml": fn,
                        "yaml_bytes": len(data), "yaml_sha256": sha256(data)})
    print(f"pulled {len(entries)} configs")

    # 2b. hand-added configs --------------------------------------------
    extras_dir = args.extras or os.path.join(os.path.dirname(os.path.abspath(__file__)), "extras")
    if not args.no_extras:
        extra = collect_extras(extras_dir, players_dir)
        for e in extra:
            flag = "" if e["game"] else "   <- no 'game:' line, generation will reject it"
            print(f"  + extra  {e['player']:20} {e['game'] or '?'}{flag}")
        entries += extra
        if extra:
            print(f"added {len(extra)} config(s) from {extras_dir}")
    print()

    # 3. preflight ------------------------------------------------------
    index = index_worlds(args.ap)
    print(f"worlds {len(index)} games installed")
    missing, used = [], {}
    for e in entries:
        hit = index.get(e["game"])
        if hit:
            used[e["game"]] = hit
            e["world"] = hit["file"]
        else:
            missing.append(e)
            e["world"] = None

    for e in sorted(entries, key=lambda x: x["game"]):
        hit = index.get(e["game"])
        if hit:
            mark = "*" if hit["ships_client_code"] else " "
            print(f"  ok  {mark} {e['game'][:38]:38} {hit['file']}")
        else:
            print(f"  --    {e['game'][:38]:38} NO WORLD INSTALLED")

    if missing:
        print(f"\n{len(missing)} game(s) have no installed world:")
        for e in missing:
            print(f"  {e['player']}  {e['game']}")
        if not args.allow_missing:
            print("\nInstall the missing world files into custom_worlds, or pass "
                  "--allow-missing to generate without them (those players are "
                  "still included and generation will fail).")
            return 1

    if args.dry_run:
        print("\ndry run - stopping before generation")
        return 0

    # 4. generate -------------------------------------------------------
    exe = os.path.join(args.ap, "ArchipelagoGenerate.exe")
    if not os.path.isfile(exe):
        print(f"generator not found: {exe}")
        return 2
    log_path = os.path.join(out, "generate.log")
    print("\ngenerating...")
    with open(log_path, "wb") as log:
        proc = subprocess.run([exe, "--player_files_path", players_dir,
                               "--outputpath", output_dir],
                              stdout=log, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, timeout=args.timeout)
    zips = [f for f in os.listdir(output_dir) if f.endswith(".zip")]
    log_text = open(log_path, encoding="utf-8", errors="replace").read()

    if not zips:
        print(f"generation failed (exit {proc.returncode}) - see {log_path}")
        for line in log_text.splitlines():
            if re.search(r"Exception|Error|invalid", line):
                print("  " + line.strip()[:160])
        return 2

    seed_zip = os.path.join(output_dir, zips[0])
    warn = [l.strip() for l in log_text.splitlines()
            if "not a valid option" in l or "Invalid or missing manifest" in l]

    # Lift the spoiler out of the zip so it survives independently and can be
    # read without unpacking. The copy inside the zip stays where it is.
    spoiler_path = None
    try:
        zf = zipfile.ZipFile(seed_zip)
        inner = next((n for n in zf.namelist() if n.endswith("_Spoiler.txt")), None)
        if inner:
            spoiler_path = os.path.join(out, os.path.basename(inner))
            with open(spoiler_path, "wb") as fh:
                fh.write(zf.read(inner))
    except Exception as exc:
        print(f"could not extract the spoiler: {exc}")

    # 5. lock -----------------------------------------------------------
    lock = {
        "schema": "aplobby-run/1",
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "lobby_room": room,
        "archipelago_version": ".".join(map(str, json.load(
            open(os.path.join(args.ap, "manifest.json")))["version"])),
        "seed_zip": os.path.basename(seed_zip),
        "seed_sha256": sha256(open(seed_zip, "rb").read()),
        "spoiler": os.path.basename(spoiler_path) if spoiler_path else None,
        "players": entries,
        "worlds": {g: {k: v for k, v in w.items() if k != "path"} for g, w in used.items()},
        "warnings": warn,
    }
    with open(os.path.join(out, "run.lock.json"), "w") as fh:
        json.dump(lock, fh, indent=2)

    print(f"\nseed   {seed_zip}")
    print(f"       {os.path.getsize(seed_zip):,} bytes")
    print(f"lock   {os.path.join(out, 'run.lock.json')}")
    if spoiler_path:
        print(f"spoiler {spoiler_path}")
        print(f"        {os.path.getsize(spoiler_path):,} bytes - full playthrough, do not share")
    if warn:
        print(f"\n{len(warn)} warning(s):")
        for w in warn:
            print("  " + w[:160])
    must = [w["file"] for w in used.values() if w["ships_client_code"]]
    if must:
        print(f"\nplayers of these must have the identical world file: {', '.join(sorted(must))}")

    # A dropped option is not a crash: the seed generates and looks fine, but a
    # player quietly does not get the game they configured. That has happened
    # four times in this room, so treat it as a failure unless waived.
    dropped = [w for w in warn if "not a valid option" in w]
    if dropped and not args.warn_ok:
        print(f"\n{len(dropped)} setting(s) were silently dropped - a config expects a newer "
              "world than the one installed.")
        print("The seed above is usable, but those players are not getting what they asked for.")
        print("Update the world file and re-run, or pass --warn-ok to accept it.")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
