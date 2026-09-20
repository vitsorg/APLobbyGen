"""Desktop front end for aplobby.py.

Paste a lobby room URL, pull the roster, see at a glance which games this
install can actually handle, then generate. Standard library only (tkinter).

    python aplobby_gui.py
"""
from __future__ import annotations

import datetime
import json
import os
import queue
import subprocess
import sys
import threading
import zipfile
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import aplobby as core

DEFAULT_AP = r"C:\ProgramData\Archipelago"
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
        self.roster: list = []
        self.index: dict = {}
        self.run_dir: str | None = None
        self.seed_zip: str | None = None
        self.published: dict | None = None
        self.spoiler_path: str | None = None
        self.busy = False

        self._build_source()
        self._build_actions()
        self._build_table()
        self._build_log()
        self._build_status()

        self.after(100, self._drain)

    # ---------------------------------------------------------- layout

    def _build_source(self):
        box = ttk.LabelFrame(self, text="Lobby room", padding=8)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="URL or id").grid(row=0, column=0, padx=(0, 8))
        self.room_var = tk.StringVar()
        entry = ttk.Entry(box, textvariable=self.room_var)
        entry.grid(row=0, column=1, sticky="ew")
        entry.bind("<Return>", lambda _e: self.pull())

        self.pull_btn = ttk.Button(box, text="Pull roster", command=self.pull)
        self.pull_btn.grid(row=0, column=2, padx=(8, 0))

        ttk.Label(box, text="Archipelago").grid(row=1, column=0, pady=(8, 0), padx=(0, 8))
        self.ap_var = tk.StringVar(value=DEFAULT_AP)
        ttk.Entry(box, textvariable=self.ap_var).grid(row=1, column=1, sticky="ew", pady=(8, 0))
        ttk.Button(box, text="Browse", command=self._pick_ap).grid(
            row=1, column=2, padx=(8, 0), pady=(8, 0))

    def _build_actions(self):
        bar = ttk.Frame(self)
        bar.grid(row=1, column=0, sticky="ew", pady=(10, 6))

        self.gen_btn = ttk.Button(bar, text="Generate seed", command=self.generate,
                                  state="disabled")
        self.gen_btn.pack(side="left")

        self.upstream_btn = ttk.Button(bar, text="Check upstream",
                                       command=self.check_upstream, state="disabled")
        self.upstream_btn.pack(side="left", padx=6)

        self.players_btn = ttk.Button(bar, text="Add configs...",
                                      command=self.open_players, state="disabled")
        self.players_btn.pack(side="left", padx=(0, 6))

        self.rescan_btn = ttk.Button(bar, text="Rescan", command=self.rescan,
                                     state="disabled")
        self.rescan_btn.pack(side="left", padx=(0, 6))

        self.allow_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Generate even with missing worlds",
                        variable=self.allow_var,
                        command=self._refresh_gen_state).pack(side="left", padx=10)

        self.publish_btn = ttk.Button(bar, text="Publish to archipelago.gg",
                                      command=self.publish, state="disabled")
        self.publish_btn.pack(side="left", padx=6)

        self.room_btn = ttk.Button(bar, text="Create room...", command=self.open_room_link,
                                   state="disabled")
        self.room_btn.pack(side="left", padx=(0, 6))

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

        cols = ("player", "game", "world", "version", "state")
        self.tree = ttk.Treeview(box, columns=cols, show="headings", height=12)
        for col, label, width in (("player", "Player", 136), ("game", "Game", 232),
                                  ("world", "World file", 180), ("version", "Version", 120),
                                  ("state", "Status", 124)):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")

        bar = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=bar.set)

        self.tree.tag_configure("missing", foreground="#b3261e")
        self.tree.tag_configure("match", foreground="#7a5200")

    def _build_log(self):
        box = ttk.LabelFrame(self, text="Log", padding=6)
        box.grid(row=5, column=0, sticky="nsew", pady=(10, 0))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)

        self.log = tk.Text(box, height=9, wrap="none", font=("Consolas", 9))
        self.log.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(box, orient="vertical", command=self.log.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=bar.set, state="disabled")

    def _build_status(self):
        self.status = tk.StringVar(value="Paste a room URL and pull the roster.")
        ttk.Label(self, textvariable=self.status, anchor="w").grid(
            row=6, column=0, sticky="ew", pady=(8, 0))
        self.bar = ttk.Progressbar(self, mode="determinate")
        self.bar.grid(row=7, column=0, sticky="ew", pady=(4, 0))

    # ---------------------------------------------------------- plumbing

    def _pick_ap(self):
        chosen = filedialog.askdirectory(initialdir=self.ap_var.get(),
                                         title="Archipelago install folder")
        if chosen:
            self.ap_var.set(os.path.normpath(chosen))

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
        self.pull_btn.configure(state="disabled")
        self.gen_btn.configure(state="disabled")
        self.upstream_btn.configure(state="disabled")
        self.publish_btn.configure(state="disabled")
        self.players_btn.configure(state="disabled")
        self.rescan_btn.configure(state="disabled")

        def run():
            try:
                fn()
            except Exception as exc:  # surfaced in the log, never a silent death
                self.say(f"ERROR  {exc}")
                self.msgs.put(("status", f"Failed: {exc}"))
            finally:
                self.busy = False
                self.msgs.put(("done", self._reenable))

        threading.Thread(target=run, daemon=True).start()

    def _reenable(self):
        self.pull_btn.configure(state="normal")
        self.upstream_btn.configure(state="normal" if self.roster else "disabled")
        for b in (self.players_btn, self.rescan_btn):
            b.configure(state="normal" if self.run_dir else "disabled")
        self.publish_btn.configure(state="normal" if self.seed_zip else "disabled")
        self._refresh_gen_state()

    def _refresh_gen_state(self):
        ready = bool(self.roster) and not self.busy
        if ready and self._missing() and not self.allow_var.get():
            ready = False
        self.gen_btn.configure(state="normal" if ready else "disabled")

    def _missing(self):
        return [r for r in self.roster if not r.get("world")]

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
            if not r.get("world"):
                state, tag = "no world installed", "missing"
            elif r.get("must_match"):
                state, tag = "world must match", "match"
            else:
                state, tag = "ready", ""
            self.tree.insert("", "end", tags=(tag,),
                             values=(r["player"], r["game"], r.get("world") or "-",
                                     r.get("version") or "-", state))

    # ---------------------------------------------------------- actions

    def pull(self):
        raw = self.room_var.get().strip()
        if not raw:
            messagebox.showinfo("Room needed", "Paste a lobby room URL or id first.")
            return
        self._work(lambda: self._pull(raw))

    def _pull(self, raw):
        room = core.room_id(raw)
        self.msgs.put(("status", "Reading the room..."))
        self.msgs.put(("progress", 5))
        self.say(f"room   {room}")

        roster = core.scrape_roster(room)
        self.say(f"roster {len(roster)} players")

        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_dir = os.path.join(HERE, "runs", f"{room[:8]}-{stamp}")
        players_dir = os.path.join(self.run_dir, "Players")
        os.makedirs(players_dir, exist_ok=True)
        os.makedirs(os.path.join(self.run_dir, "output"), exist_ok=True)

        rows = []
        for i, (player, game, yid) in enumerate(roster, 1):
            data = core.fetch(f"{core.LOBBY}/room/{room}/download/{yid}")
            fn = core.safe_name(player, game)
            with open(os.path.join(players_dir, fn), "wb") as fh:
                fh.write(data)
            rows.append({"player": player, "game": game, "yaml": fn,
                         "yaml_bytes": len(data), "yaml_sha256": core.sha256(data)})
            self.msgs.put(("progress", 5 + int(70 * i / len(roster))))
            self.msgs.put(("status", f"Pulled {i}/{len(roster)} configs"))

        extras_dir = os.path.join(HERE, "extras")
        for e in core.collect_extras(extras_dir, players_dir):
            rows.append({"player": e["player"], "game": e["game"], "yaml": e["yaml"],
                         "yaml_bytes": e["yaml_bytes"], "yaml_sha256": e["yaml_sha256"],
                         "source": "extras"})
            self.say(f"  + extra  {e['player']}  {e['game'] or '? no game: line'}")

        self.say(f"pulled {len(rows)} configs into {players_dir}")

        self.index = core.index_worlds(self.ap_var.get())
        self.say(f"worlds {len(self.index)} games installed")
        anchors = self._anchors()
        for r in rows:
            hit = self.index.get(r["game"])
            r["world"] = hit["file"] if hit else None
            r["must_match"] = bool(hit and hit["ships_client_code"])
            r["version"] = self._version_for(hit, anchors) if hit else None

        self.roster = rows
        self.room = room
        self.msgs.put(("rows", rows))
        self.msgs.put(("progress", 100))

        missing = [r for r in rows if not r["world"]]
        for r in missing:
            self.say(f"  MISSING  {r['player']}  {r['game']}")
        if missing:
            self.msgs.put(("status",
                           f"{len(missing)} game(s) have no installed world - "
                           "install them, or tick the override."))
        else:
            self.msgs.put(("status", f"{len(rows)} players ready to generate."))

    def open_players(self):
        """Open this run's Players folder so configs can be dropped in by hand."""
        d = os.path.join(self.run_dir or "", "Players")
        if os.path.isdir(d):
            os.startfile(d)  # noqa: S606 - Windows shell open
            self.msgs.put(("status", "Drop .yaml files in, then press Rescan."))

    def rescan(self):
        """Re-read the Players folder and redo preflight.

        Lets a config be added between the pull and generation without losing
        what was already pulled - the table and the Generate gate both update.
        """
        self._work(self._rescan)

    def _rescan(self):
        players_dir = os.path.join(self.run_dir, "Players")
        known = {r["yaml"] for r in self.roster}
        anchors = self._anchors()
        self.index = core.index_worlds(self.ap_var.get())
        added = 0
        for fn in sorted(os.listdir(players_dir)):
            if not fn.lower().endswith((".yaml", ".yml")) or fn in known:
                continue
            data = open(os.path.join(players_dir, fn), "rb").read()
            name, game = core.yaml_fields(data)
            self.roster.append({"player": name or f"(unnamed: {fn})", "game": game or "?",
                                "yaml": fn, "yaml_bytes": len(data),
                                "yaml_sha256": core.sha256(data), "source": "added"})
            added += 1
            self.say(f"  + added  {name or fn}  {game or '? no game: line'}")
        for r in self.roster:
            hit = self.index.get(r["game"])
            r["world"] = hit["file"] if hit else None
            r["must_match"] = bool(hit and hit["ships_client_code"])
            r["version"] = self._version_for(hit, anchors) if hit else None
        self.msgs.put(("rows", self.roster))
        missing = [r for r in self.roster if not r["world"]]
        self.msgs.put(("status",
                       f"{len(self.roster)} configs, {added} newly added"
                       + (f" - {len(missing)} still have no world." if missing else ".")))

    def check_upstream(self):
        """Ask GitHub whether any world in this room has a newer build.

        A stale world is the quiet failure: it generates fine, then drops the
        settings a player wrote for a newer build. Worth knowing beforehand.
        """
        self._work(self._check_upstream)

    def _check_upstream(self):
        import check_upstream as up

        reg = {}
        path = os.path.join(HERE, "registry.json")
        if os.path.isfile(path):
            reg = json.load(open(path, encoding="utf-8"))
        registry = reg.get("repos", {})
        no_upstream = reg.get("no_upstream", {})

        worlds = {r["world"] for r in self.roster if r.get("world")}
        self.msgs.put(("status", f"Checking {len(worlds)} world(s) against GitHub..."))
        self.say(f"\nupstream check - {len(worlds)} world(s) in this room")
        behind = 0
        for i, fn in enumerate(sorted(worlds), 1):
            slug = fn[: -len(".apworld")]
            self.msgs.put(("progress", int(100 * i / len(worlds))))
            if slug in no_upstream:
                self.say(f"  -  {slug:22} no upstream ({no_upstream[slug]})")
                continue
            folder = os.path.join(self.ap_var.get(), "custom_worlds", fn)
            if not os.path.isfile(folder):
                folder = os.path.join(self.ap_var.get(), "lib", "worlds", fn)
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
                       else "Every world in this room is current."))

    def generate(self):
        self._work(self._generate)

    def _generate(self):
        ap = self.ap_var.get()
        exe = os.path.join(ap, "ArchipelagoGenerate.exe")
        if not os.path.isfile(exe):
            raise RuntimeError(f"generator not found: {exe}")

        players_dir = os.path.join(self.run_dir, "Players")
        output_dir = os.path.join(self.run_dir, "output")
        log_path = os.path.join(self.run_dir, "generate.log")

        self.msgs.put(("status", "Generating - this can take several minutes."))
        self.msgs.put(("progress", 0))
        self.bar.configure(mode="indeterminate")
        self.bar.start(12)
        self.say("generating...")

        try:
            with open(log_path, "wb") as log:
                subprocess.run([exe, "--player_files_path", players_dir,
                                "--outputpath", output_dir],
                               stdout=log, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, timeout=1800)
        finally:
            self.bar.stop()
            self.bar.configure(mode="determinate")
            self.msgs.put(("progress", 100))

        text = open(log_path, encoding="utf-8", errors="replace").read()
        zips = [f for f in os.listdir(output_dir) if f.endswith(".zip")]
        if not zips:
            self.say("generation FAILED - offending lines:")
            for line in text.splitlines():
                if "Exception" in line or "is invalid" in line:
                    self.say("  " + line.strip()[:150])
            raise RuntimeError("generation failed, see the log pane")

        self.seed_zip = os.path.join(output_dir, zips[0])
        size = os.path.getsize(self.seed_zip)
        self.say(f"seed   {self.seed_zip}")
        self.say(f"       {size:,} bytes")

        # Lift the spoiler out so it survives and can be read without unpacking.
        self.spoiler_path = None
        try:
            zf = zipfile.ZipFile(self.seed_zip)
            inner = next((n for n in zf.namelist() if n.endswith("_Spoiler.txt")), None)
            if inner:
                self.spoiler_path = os.path.join(self.run_dir, os.path.basename(inner))
                with open(self.spoiler_path, "wb") as fh:
                    fh.write(zf.read(inner))
                self.say(f"spoiler {os.path.basename(self.spoiler_path)} "
                         f"({os.path.getsize(self.spoiler_path):,} B) - full playthrough")
        except Exception as exc:
            self.say(f"  could not extract the spoiler: {exc}")

        warn = [l.strip() for l in text.splitlines()
                if "not a valid option" in l or "Invalid or missing manifest" in l]
        for w in warn:
            self.say("  warn " + w[:150])

        used = {r["game"]: self.index[r["game"]] for r in self.roster if r.get("world")}
        lock = {
            "schema": "aplobby-run/1",
            "generated": datetime.datetime.now().isoformat(timespec="seconds"),
            "lobby_room": self.room,
            "seed_zip": os.path.basename(self.seed_zip),
            "seed_sha256": core.sha256(open(self.seed_zip, "rb").read()),
            "players": [{k: v for k, v in r.items() if k != "must_match"} for r in self.roster],
            "worlds": {g: {k: v for k, v in w.items() if k != "path"} for g, w in used.items()},
            "warnings": warn,
        }
        with open(os.path.join(self.run_dir, "run.lock.json"), "w") as fh:
            json.dump(lock, fh, indent=2)
        self.say("lock   run.lock.json written")

        self.msgs.put(("done", lambda: (self.open_btn.configure(state="normal"),
                                        self.copy_btn.configure(state="normal"),
                                        self.publish_btn.configure(state="normal"),
                                        self.spoiler_btn.configure(
                                            state="normal" if self.spoiler_path else "disabled"))))
        self.msgs.put(("status",
                       f"Seed ready: {os.path.basename(self.seed_zip)} "
                       f"({size:,} bytes)" + (f" - {len(warn)} warning(s)" if warn else "")))

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
            with open(os.path.join(self.run_dir, "published.json"), "w") as fh:
                json.dump(result, fh, indent=2)
            self.say("  written to published.json")

        self.msgs.put(("done", lambda: self.room_btn.configure(state="normal")))
        self.msgs.put(("status", f"Published: {result['seed_url']}"))

    def open_room_link(self):
        """Open the room-creation link in the default browser."""
        if not getattr(self, "published", None):
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
    root.geometry("980x760")
    root.minsize(760, 560)
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
