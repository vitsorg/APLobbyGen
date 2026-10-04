r"""Read a game's options out of Archipelago's own template, for editing.

Archipelago generates `Players/Templates/<Game>.yaml` for every installed
world: every option, its documentation, its allowed values and its default,
written by the world itself. That is enough to build an editor for a game
nobody has ever hand-coded support for - which is the whole point, because
there are 85 of those templates on this machine and no chance of curating
forms for each by hand.

Nothing here imports an apworld. Templates are plain text, so this works for
worlds that ship only compiled modules built for another Python - the case that
stops the logic bridge cold.

    python options.py EarthBound            every option, typed
    python options.py EarthBound --values   just key: default

Standard library only.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

BOOL, RANGE, CHOICE, COLLECTION = "bool", "range", "choice", "collection"
BOM = b"\xef\xbb\xbf"          # Windows editors add it; round-trip it faithfully

# Weighted keys the template adds to every option; they are rolls, not values.
RANDOM_KEYS = re.compile(r"^(random|random-low|random-high|random-range-[\d-]+)$")


def _flatten(name: str) -> str:
    """A name with punctuation and case thrown away, for matching filenames."""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def template_path(game: str, ap_dir: str) -> str:
    """The template file for a game, found even if its name was not filename-safe.

    Windows forbids : \\ / * ? " < > | in a filename, so a game whose name
    contains one is written to a template that is not simply "<game>.yaml" -
    "Jak and Daxter: The Precursor Legacy" loses its colon. Matching on the
    name with punctuation ignored finds it without guessing which character
    was dropped, and without a table of special cases.

    Returns the exact path when nothing matches, so the caller's error names
    the file it was looking for.
    """
    folder = os.path.join(ap_dir, "Players", "Templates")
    exact = os.path.join(folder, f"{game}.yaml")
    if os.path.isfile(exact):
        return exact
    want = _flatten(game)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return exact
    for fn in names:
        if fn.lower().endswith(".yaml") and _flatten(fn[: -len(".yaml")]) == want:
            return os.path.join(folder, fn)
    return exact


def available(ap_dir: str, index: dict | None = None) -> list[str]:
    """Games with a generated template.

    With an installed-world `index`, this answers the question a caller
    actually has - which games can I edit and then generate - and so returns
    real game names, filtered to what is installed. Templates outlive the
    worlds that produced them: uninstalling a world leaves its template behind,
    and offering it leads to editing settings for a game that cannot generate.
    A game whose name was not filename-safe is also only reachable this way,
    since its template is not named after it.

    Without an index it falls back to the filename stems, which is all there is
    to go on.
    """
    folder = os.path.join(ap_dir, "Players", "Templates")
    if not os.path.isdir(folder):
        return []
    if index is not None:
        return sorted(g for g in index if os.path.isfile(template_path(g, ap_dir)))
    return sorted(f[: -len(".yaml")] for f in os.listdir(folder)
                  if f.lower().endswith(".yaml"))


def _coerce(text: str):
    """'true'/'false'/digits to their Python values, anything else stays text."""
    low = text.strip().strip("'\"")
    if low in ("true", "false"):
        return low == "true"
    if re.fullmatch(r"-?\d+", low):
        return int(low)
    return low


def parse(game: str, ap_dir: str) -> list[dict]:
    """[{key, doc, kind, values, default, min, max, section}] for one game.

    The template is parsed rather than YAML-loaded because the documentation
    lives in comments, and the comments are most of the value here: they are
    what the world author wrote to explain the option.
    """
    path = template_path(game, ap_dir)
    try:
        text = open(path, encoding="utf-8-sig", errors="replace").read()
    except OSError as exc:
        raise FileNotFoundError(f"no template for {game!r} ({exc}). Run "
                                "Archipelago's 'Generate Template Options'.") from exc

    # Everything after the "<Game>:" line is that game's own options. The key is
    # quoted when the name contains a colon - 'Jak and Daxter: The Precursor
    # Legacy': - because unquoted it would not be valid YAML. Missing that meant
    # reading the whole file as the body and parsing the shared preamble as
    # options, so the form offered one nonsense setting instead of the game's.
    head = re.search(rf"^['\"]?{re.escape(game)}['\"]?:\s*$", text, re.M)
    body = text[head.end():] if head else text

    out, section, doc, key, values = [], None, [], None, {}
    collection = False
    aliases: dict = {}       # "normal" -> 50, from "# equivalent to 50"
    key_section = None       # the heading in force when THIS option started

    def flush():
        if key is None:
            return
        raw_keys = set(values)
        # [] or {} in the template: a list of items or a mapping, not a setting
        # with values to pick from. Plando, local_items, start_inventory. These
        # are NOT editable here, and emitting a scalar for one produced
        # "local_items: None", which fails generation outright.
        # A generated template gives a Choice exactly ONE non-zero weight: its
        # default. More than one means this is a weight TABLE whose keys are all
        # used together - trap_weights, filler_item_weights, Factorio's world_gen,
        # KH2's CustomItemPoolQuantity. Collapsing one of those to its highest
        # entry would quietly rewrite the item pool, so they are left alone too.
        weight_table = len([v for k, v in values.items()
                            if v and not k.startswith("#")]) > 1
        if collection or weight_table or not values:
            out.append({"key": key, "section": key_section,
                        "doc": "\n".join(doc).strip(),
                        "kind": COLLECTION, "editable": False, "values": [], "rolls": [],
                        "default": None, "min": None, "max": None, "aliases": {}})
            return
        named = {k: v for k, v in values.items() if not RANDOM_KEYS.match(k)}
        blob = "\n".join(doc)
        lo = re.search(r"Minimum value is (-?\d+)", blob)
        hi = re.search(r"Maximum value is (-?\d+)", blob)
        keys = set(named)
        if keys and keys <= {"true", "false"}:
            kind = BOOL
        elif lo and hi:
            kind = RANGE
        else:
            kind = CHOICE
        best = max(named.items(), key=lambda kv: kv[1], default=(None, 0))[0]
        # "random" is accepted for EVERY option - Archipelago's Toggle.from_text
        # and Choice.from_text both special-case it - but the template only
        # enumerates it for ranges. Filtering it out hid a real setting: there
        # was no way to ask for a random starting character.
        value_list = [_coerce(k) for k in named]
        rolls = [k for k in sorted(raw_keys) if RANDOM_KEYS.match(k)]
        if kind in (BOOL, CHOICE) and "random" not in rolls:
            rolls = ["random"] + rolls
        default = _coerce(best) if best is not None else None
        # A range option may default to a named alias ("normal"); a numeric
        # editor needs the number the name stands for.
        if kind == RANGE and isinstance(default, str) and default in aliases:
            default = aliases[default]
        out.append({
            "key": key, "section": key_section, "doc": blob.strip(),
            "editable": True,
            "kind": kind,
            "values": value_list,
            "rolls": rolls,
            "default": default,
            "aliases": dict(aliases),
            "min": int(lo.group(1)) if lo else None,
            "max": int(hi.group(1)) if hi else None,
        })

    for raw in body.splitlines():
        if not raw.strip():
            continue
        if re.match(r"^\s*#+\s*$", raw):            # ruler lines around a heading
            continue
        m = re.match(r"^  # (.+?) #\s*$", raw)      # "  # Goal Settings #"
        if m:
            section = m.group(1).strip()
            continue
        m = re.match(r"^  (\w[\w\d_]*):\s*$", raw)  # a new option
        if m:
            flush()
            key, doc, values, aliases = m.group(1), [], {}, {}
            collection = False
            key_section = section
            continue
        if key is None:
            continue
        m = re.match(r"^\s+#\s?(.*)$", raw)         # that option's documentation
        if m:
            doc.append(m.group(1).rstrip())
            continue
        if raw.strip() in ("[]", "{}"):
            collection = True
            continue
        m = re.match(r"^\s+(.+?):\s*(\d+)(.*)$", raw)   # "value: weight  # note"
        if m:
            name = m.group(1).strip().strip("'\"")
            if name.startswith("#"):          # a commented-out hint, not a value
                continue
            values[name] = int(m.group(2))
            eq = re.search(r"equivalent to (-?\d+)", m.group(3) or "")
            if eq:
                aliases[name] = int(eq.group(1))
            continue
        if re.match(r"^\S", raw):                   # left the game's block
            break
    flush()
    return out


def read_config(data: bytes, game: str) -> dict:
    """The option values a player's own YAML sets, as plain scalars.

    Only the simple `key: value` form is read. A weighted option (several
    values with odds) is deliberately left alone: collapsing someone's weights
    into one number would silently throw away what they asked for.
    """
    text = data.decode("utf-8-sig", "replace")
    head = re.search(rf"^{re.escape(game)}:\s*$", text, re.M)
    if not head:
        return {}
    out, weighted = {}, None
    for raw in text[head.end():].splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if re.match(r"^\S", raw):
            break                                    # back to top level
        m = re.match(r"^  (\w[\w\d_]*):\s*(.*)$", raw)
        if m:
            key, value = m.group(1), m.group(2).split("#")[0].strip()
            weighted = key if value == "" else None  # a block follows: weights
            if value:
                out[key] = _coerce(value)
            continue
    return out


def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def write_config(data: bytes, game: str, values: dict) -> bytes:
    """Return the config with ONLY these options changed.

    A merge, not a rewrite. Every line this function does not recognise is
    copied through: another game's block, comments, and - the reason this
    matters - options whose value is a list or a mapping, like plando_items,
    start_inventory or exclude_locations. Replacing the whole block would
    silently delete all of those the first time someone saved a toggle.

    An option named in `values` loses whatever it had, including a weighted
    block, because that is what editing it means.
    """
    text = data.decode("utf-8-sig", "replace")
    had_bom = data.startswith(BOM)
    lines = text.splitlines()
    head = re.search(rf"^{re.escape(game)}:\s*$", text, re.M)
    pending = dict(values)

    if not head:
        out = lines + [f"{game}:"] + [f"  {k}: {_scalar(v)}" for k, v in pending.items()]
        blob = ("\n".join(out).rstrip("\n") + "\n").encode("utf-8")
        return (BOM + blob) if had_bom else blob

    start = text[: head.start()].count("\n")
    end = start + 1
    while end < len(lines) and (not lines[end].strip()
                                or lines[end].startswith((" ", "\t"))):
        end += 1

    body, i = [], start + 1
    while i < end:
        line = lines[i]
        m = re.match(r"^  (\w[\w\d_]*):", line)
        if m and m.group(1) in pending:
            key = m.group(1)
            body.append(f"  {key}: {_scalar(pending.pop(key))}")
            i += 1
            # Drop whatever belonged to that key: an inline value is one line,
            # a weighted block is the indented lines that follow it.
            while i < end and (lines[i].startswith(("    ", "\t"))
                               or not lines[i].strip()):
                if lines[i].strip() and not lines[i].startswith(("    ", "\t")):
                    break
                if not lines[i].strip() and i + 1 < end and re.match(r"^  \w", lines[i + 1]):
                    break
                i += 1
            continue
        body.append(line)
        i += 1

    for key, value in pending.items():          # options the file did not have
        body.append(f"  {key}: {_scalar(value)}")

    out = lines[:start] + [lines[start]] + body + lines[end:]
    blob = ("\n".join(out).rstrip("\n") + "\n").encode("utf-8")
    return (BOM + blob) if had_bom else blob


def defaults(game: str, ap_dir: str) -> dict:
    return {o["key"]: o["default"] for o in parse(game, ap_dir)}


def main(argv=None) -> int:
    import aplobby as core

    ap = argparse.ArgumentParser(description="Read a game's options from its template.")
    ap.add_argument("game", nargs="?", help="game name, e.g. EarthBound")
    ap.add_argument("--ap", default=core.AP_DEFAULT)
    ap.add_argument("--values", action="store_true", help="just key: default")
    args = ap.parse_args(argv)

    if not args.game:
        names = available(args.ap)
        print(f"{len(names)} game(s) have a template:\n")
        for n in names:
            print(" ", n)
        return 0

    try:
        opts = parse(args.game, args.ap)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.values:
        for o in opts:
            print(f"{o['key']}: {o['default']}")
        return 0

    section = None
    for o in opts:
        if o["section"] != section:
            section = o["section"]
            print(f"\n== {section or 'Options'} ==")
        bits = o["kind"]
        if o["kind"] == RANGE:
            bits += f" {o['min']}..{o['max']}"
        elif o["kind"] == CHOICE:
            bits += f" {o['values']}"
        print(f"  {o['key']:34} {bits:38} default={o['default']!r}")
        first = (o["doc"].splitlines() or [""])[0]
        if first:
            print(f"      {first[:92]}")
    print(f"\n{len(opts)} options")
    return 0


if __name__ == "__main__":
    sys.exit(main())
