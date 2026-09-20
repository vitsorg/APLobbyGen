"""Desktop front end for the local lobby.

The lobby on this machine owns the roster. Import configs into it from a lobby
room, a folder, a zip or individual files, see at a glance which games this
install can actually handle, then generate - with no network involved.

Standard library only (tkinter).

    python aplobby_gui.py
"""
from __future__ import annotations

import json
import os
import queue
import threading
import zipfile
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

import aplobby as core
import lobby
import sources
import theme

HERE = os.path.dirname(os.path.abspath(__file__))


class App(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=10)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=3)
        self.rowconfigure(5, weight=2)

        self.msgs: queue.Queue = queue.Queue()
        self.rows: list = []
        # None means "not scanned yet", {} means "scanned and nothing installed".
        # Collapsing those two is why Generate used to light up too early.
        self.index: dict | None = None
        self.run_dir: str | None = None
        self.seed_zip: str | None = None
        self.published: dict | None = None
        self.spoiler_path: str | None = None
        self.busy = False
        self.lobby_root = os.path.join(HERE, "lobby")

        # Paint before building: ttk styles are global, so widgets created
        # afterwards are born with the right colours and never flash white.
        self.theme_mode = theme.load_pref()
        self.palette = theme.apply(master.winfo_toplevel(), self.theme_mode)

        self._build_lobby_bar()
        self._build_actions()
        self._build_table()
        self._build_log()
        self._build_status()

        self.after(100, self._drain)
        # Load and preflight on the worker thread: index_worlds() hashes every
        # installed apworld, which is hundreds of megabytes and would freeze
        # the window if it ran here.
        self.after(150, self.reload)

    # ---------------------------------------------------------- layout

    def _build_lobby_bar(self):
        box = ttk.LabelFrame(self, text="Lobby", padding=8)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)

        self.lobby_summary = tk.StringVar(value="opening the lobby...")
        ttk.Label(box, textvariable=self.lobby_summary).grid(
            row=0, column=0, columnspan=2, sticky="w")

        btns = ttk.Frame(box)
        btns.grid(row=0, column=2, sticky="e")

        self.import_btn = ttk.Menubutton(btns, text="Import...")
        menu = tk.Menu(self.import_btn, tearoff=False)
        menu.add_command(label="From a lobby room...", command=self.import_room)
        menu.add_command(label="From a folder...", command=self.import_folder)
        menu.add_command(label="From a zip...", command=self.import_zip)
        menu.add_separator()
        menu.add_command(label="Add config files...", command=self.import_files)
        self.import_btn["menu"] = menu
        self.import_btn.pack(side="left")

        self.reload_btn = ttk.Button(btns, text="Reload", command=self.reload)
        self.reload_btn.pack(side="left", padx=6)
        ttk.Button(btns, text="Open folder", command=self.open_lobby).pack(side="left")

        ttk.Label(box, text="Archipelago").grid(row=1, column=0, pady=(8, 0), padx=(0, 8))
        self.ap_var = tk.StringVar(value=core.AP_DEFAULT)
        ttk.Entry(box, textvariable=self.ap_var).grid(row=1, column=1, sticky="ew", pady=(8, 0))
        ttk.Button(box, text="Browse", command=self._pick_ap).grid(
            row=1, column=2, padx=(8, 0), pady=(8, 0), sticky="e")

    def _build_actions(self):
        bar = ttk.Frame(self)
        bar.grid(row=1, column=0, sticky="ew", pady=(10, 6))

        self.gen_btn = ttk.Button(bar, text="Generate seed", command=self.generate,
                                  state="disabled")
        self.gen_btn.pack(side="left")

        self.gen_reason = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.gen_reason, style="Muted.TLabel").pack(
            side="left", padx=8)

        self.upstream_btn = ttk.Button(bar, text="Check upstream",
                                       command=self.check_upstream, state="disabled")
        self.upstream_btn.pack(side="left", padx=6)

        self.allow_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Generate even with missing worlds",
                        variable=self.allow_var,
                        command=self._refresh_gen_state).pack(side="left", padx=10)

        self.publish_btn = ttk.Button(bar, text="Publish to archipelago.gg",
                                      command=self.publish, state="disabled")
        self.publish_btn.pack(side="left", padx=6)

        self.room_btn = ttk.Button(bar, text="Create room...", command=self.open_room_link,
                                   state="disabled")
        self.room_btn.pack(side="left")

        self.spoiler_btn = ttk.Button(bar, text="Open spoiler", command=self.open_spoiler,
                                      state="disabled")
        self.spoiler_btn.pack(side="right", padx=6)
        self.open_btn = ttk.Button(bar, text="Open run folder", command=self.open_run,
                                   state="disabled")
        self.open_btn.pack(side="right")
        self.copy_btn = ttk.Button(bar, text="Copy seed path", command=self.copy_seed,
                                   state="disabled")
        self.copy_btn.pack(side="right", padx=6)

    def _build_table(self):
        box = ttk.LabelFrame(self, text="Players", padding=6)
        box.grid(row=3, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)

        cols = ("in", "slot", "player", "game", "world", "version", "state")
        self.tree = ttk.Treeview(box, columns=cols, show="headings", height=12,
                                 selectmode="extended")
        for col, label, width, anchor in (
                ("in", "In", 36, "center"), ("slot", "Slot", 54, "w"),
                ("player", "Player", 130, "w"), ("game", "Game", 214, "w"),
                ("world", "World file", 170, "w"), ("version", "Version", 108, "w"),
                ("state", "Status", 128, "w")):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor=anchor)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", lambda _e: self.toggle_selected())

        sb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)

        self._paint_tags()

        row = ttk.Frame(box)
        row.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(row, text="Include", command=lambda: self.set_selected(True)).pack(side="left")
        ttk.Button(row, text="Sit out", command=lambda: self.set_selected(False)).pack(
            side="left", padx=6)
        ttk.Button(row, text="Remove from lobby", command=self.remove_selected).pack(side="left")
        ttk.Label(row, text="   double-click a row to include or exclude it",
                  style="Muted.TLabel").pack(side="left")

    def _build_log(self):
        box = ttk.LabelFrame(self, text="Log", padding=6)
        box.grid(row=5, column=0, sticky="nsew", pady=(10, 0))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)

        self.log = tk.Text(box, height=9, wrap="none", font=("Consolas", 9),
                           relief="flat", borderwidth=0, highlightthickness=0)
        self.log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=sb.set, state="disabled")
        self._paint_log()

    def _build_status(self):
        row = ttk.Frame(self)
        row.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        row.columnconfigure(0, weight=1)

        self.status = tk.StringVar(value="Opening the lobby...")
        ttk.Label(row, textvariable=self.status, anchor="w").grid(
            row=0, column=0, sticky="ew")

        self.theme_label = tk.StringVar(value=self._theme_label())
        btn = ttk.Menubutton(row, textvariable=self.theme_label, width=14)
        menu = tk.Menu(btn, tearoff=0)
        self.theme_var = tk.StringVar(value=self.theme_mode)
        for mode, label in (("auto", "Auto (follow Windows)"),
                            ("light", "Light"), ("dark", "Dark")):
            menu.add_radiobutton(label=label, value=mode, variable=self.theme_var,
                                 command=lambda m=mode: self.set_theme(m))
        btn["menu"] = menu
        btn.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.theme_menu = menu

        self.bar = ttk.Progressbar(self, mode="determinate")
        self.bar.grid(row=7, column=0, sticky="ew", pady=(4, 0))

    # ---------------------------------------------------------- appearance

    def _theme_label(self) -> str:
        if self.theme_mode == "auto":
            return f"Theme: auto ({theme.resolve('auto')})"
        return f"Theme: {self.theme_mode}"

    def _paint_tags(self):
        """Row colours live in the palette: the same red goes muddy on dark."""
        for tag in ("missing", "match", "out"):
            self.tree.tag_configure(tag, foreground=self.palette[tag])

    def _paint_log(self):
        self.log.configure(background=self.palette["field"],
                           foreground=self.palette["fg"],
                           insertbackground=self.palette["fg"],
                           selectbackground=self.palette["sel_bg"],
                           selectforeground=self.palette["sel_fg"])

    def set_theme(self, mode: str):
        """Repaint live. ttk styles are global, so every widget follows along;
        the plain-tk ones - the log and the table tags - need repainting here."""
        self.theme_mode = mode
        self.palette = theme.apply(self.winfo_toplevel(), mode)
        self._paint_tags()
        self._paint_log()
        self.theme_var.set(mode)
        self.theme_label.set(self._theme_label())
        theme.save_pref(mode)

    # ---------------------------------------------------------- plumbing

    def _pick_ap(self):
        chosen = filedialog.askdirectory(initialdir=self.ap_var.get(),
                                         title="Archipelago install folder")
        if chosen:
            self.ap_var.set(os.path.normpath(chosen))
            self.reload()

    def _snapshot(self):
        """Tk variables read on the UI thread, for a worker to use safely.

        Reading a Tk variable from another thread reaches into Tcl from the
        wrong place; it usually appears to work and then fails as "main thread
        is not in main loop". Workers get plain values instead.
        """
        return {"ap": self.ap_var.get(), "allow": self.allow_var.get()}

    def say(self, line=""):
        self.msgs.put(("log", line))

    def _drain(self):
        try:
            while True:
                kind, payload = self.msgs.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", payload + "\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "status":
                    self.status.set(payload)
                elif kind == "progress":
                    self.bar["value"] = payload
                elif kind == "rows":
                    self._fill(payload)
                elif kind == "done":
                    payload()
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _work(self, fn):
        if self.busy:
            return
        self.busy = True
        for b in (self.gen_btn, self.upstream_btn, self.publish_btn,
                  self.reload_btn, self.import_btn):
            b.configure(state="disabled")

        def run():
            try:
                fn()
            except (lobby.LobbyError, sources.SourceError) as exc:
                self.say(f"  {exc}")
                self.msgs.put(("status", str(exc)))
            except Exception as exc:  # surfaced in the log, never a silent death
                self.say(f"ERROR  {exc}")
                self.msgs.put(("status", f"Failed: {exc}"))
            finally:
                self.busy = False
                self.msgs.put(("done", self._reenable))

        threading.Thread(target=run, daemon=True).start()

    def _reenable(self):
        for b in (self.reload_btn, self.import_btn):
            b.configure(state="normal")
        self.upstream_btn.configure(state="normal" if self.rows else "disabled")
        self.publish_btn.configure(state="normal" if self.seed_zip else "disabled")
        self._refresh_gen_state()

    def _refresh_gen_state(self):
        """Enable Generate, and when it is off say which of the reasons it is."""
        reason = ""
        if self.busy:
            reason = "working..."
        elif self.index is None:
            reason = "still reading installed worlds"
        elif not [r for r in self.rows if r.get("enabled", True)]:
            reason = "no players are in - import some configs"
        elif self._missing() and not self.allow_var.get():
            n = len(self._missing())
            reason = f"{n} game{'s' if n > 1 else ''} with no installed world"
        self.gen_reason.set(reason)
        self.gen_btn.configure(state="disabled" if reason else "normal")

    def _missing(self):
        return [r for r in self.rows
                if r.get("enabled", True) and not r.get("world")]

    @staticmethod
    def _anchors():
        """registry.json anchors: a version a world states about itself indirectly."""
        path = os.path.join(HERE, "registry.json")
        if not os.path.isfile(path):
            return {}
        try:
            return json.load(open(path, encoding="utf-8")).get("anchors", {})
        except Exception:
            return {}

    @staticmethod
    def _version_for(hit, anchors):
        """Prefer the world's own declared version; fall back to a registry anchor.

        An anchor is shown with a marker because it is not the world's own
        version - it is the game or engine it carries. Never blank when
        something is known.
        """
        slug = hit["file"][: -len(".apworld")]
        try:
            z = zipfile.ZipFile(hit["path"])
            name = next((n for n in z.namelist() if n.endswith("archipelago.json")), None)
            if name:
                declared = json.loads(z.read(name)).get("world_version")
                if declared:
                    return str(declared)
        except Exception:
            pass
        anchor = anchors.get(slug)
        if anchor:
            return f"~ {anchor['version']}"
        return "unversioned"

    def _fill(self, rows):
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            on = r.get("enabled", True)
            if r.get("missing"):
                state, tag = "config file gone", "missing"
            elif not r.get("world"):
                state, tag = "no world installed", "missing"
            elif r.get("must_match"):
                state, tag = "world must match", "match"
            else:
                state, tag = "ready", ""
            if not on:
                tag = "out"
                state = "sitting out"
            self.tree.insert("", "end", iid=r["slot"], tags=(tag,),
                             values=("Y" if on else "-", r["slot"],
                                     r.get("name") or "?", r.get("game") or "?",
                                     r.get("world") or "-", r.get("version") or "-",
                                     state))

    def _selected_slots(self):
        return list(self.tree.selection())

    # ---------------------------------------------------------- lobby

    def _load_rows(self, lb, ap):
        """Build display rows and preflight them, without touching the store.

        The rows are copies: preflight() writes 'world' and 'must_match' into
        whatever it is given, and those are run facts, not lobby facts - they
        must never end up persisted in lobby.json.
        """
        rows = [dict(e) for e in lb.entries]
        index, _used, _missing = core.preflight(rows, ap)
        anchors = self._anchors()
        for r in rows:
            hit = index.get(r.get("game"))
            r["version"] = self._version_for(hit, anchors) if hit else None
        self.index = index
        self.rows = rows
        self.msgs.put(("rows", rows))
        return rows

    def _summarise(self, lb, rows):
        on = len([r for r in rows if r.get("enabled", True)])
        out = len(rows) - on
        text = f"{len(rows)} player{'s' if len(rows) != 1 else ''} in the lobby"
        if out:
            text += f" - {on} in, {out} sitting out"
        self.msgs.put(("done", lambda: self.lobby_summary.set(text)))
        for p in lb.problems():
            self.say(f"  ! {p}")

    def reload(self):
        snap = self._snapshot()
        self._work(lambda: self._reload(snap))

    def _reload(self, snap):
        self.msgs.put(("status", "Reading the lobby and the installed worlds..."))
        with lobby.locked(self.lobby_root) as lb:
            rows = self._load_rows(lb, snap["ap"])
            self._summarise(lb, rows)
        missing = self._missing()
        self.msgs.put(("status",
                       f"{len(missing)} game(s) have no installed world."
                       if missing else
                       (f"{len(rows)} player(s) ready to generate." if rows else
                        "The lobby is empty - import a room, a folder or a zip.")))

    # ---------------------------------------------------------- importing

    def import_room(self):
        raw = simpledialog.askstring("Import from a lobby room",
                                     "Room URL or id:", parent=self)
        if raw and raw.strip():
            self._work(lambda: self._import("room", raw.strip(), self._snapshot()))

    def import_folder(self):
        d = filedialog.askdirectory(title="Folder of .yaml configs")
        if d:
            self._work(lambda: self._import("folder", d, self._snapshot()))

    def import_zip(self):
        f = filedialog.askopenfilename(title="Zip containing .yaml configs",
                                       filetypes=[("Zip archives", "*.zip"), ("All", "*.*")])
        if f:
            self._work(lambda: self._import("zip", f, self._snapshot()))

    def import_files(self):
        fs = filedialog.askopenfilenames(title="Config files",
                                         filetypes=[("YAML", "*.yaml *.yml"), ("All", "*.*")])
        if fs:
            self._work(lambda: self._import("files", list(fs), self._snapshot()))

    def _import(self, kind, target, snap):
        self.msgs.put(("status", "Reading the source..."))
        if kind == "room":
            self.say(f"\nimporting from lobby room {target}")
            got = sources.ionium_room(
                target, progress=lambda i, n, who: self.msgs.put(
                    ("progress", int(90 * i / n))))
        elif kind == "folder":
            self.say(f"\nimporting from {target}")
            got = sources.folder(target)
        elif kind == "zip":
            self.say(f"\nimporting from {target}")
            got = sources.archive(target)
        else:
            self.say(f"\nimporting {len(target)} file(s)")
            got = sources.files(target)

        tally = {"added": 0, "updated": 0, "unchanged": 0}
        with lobby.locked(self.lobby_root) as lb:
            for data, src in got:
                action, entry = lb.upsert(data, src)
                tally[action] += 1
                if action != "unchanged":
                    self.say(f"  {action:9} {entry['slot']}  {entry.get('name') or '?'}"
                             f"  ({entry.get('game') or 'no game: line'})")
            self.say(f"{tally['added']} added, {tally['updated']} updated, "
                     f"{tally['unchanged']} unchanged")
            rows = self._load_rows(lb, snap["ap"])
            self._summarise(lb, rows)
        self.msgs.put(("progress", 100))
        self.msgs.put(("status", f"{len(rows)} player(s) in the lobby."))

    def open_lobby(self):
        os.makedirs(self.lobby_root, exist_ok=True)
        os.startfile(self.lobby_root)  # noqa: S606 - Windows shell open
        self.msgs.put(("status", "Press Reload after changing files by hand."))

    # ---------------------------------------------------------- roster edits

    def set_selected(self, on: bool):
        slots = self._selected_slots()
        if not slots:
            return
        snap = self._snapshot()
        self._work(lambda: self._set_enabled(slots, on, snap))

    def _set_enabled(self, slots, on, snap):
        with lobby.locked(self.lobby_root) as lb:
            for s in slots:
                e = lb.set_enabled(s, on)
                self.say(f"  {e['slot']} {e.get('name')} is "
                         f"{'in' if on else 'sitting out'}")
            rows = self._load_rows(lb, snap["ap"])
            self._summarise(lb, rows)

    def remove_selected(self):
        slots = self._selected_slots()
        if not slots:
            return
        who = ", ".join(self.tree.set(s, "player") for s in slots)
        if not messagebox.askokcancel(
                "Remove from the lobby",
                f"Remove {who} from the lobby?\n\n"
                "Their earlier configs stay in the lobby's history, so previous "
                "runs remain reproducible. To leave someone out of just the next "
                "seed, use 'Sit out' instead."):
            return
        snap = self._snapshot()
        self._work(lambda: self._remove(slots, snap))

    def _remove(self, slots, snap):
        with lobby.locked(self.lobby_root) as lb:
            for s in slots:
                e = lb.remove(s)
                self.say(f"  removed {e['slot']} {e.get('name')}")
            rows = self._load_rows(lb, snap["ap"])
            self._summarise(lb, rows)

    def toggle_selected(self):
        slots = self._selected_slots()
        if not slots:
            return
        now_on = self.tree.set(slots[0], "in") == "Y"
        self.set_selected(not now_on)

    # ---------------------------------------------------------- upstream

    def check_upstream(self):
        """Ask GitHub whether any world in the lobby has a newer build.

        The lobby's games are what matter, so they are what gets checked -
        never the whole installed catalogue.
        """
        snap = self._snapshot()
        self._work(lambda: self._check_upstream(snap))

    def _check_upstream(self, snap):
        import check_upstream as up

        reg = {}
        path = os.path.join(HERE, "registry.json")
        if os.path.isfile(path):
            reg = json.load(open(path, encoding="utf-8"))
        registry = reg.get("repos", {})
        no_upstream = reg.get("no_upstream", {})

        worlds = {r["world"] for r in self.rows if r.get("world")}
        if not worlds:
            self.say("\nupstream check - no lobby game resolved to an installed "
                     "file, so there is nothing to check")
            self.msgs.put(("status", "Nothing to check against GitHub."))
            return
        self.msgs.put(("status", f"Checking {len(worlds)} world(s) against GitHub..."))
        self.say(f"\nupstream check - {len(worlds)} world(s) in the lobby")
        behind = 0
        for i, fn in enumerate(sorted(worlds), 1):
            slug = fn[: -len(".apworld")]
            self.msgs.put(("progress", int(100 * i / len(worlds))))
            if slug in no_upstream:
                self.say(f"  -  {slug:22} no upstream ({no_upstream[slug]})")
                continue
            folder = os.path.join(snap["ap"], "custom_worlds", fn)
            if not os.path.isfile(folder):
                folder = os.path.join(snap["ap"], "lib", "worlds", fn)
            try:
                _slug, manifest, repos, _size = up.read_world(folder)
            except Exception as exc:
                self.say(f"  ?  {slug:22} unreadable ({exc})")
                continue
            have = manifest.get("world_version")
            have_t = up.version_tuple(have)
            found = None
            for repo in ([registry[slug]] if slug in registry else []) + repos:
                try:
                    found = up.upstream_release(repo, slug)
                except RuntimeError as exc:
                    self.say(f"  !  {exc}")
                    found = None
                    break
                if found:
                    break
            if not found:
                self.say(f"  ?  {slug:22} no upstream found")
            elif found["version"] and have_t and found["version"] > have_t:
                behind += 1
                self.say(f"  !  {slug:22} {have} -> {found['tag']}  {found['repo']}")
                self.say(f"     {found['url']}")
            else:
                self.say(f"  ok {slug:22} {have or 'unversioned'} is current")
        self.msgs.put(("status",
                       f"{behind} world(s) behind upstream." if behind
                       else "Every world in the lobby is current."))

    # ---------------------------------------------------------- generate

    def generate(self):
        snap = self._snapshot()
        self._work(lambda: self._generate(snap))

    def _generate(self, snap):
        ap = snap["ap"]

        # Stage under the lock, then release it: generation takes minutes and
        # there is no reason to hold the lobby shut for all of it.
        with lobby.locked(self.lobby_root) as lb:
            problems = lb.problems()
            for p in problems:
                self.say(f"  ! {p}")
            if problems:
                raise RuntimeError("fix the problems above before generating")
            players = lb.records()
            excluded = [{"slot": e["slot"], "player": e.get("name")}
                        for e in lb.entries if not e.get("enabled", True)]
            index, used, missing = core.preflight(players, ap)
            if missing and not snap["allow"]:
                raise RuntimeError(f"{len(missing)} game(s) have no installed world")

            self.run_dir = core.new_run_dir()
            staged = {r["slot"]: r["yaml"]
                      for r in lb.stage(os.path.join(self.run_dir, "Players"))}
            for p in players:
                p["yaml"] = staged[p["slot"]]

        output_dir = os.path.join(self.run_dir, "output")
        os.makedirs(output_dir, exist_ok=True)
        self.say(f"\nout    {self.run_dir}")
        self.say(f"staged {len(players)} players"
                 + (f", {len(excluded)} sitting out" if excluded else ""))

        self.msgs.put(("status", "Generating - this can take several minutes."))
        self.bar.configure(mode="indeterminate")
        self.bar.start(12)
        self.say("generating...")
        try:
            seed_zip, log_text, code = core.run_generation(
                os.path.join(self.run_dir, "Players"), output_dir, ap, 1800,
                os.path.join(self.run_dir, "generate.log"))
        except FileNotFoundError as exc:
            raise RuntimeError(f"generator not found: {exc}")
        finally:
            self.bar.stop()
            self.bar.configure(mode="determinate")
            self.msgs.put(("progress", 100))

        if not seed_zip:
            self.say(f"generation FAILED (exit {code}) - offending lines:")
            for line in log_text.splitlines():
                if "Exception" in line or "is invalid" in line:
                    self.say("  " + line.strip()[:150])
            raise RuntimeError("generation failed, see the log pane")

        self.seed_zip = seed_zip
        size = os.path.getsize(seed_zip)
        self.say(f"seed   {seed_zip}")
        self.say(f"       {size:,} bytes")

        self.spoiler_path = core.extract_spoiler(seed_zip, self.run_dir)
        if self.spoiler_path:
            self.say(f"spoiler {os.path.basename(self.spoiler_path)} "
                     f"({os.path.getsize(self.spoiler_path):,} B) - full playthrough")

        warn = core.warnings_from(log_text)
        for w in warn:
            self.say("  warn " + w[:150])

        core.write_lock(os.path.join(self.run_dir, "run.lock.json"),
                        players=players, used=used, warnings=warn, ap_dir=ap,
                        seed_zip=seed_zip, spoiler=self.spoiler_path,
                        excluded=excluded)
        self.say("lock   run.lock.json written")

        must = sorted(w["file"] for w in used.values() if w["ships_client_code"])
        if must:
            self.say("players of these must have the identical world file: "
                     + ", ".join(must))

        dropped = [w for w in warn if "not a valid option" in w]
        if dropped:
            self.say(f"{len(dropped)} setting(s) were silently dropped - those players "
                     "are not getting what they configured.")

        self.msgs.put(("done", lambda: (self.open_btn.configure(state="normal"),
                                        self.copy_btn.configure(state="normal"),
                                        self.publish_btn.configure(state="normal"),
                                        self.spoiler_btn.configure(
                                            state="normal" if self.spoiler_path else "disabled"))))
        self.msgs.put(("status",
                       f"Seed ready: {os.path.basename(seed_zip)} ({size:,} bytes)"
                       + (f" - {len(warn)} warning(s)" if warn else "")))

    # ---------------------------------------------------------- publish

    def publish(self):
        """Upload the generated seed to archipelago.gg.

        Confirmed first: this puts the seed on a public host, and it cannot be
        taken back. Creating the room stays a separate, deliberate click.
        """
        if not self.seed_zip:
            return
        name = os.path.basename(self.seed_zip)
        size = os.path.getsize(self.seed_zip)
        if not messagebox.askokcancel(
                "Publish to archipelago.gg",
                f"Upload {name} ({size:,} bytes) to archipelago.gg?\n\n"
                "This puts the seed on a public host and cannot be undone.\n"
                "No room is created - you get a link to open when ready."):
            return
        self._work(self._publish)

    def _publish(self):
        import publish as pub

        self.msgs.put(("status", "Uploading to archipelago.gg..."))
        self.bar.configure(mode="indeterminate")
        self.bar.start(12)
        self.say(f"\npublishing {os.path.basename(self.seed_zip)}...")
        try:
            result = pub.publish(self.seed_zip)
        finally:
            self.bar.stop()
            self.bar.configure(mode="determinate")
            self.msgs.put(("progress", 100))

        self.published = result
        self.say(f"  seed    {result['seed_url']}")
        self.say(f"  spoiler {result['spoiler_url']}")
        self.say(f"  room    {result['new_room_url']}")
        self.say("  (opening the room link is what actually starts the server)")

        if self.run_dir:
            with open(os.path.join(self.run_dir, "published.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(result, fh, indent=2)
            self.say("  written to published.json")

        self.msgs.put(("done", lambda: self.room_btn.configure(state="normal")))
        self.msgs.put(("status", f"Published: {result['seed_url']}"))

    def open_room_link(self):
        """Open the room-creation link in the default browser."""
        if not self.published:
            return
        url = self.published["new_room_url"]
        if messagebox.askokcancel(
                "Create the room",
                f"Open {url} ?\n\nThis starts a live multiworld server that "
                "players can connect to."):
            import webbrowser
            webbrowser.open(url)
            self.msgs.put(("status", "Room link opened in your browser."))

    def open_spoiler(self):
        """Open the extracted spoiler. It is the whole playthrough - never shared."""
        if self.spoiler_path and os.path.isfile(self.spoiler_path):
            os.startfile(self.spoiler_path)  # noqa: S606 - Windows shell open

    def open_run(self):
        if self.run_dir and os.path.isdir(self.run_dir):
            os.startfile(self.run_dir)  # noqa: S606 - Windows shell open

    def copy_seed(self):
        if not self.seed_zip:
            return
        self.clipboard_clear()
        self.clipboard_append(self.seed_zip)
        self.msgs.put(("status", "Seed path copied to the clipboard."))


def main():
    root = tk.Tk()
    root.title("Archipelago Lobby Generator")
    root.geometry("1040x780")
    root.minsize(820, 580)
    App(root)          # App.__init__ applies the saved theme to root
    root.mainloop()


if __name__ == "__main__":
    main()
