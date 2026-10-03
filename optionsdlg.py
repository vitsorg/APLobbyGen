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

        self.spec = opts.parse(game, ap_dir)
        self.vars: dict = {}
        self.kinds = {o["key"]: o["kind"] for o in self.spec}

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

            if o["kind"] == opts.BOOL:
                var = tk.BooleanVar(value=bool(value) if not isinstance(value, str)
                                    else value == "true")
                ttk.Checkbutton(body, variable=var).grid(row=row, column=1, sticky="w")
            elif o["kind"] == opts.RANGE:
                var = tk.StringVar(value=str(value))
                ttk.Spinbox(body, textvariable=var, width=10,
                            from_=o["min"], to=o["max"]).grid(row=row, column=1, sticky="w")
            else:
                var = tk.StringVar(value=str(value))
                choices = [str(v) for v in o["values"]]
                if str(value) not in choices:          # a value the template lost
                    choices = [str(value)] + choices
                ttk.Combobox(body, textvariable=var, values=choices, width=28,
                             state="readonly").grid(row=row, column=1, sticky="w")
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
            var = self.vars[o["key"]]
            if o["kind"] == opts.BOOL:
                var.set(bool(o["default"]))
            else:
                var.set(str(o["default"]))

    def collect(self) -> dict:
        out = {}
        for key, var in self.vars.items():
            value = var.get()
            if self.kinds[key] == opts.RANGE:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise ValueError(f"{key}: {value!r} is not a number")
            out[key] = value
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
