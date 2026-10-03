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

BOOL, RANGE, CHOICE = "bool", "range", "choice"
BOM = b"\xef\xbb\xbf"          # Windows editors add it; round-trip it faithfully

# Weighted keys the template adds to every option; they are rolls, not values.
RANDOM_KEYS = re.compile(r"^(random|random-low|random-high|random-range-[\d-]+)$")


def template_path(game: str, ap_dir: str) -> str:
    return os.path.join(ap_dir, "Players", "Templates", f"{game}.yaml")


def available(ap_dir: str) -> list[str]:
    """Games with a generated template, newest install state wins."""
    folder = os.path.join(ap_dir, "Players", "Templates")
    if not os.path.isdir(folder):
        return []
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

    # Everything after the "<Game>:" line is that game's own options.
    head = re.search(rf"^{re.escape(game)}:\s*$", text, re.M)
    body = text[head.end():] if head else text

    out, section, doc, key, values = [], None, [], None, {}
    aliases: dict = {}       # "normal" -> 50, from "# equivalent to 50"
    key_section = None       # the heading in force when THIS option started

    def flush():
        if key is None:
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
        default = _coerce(best) if best is not None else None
        # A range option may default to a named alias ("normal"); a numeric
        # editor needs the number the name stands for.
        if kind == RANGE and isinstance(default, str) and default in aliases:
            default = aliases[default]
        out.append({
            "key": key, "section": key_section, "doc": blob.strip(),
            "kind": kind,
            "values": [_coerce(k) for k in named],
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
            key_section = section
            continue
        if key is None:
            continue
        m = re.match(r"^\s+#\s?(.*)$", raw)         # that option's documentation
        if m:
            doc.append(m.group(1).rstrip())
            continue
        m = re.match(r"^\s+(.+?):\s*(\d+)(.*)$", raw)   # "value: weight  # note"
        if m:
            name = m.group(1).strip().strip("'\"")
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


def write_config(data: bytes, game: str, values: dict) -> bytes:
    """Return the config with the game block replaced by these values.

    Everything outside the block - name, description, requires, other games -
    is passed through untouched, because this edits one game's settings and
    should not rewrite a file it does not fully understand.
    """
    text = data.decode("utf-8-sig", "replace")
    had_bom = data.startswith(BOM)
    head = re.search(rf"^{re.escape(game)}:\s*$", text, re.M)
    lines = text.splitlines()
    block = [f"{game}:"]
    for key, value in values.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        block.append(f"  {key}: {value}")

    if not head:
        out = lines + [""] + block
    else:
        start = text[: head.start()].count("\n")
        end = start + 1
        while end < len(lines) and (not lines[end].strip()
                                    or lines[end].startswith((" ", "\t"))):
            end += 1
        out = lines[:start] + block + lines[end:]
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
