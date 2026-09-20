"""The local lobby: a durable roster of player configs this machine owns.

This is the foundation the rest of the app sits on. A remote room is one way to
put configs *into* it, not the thing that defines it - so generation works with
no network at all.

Layout, all under ./lobby:

    lobby.json        the manifest; the single commit point
    lobby.json.bak    the previous manifest, for torn-write recovery
    players/p001.yaml the live config for each slot
    history/<sha>.yaml  superseded configs, content-addressed
    .tmp/             scratch for atomic writes
    .lock             held while a process is mutating the lobby

Three rules make the store safe to reason about:

1. **Slots are opaque.** A slot is p001, p002 ... and never changes. Identity is
   never derived from a player's name: safe_name() strips every non-alphanumeric
   character, so "Sea Star" and "SeaStar" collide, and a name with no ASCII at
   all reduces to "" and collides with every other such player. Human-readable
   filenames are generated only at stage() time, into a throwaway run directory
   where a collision suffix costs nothing.

2. **Blobs first, manifest last.** Every write is tmp -> fsync -> os.replace, and
   lobby.json is always written last. A crash can therefore leave an orphan file
   that nothing references, which load() adopts; it cannot leave the manifest
   pointing at a file that does not exist.

3. **Every mutation persists immediately.** There is no public save() to forget
   to call and no in-memory batch to lose. The manifest is a few KB.

Standard library only.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import tempfile
import time
import unicodedata

import aplobby as core  # core.* is only ever touched inside a function body,
                        # so the two modules may import each other freely.

SCHEMA = "aplobby-lobby/1"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.join(HERE, "lobby")

STALE_TMP_SECONDS = 3600


class LobbyError(Exception):
    """Anything that should stop a lobby operation with a readable message."""


class LobbyLocked(LobbyError):
    """Another process holds the lobby lock."""


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _norm(text) -> str:
    """Fold a name or game for fallback matching only - never for identity.

    NFC so composed and decomposed accents compare equal, casefold for case,
    and collapsed whitespace so "Fee  Bear" and "Sea Star" are one player.
    """
    if not text:
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(text)).casefold()).strip()


# ------------------------------------------------------------------ atomicity

def _replace_with_retry(src: str, dest: str, attempts: int = 5):
    """os.replace, retried - on Windows the destination may be briefly held.

    Antivirus mid-scan, Explorer's preview pane, or an editor with lobby.json
    open all raise PermissionError here. That is common enough that failing on
    the first attempt would make the app feel broken.
    """
    delay = 0.05
    for attempt in range(attempts):
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise LobbyError(
                    f"could not replace {dest} - it is open in another program. "
                    "Close anything viewing the lobby folder and try again.")
            time.sleep(delay)
            delay *= 2


def _atomic_write(path: str, data: bytes, tmp_dir: str):
    """Write bytes so the destination is either the old file or the new one."""
    os.makedirs(tmp_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=tmp_dir)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp, path)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


class _Lock:
    """Cross-process guard. The GUI and the CLI can both reach the lobby."""

    def __init__(self, path: str):
        self.path = path
        self.fd = None

    def __enter__(self):
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = ""
            try:
                holder = open(self.path, encoding="utf-8").read().strip()
            except OSError:
                pass
            # Deliberately no liveness probe: guessing wrong here loses data,
            # and a stale lock is cheap for a person to clear.
            raise LobbyLocked(
                f"the lobby is locked by {holder or 'another process'}. "
                f"If that process is gone, delete {self.path}")
        os.write(self.fd, f"pid {os.getpid()} since {_now()}".encode())
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            os.unlink(self.path)
        except OSError:
            pass
        return False


# ------------------------------------------------------------------ the store

class Lobby:
    """The roster. Open it with open_lobby() so the lock is held correctly."""

    def __init__(self, root: str):
        self.root = root
        self.players_dir = os.path.join(root, "players")
        self.history_dir = os.path.join(root, "history")
        self.tmp_dir = os.path.join(root, ".tmp")
        self.manifest = os.path.join(root, "lobby.json")
        self.backup = self.manifest + ".bak"
        self.entries: list[dict] = []
        self.next_slot = 1
        self.migrations: list[str] = []
        self.adopted: list[str] = []   # orphans taken in by the last load()
        self.created = _now()

    # -- persistence ------------------------------------------------------

    def _blob(self, slot: str) -> str:
        return os.path.join(self.players_dir, f"{slot}.yaml")

    def _doc(self) -> dict:
        return {"schema": SCHEMA, "created": self.created, "updated": _now(),
                "next_slot": self.next_slot, "migrations": self.migrations,
                "players": self.entries}

    def _ensure_dirs(self):
        for d in (self.players_dir, self.history_dir, self.tmp_dir):
            os.makedirs(d, exist_ok=True)

    def _persist(self):
        """Write the manifest. Always the last step of any mutation."""
        self._ensure_dirs()
        if os.path.isfile(self.manifest):
            try:
                # Only keep a backup of a manifest that actually parses, or
                # recovering from a torn write would overwrite the good copy
                # with the bad one.
                json.load(open(self.manifest, encoding="utf-8"))
                shutil.copyfile(self.manifest, self.backup)
            except (OSError, ValueError):
                pass  # a missing backup is worse news later, not now
        blob = json.dumps(self._doc(), indent=2, ensure_ascii=False).encode("utf-8")
        _atomic_write(self.manifest, blob, self.tmp_dir)

    def _read_manifest(self):
        """Load the manifest, falling back to the backup rather than to empty.

        An empty lobby that *looks* fine is the worst failure here - it would
        silently generate a seed with nobody in it - so a torn manifest is loud.
        """
        for path, note in ((self.manifest, None), (self.backup, "backup")):
            if not os.path.isfile(path):
                continue
            try:
                doc = json.load(open(path, encoding="utf-8"))
            except (ValueError, OSError) as exc:
                if path == self.manifest:
                    self.problems_at_load = f"{self.manifest} is unreadable ({exc})"
                continue
            self.entries = doc.get("players", [])
            self.next_slot = doc.get("next_slot", len(self.entries) + 1)
            self.migrations = doc.get("migrations", [])
            self.created = doc.get("created", _now())
            if note:
                self.problems_at_load = (
                    f"{self.manifest} was unreadable; recovered from {note}. "
                    "Check the roster before generating.")
            return
        # No manifest at all is a legitimate first run, not a failure.

    def _reconcile(self):
        """Make the manifest and the files on disk agree.

        Both directions are possible after a crash, and they are not equally
        bad: an orphan blob is complete (os.replace is atomic) so it can be
        adopted, while a manifest entry whose blob is gone must be flagged.
        """
        known = {e["slot"] for e in self.entries}
        for e in self.entries:
            e["missing"] = not os.path.isfile(self._blob(e["slot"]))

        if os.path.isdir(self.players_dir):
            for fn in sorted(os.listdir(self.players_dir)):
                stem, ext = os.path.splitext(fn)
                if ext.lower() not in (".yaml", ".yml") or stem in known:
                    continue
                data = open(os.path.join(self.players_dir, fn), "rb").read()
                name, game = core.yaml_fields(data)
                slot = stem if re.fullmatch(r"p\d{3,}", stem) else self._new_slot()
                if slot != stem:
                    _atomic_write(self._blob(slot), data, self.tmp_dir)
                    os.unlink(os.path.join(self.players_dir, fn))
                self.entries.append(self._entry(slot, name, game, data,
                                                {"kind": "adopted", "id": fn}))
                self.adopted.append(slot)
                self._bump_slot(slot)

        # Clear scratch a previous crash left behind.
        if os.path.isdir(self.tmp_dir):
            cutoff = time.time() - STALE_TMP_SECONDS
            for fn in os.listdir(self.tmp_dir):
                p = os.path.join(self.tmp_dir, fn)
                try:
                    if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                        os.unlink(p)
                except OSError:
                    pass

    # -- slots ------------------------------------------------------------

    def _new_slot(self) -> str:
        return f"p{self.next_slot:03d}"

    def _bump_slot(self, slot: str):
        m = re.fullmatch(r"p(\d+)", slot)
        if m:
            self.next_slot = max(self.next_slot, int(m.group(1)) + 1)

    @staticmethod
    def _entry(slot, name, game, data, source) -> dict:
        return {"slot": slot, "name": name, "game": game,
                "sha256": core.sha256(data), "bytes": len(data),
                "enabled": True, "added": _now(), "updated": _now(),
                "names_seen": [name] if name else [],
                "sources": [source], "history": []}

    def find(self, slot: str):
        return next((e for e in self.entries if e["slot"] == slot), None)

    def _match(self, data: bytes, name, game, source):
        """Which existing entry does this config belong to? First rule wins.

        1. The source's own stable id. For a room that is the yaml-id already
           on each row, which survives a player renaming themselves - the case
           every other key gets wrong.
        2. Content hash: identical bytes are the same config whatever it says.
        3. Normalised name+game, for sources that carry no id of their own.
        """
        sid = (source.get("kind"), source.get("id"))
        if sid[1]:
            for e in self.entries:
                if any((s.get("kind"), s.get("id")) == sid for s in e.get("sources", [])):
                    return e
        digest = core.sha256(data)
        for e in self.entries:
            if e.get("sha256") == digest:
                return e
        key = (_norm(name), _norm(game))
        if key != ("", ""):
            for e in self.entries:
                if (_norm(e.get("name")), _norm(e.get("game"))) != key:
                    continue
                # Only a fallback: if this entry is already pinned to a
                # different stable id, these are two different people who
                # happen to share a name, not one person re-importing.
                ids = {(s.get("kind"), s.get("id")) for s in e.get("sources", [])
                       if s.get("id")}
                if sid[1] and ids and sid not in ids:
                    continue
                return e
        return None

    # -- mutations --------------------------------------------------------

    def upsert(self, data: bytes, source: dict):
        """Add or update one config. Returns (action, entry).

        action is "added", "updated" or "unchanged". Unchanged bytes never
        touch the timestamps or write history - re-importing a room that has
        not moved is a no-op you can run as often as you like.
        """
        name, game = core.yaml_fields(data)
        digest = core.sha256(data)
        entry = self._match(data, name, game, source)

        if entry is None:
            slot = self._new_slot()
            entry = self._entry(slot, name, game, data, source)
            self.entries.append(entry)
            self._bump_slot(slot)
            _atomic_write(self._blob(slot), data, self.tmp_dir)
            self._persist()
            return "added", entry

        if entry.get("sha256") == digest and not entry.get("missing"):
            self._remember_source(entry, source)
            self._persist()
            return "unchanged", entry

        # Retire the previous bytes before the new ones land on them. Content
        # addressed, so a config that flip-flops back does not duplicate.
        old = self._blob(entry["slot"])
        if entry.get("sha256") and os.path.isfile(old):
            keep = os.path.join(self.history_dir, f"{entry['sha256']}.yaml")
            if not os.path.isfile(keep):
                _atomic_write(keep, open(old, "rb").read(), self.tmp_dir)
            entry.setdefault("history", []).append(
                {"sha256": entry["sha256"], "bytes": entry.get("bytes"),
                 "replaced": _now(), "source": entry.get("sources", [])[-1:]})

        _atomic_write(self._blob(entry["slot"]), data, self.tmp_dir)
        if name and name not in entry.get("names_seen", []):
            entry.setdefault("names_seen", []).append(name)
        entry.update(name=name, game=game, sha256=digest, bytes=len(data),
                     updated=_now(), missing=False)
        self._remember_source(entry, source)
        self._persist()
        return "updated", entry

    @staticmethod
    def _remember_source(entry: dict, source: dict):
        sid = (source.get("kind"), source.get("id"))
        known = {(s.get("kind"), s.get("id")) for s in entry.setdefault("sources", [])}
        if sid not in known:
            entry["sources"].append(source)

    def remove(self, slot: str):
        """Drop a slot. Its history blobs stay - old runs still resolve."""
        entry = self.find(slot)
        if entry is None:
            raise LobbyError(f"no such slot: {slot}")
        blob = self._blob(slot)
        if os.path.isfile(blob):
            keep = os.path.join(self.history_dir, f"{entry['sha256']}.yaml")
            if not os.path.isfile(keep):
                _atomic_write(keep, open(blob, "rb").read(), self.tmp_dir)
            os.unlink(blob)
        self.entries = [e for e in self.entries if e["slot"] != slot]
        self._persist()
        return entry

    def set_enabled(self, slot: str, on: bool):
        """Include or exclude a slot from generation without losing them.

        Removing someone would destroy the history that makes older runs
        reproducible, so leaving the group is a toggle, not a deletion.
        """
        entry = self.find(slot)
        if entry is None:
            raise LobbyError(f"no such slot: {slot}")
        entry["enabled"] = bool(on)
        self._persist()
        return entry

    # -- reading ----------------------------------------------------------

    def enabled(self) -> list[dict]:
        return [e for e in self.entries if e.get("enabled", True)]

    @staticmethod
    def _record(e: dict) -> dict:
        """One player as the run lock wants them, independent of staging."""
        return {"slot": e["slot"], "player": e.get("name"), "game": e.get("game"),
                "yaml_bytes": e.get("bytes"), "yaml_sha256": e.get("sha256"),
                "source": (e.get("sources") or [{}])[-1]}

    def records(self) -> list[dict]:
        """The enabled roster, without writing anything.

        Preflight needs only the games, so it can run - and a dry run can
        finish - without creating a directory or copying a file.
        """
        return [self._record(e) for e in self.enabled()]

    def problems(self) -> list[str]:
        """Everything that would make a generation wrong, in plain words."""
        out = []
        if getattr(self, "problems_at_load", None):
            out.append(self.problems_at_load)
        for e in self.entries:
            if e.get("missing"):
                out.append(f"{e['slot']} ({e.get('name') or '?'}): its config file "
                           "is missing from the lobby")
        seen = {}
        for e in self.enabled():
            key = _norm(e.get("name"))
            if not key:
                out.append(f"{e['slot']}: the config has no 'name:' line")
                continue
            if key in seen:
                out.append(f"{e['slot']} and {seen[key]} are both named "
                           f"{e.get('name')!r} - Archipelago needs distinct names")
            seen[key] = e["slot"]
        return out

    def stage(self, players_dir: str) -> list[str]:
        """Copy the enabled configs into a run's Players directory.

        Returns one record per staged player - slot, readable filename, and the
        bytes' identity - which is exactly what the run lock needs, so nothing
        downstream has to re-derive the mapping and get it subtly wrong.

        Readable names are minted here and nowhere else, because this directory
        is disposable - a collision suffix costs nothing and never becomes an
        identity. Asserts the count, since a run that quietly generates with
        fewer players than the table showed is worse than one that refuses.
        """
        os.makedirs(players_dir, exist_ok=True)
        want = self.enabled()
        written = []
        for e in want:
            src = self._blob(e["slot"])
            if not os.path.isfile(src):
                raise LobbyError(f"{e['slot']} ({e.get('name') or '?'}) has no config "
                                 "file in the lobby; staging would drop a player")
            stem = os.path.splitext(core.safe_name(e.get("name") or e["slot"],
                                                   e.get("game") or ""))[0]
            fn, n = f"{stem}.yaml", 2
            while os.path.exists(os.path.join(players_dir, fn)):
                fn = f"{stem}-{n}.yaml"
                n += 1
            _atomic_write(os.path.join(players_dir, fn),
                          open(src, "rb").read(), self.tmp_dir)
            written.append({**self._record(e), "yaml": fn})
        if len(written) != len(want):
            raise LobbyError(f"staged {len(written)} configs for {len(want)} enabled "
                             "players; refusing to generate a short roster")
        return written


def open_lobby(root: str | None = None) -> tuple[Lobby, _Lock]:
    """Open the lobby and take its lock. Use as a context manager pair.

        with lobby.locked() as lb:
            lb.upsert(...)
    """
    lb = Lobby(root or DEFAULT_ROOT)
    lock = _Lock(os.path.join(lb.root, ".lock"))
    return lb, lock


class locked:
    """`with lobby.locked() as lb:` - acquires, loads, reconciles, releases."""

    def __init__(self, root: str | None = None):
        self.root = root or DEFAULT_ROOT

    def __enter__(self) -> Lobby:
        os.makedirs(self.root, exist_ok=True)
        self.lb = Lobby(self.root)
        self.lb._ensure_dirs()
        self.lock = _Lock(os.path.join(self.root, ".lock"))
        self.lock.__enter__()
        try:
            self.lb.problems_at_load = None
            self.lb._read_manifest()
            self.lb._reconcile()
            # Repair on the way in. Adopting orphans or recovering from the
            # backup both leave the manifest on disk disagreeing with what we
            # just loaded; writing it back means the next reader - including a
            # person opening lobby.json - sees the recovered state, not the
            # damage.
            if self.lb.adopted or self.lb.problems_at_load:
                self.lb._persist()
        except Exception:
            self.lock.__exit__(None, None, None)
            raise
        return self.lb

    def __exit__(self, *exc):
        self.lock.__exit__(*exc)
        return False
