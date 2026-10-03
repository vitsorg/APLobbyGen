r"""A settings form built from a game's own Archipelago template.

No game is special-cased. Every widget here is chosen from what the template
says an option is - a toggle, a number in a range, or a list to pick from - so
a world nobody has ever written code for gets the same editor as EarthBound.

    import optionsdlg
    changed = optionsdlg.edit(parent, game, current_values, ap_dir)
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox

import options as opts


class OptionsDialog(tk.Toplevel):
    def __init__(self, parent, game: str, current: dict, ap_dir: str, title=None):
        super().__init__(parent)
        self.title(title or f"{game} options")
        self.transient(parent)
        self.result: dict | None = None
        self.game = game

        full = opts.parse(game, ap_dir)
        # Lists and mappings - plando, start_inventory, exclude_locations - are
        # not settings with values to pick from. Offering them produced
        # "local_items: None", which fails generation outright. They are listed
        # as read-only at the end so nobody wonders where they went.
        self.spec = [o for o in full if o.get("editable")]
        self.untouched = [o for o in full if not o.get("editable")]
        self.vars: dict = {}
        self.kinds = {o["key"]: o["kind"] for o in self.spec}
        self.by_key = {o["key"]: o for o in self.spec}

        self.geometry("760x640")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        head = ttk.Frame(self, padding=(12, 10, 12, 4))
        head.grid(row=0, column=0, sticky="ew")
        ttk.Label(head, text=f"{game} - {len(self.spec)} options",
                  font=("", 10, "bold")).pack(side="left")
        ttk.Label(head, style="Muted.TLabel",
                  text="   read from Archipelago's own template for this world"
                  ).pack(side="left")
        if self.untouched:
            ttk.Label(head, style="Muted.TLabel",
                      text=f"   {len(self.untouched)} list option(s) left as they are"
                      ).pack(side="left")

        # A canvas, because 71 options do not fit on a screen.
        wrap = ttk.Frame(self)
        wrap.grid(row=1, column=0, sticky="nsew", padx=12)
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        canvas = tk.Canvas(wrap, highlightthickness=0, borderwidth=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        bar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=bar.set)
        body = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>",
                  lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(-e.delta // 120, "units"))
        try:
            canvas.configure(background=parent.palette["bg"])
        except Exception:
            pass

        self._build_rows(body, current)
        if self.untouched:
            ttk.Label(body, text="EDITED BY HAND ONLY", font=("", 8, "bold"),
                      style="Muted.TLabel").grid(column=0, columnspan=2,
                                                 sticky="w", pady=(16, 2))
            ttk.Label(body, style="Muted.TLabel", justify="left", wraplength=620,
                      text=", ".join(o["key"] for o in self.untouched) +
                      "\n- lists and mappings; this form leaves whatever the "
                      "config already has").grid(column=0, columnspan=2, sticky="w")

        feet = ttk.Frame(self, padding=12)
        feet.grid(row=2, column=0, sticky="ew")
        ttk.Button(feet, text="Save", command=self.save).pack(side="right")
        ttk.Button(feet, text="Cancel", command=self.destroy).pack(side="right", padx=6)
        ttk.Button(feet, text="Reset to template defaults",
                   command=self.reset).pack(side="left")

        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()

    def _build_rows(self, body, current):
        body.columnconfigure(1, weight=1)
        row, section = 0, object()
        for o in self.spec:
            if o["section"] != section:
                section = o["section"]
                ttk.Label(body, text=(section or "Options").upper(),
                          font=("", 8, "bold"), style="Muted.TLabel").grid(
                    row=row, column=0, columnspan=2, sticky="w", pady=(14, 2))
                row += 1

            value = current.get(o["key"], o["default"])
            ttk.Label(body, text=o["key"]).grid(row=row, column=0, sticky="w", padx=(0, 10))

            # Every kind is a combobox, including toggles. A checkbox cannot
            # hold "random" - and silently turning a config's "random" into
            # true/false would throw away what the player actually asked for.
            # A range stays editable so any number in its span can be typed.
            shown = self._as_text(o, value)
            var = tk.StringVar(value=shown)
            choices = [self._as_text(o, v) for v in o["values"]] + list(o.get("rolls", []))
            if o["kind"] == opts.RANGE:
                for edge in (o["min"], o["max"]):
                    if edge is not None and str(edge) not in choices:
                        choices.insert(0, str(edge))
            if shown not in choices:                   # whatever the config had
                choices.insert(0, shown)
            box = ttk.Combobox(body, textvariable=var, values=choices, width=28,
                               state="normal" if o["kind"] == opts.RANGE else "readonly")
            box.grid(row=row, column=1, sticky="w")
            if o["kind"] == opts.RANGE:
                ttk.Label(body, text=f"  {o['min']}..{o['max']}",
                          style="Muted.TLabel").grid(row=row, column=2, sticky="w")
            self.vars[o["key"]] = var
            row += 1

            doc = (o["doc"] or "").splitlines()
            if doc:
                ttk.Label(body, text=doc[0][:110], style="Muted.TLabel",
                          wraplength=560, justify="left").grid(
                    row=row, column=1, sticky="w", pady=(0, 4))
                row += 1

    def reset(self):
        for o in self.spec:
            self.vars[o["key"]].set(self._as_text(o, o["default"]))

    @staticmethod
    def _as_text(o: dict, value) -> str:
        """How a stored value should read in the box."""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def collect(self) -> dict:
        out = {}
        for key, var in self.vars.items():
            spec = self.by_key[key]
            text = var.get().strip()
            if text in spec.get("rolls", []):      # random, random-low, ...
                out[key] = text
                continue
            if spec["kind"] == opts.BOOL:
                out[key] = text == "true"
                continue
            if spec["kind"] == opts.RANGE:
                if text in spec.get("aliases", {}):        # "normal" -> 50
                    out[key] = spec["aliases"][text]
                    continue
                try:
                    number = int(text)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"{key}: {text!r} is not a number. Use {spec['min']}-"
                        f"{spec['max']}, or one of {', '.join(spec['rolls'])}.")
                if not (spec["min"] <= number <= spec["max"]):
                    raise ValueError(f"{key}: {number} is outside "
                                     f"{spec['min']}..{spec['max']}")
                out[key] = number
                continue
            out[key] = text
        return out

    def save(self):
        try:
            self.result = self.collect()
        except ValueError as exc:
            messagebox.showerror("Options", str(exc), parent=self)
            return
        self.destroy()


def edit(parent, game: str, current: dict, ap_dir: str) -> dict | None:
    """Show the form. Returns the chosen values, or None if cancelled."""
    dlg = OptionsDialog(parent, game, current, ap_dir)
    parent.wait_window(dlg)
    return dlg.result
