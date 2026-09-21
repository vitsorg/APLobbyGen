# APLobbyGen

A local-first front end for running Archipelago multiworlds on Windows: keep the
roster on your own machine, generate from it with no network involved, and host
the resulting seed locally or publish it to archipelago.gg.

Standard library only - no pip install, no virtualenv. If you have Python and an
Archipelago install, you have everything.

## Why it exists

Most tooling around a multiworld treats a remote lobby as the source of truth.
That means every generation re-scrapes someone else's server, the roster
evaporates between runs, and nothing works offline. Here the **local lobby owns
the roster**; a remote room is one way to put configs into it, not the thing that
defines it.

Generation touches the network exactly never.

## What it does

- **A durable lobby.** Player configs live in `lobby/`, with a manifest, content
  history for superseded versions, and per-player include / sit-out so a roster
  survives between seeds instead of being rebuilt each time.
- **Import from anywhere.** An Ionium lobby room, a folder, a zip, or individual
  `.yaml` files. Re-importing an unchanged config is a no-op; a changed one
  updates in place and keeps the previous version.
- **Preflight that tells you the truth.** Every player's game is resolved against
  the `.apworld` files actually installed, so you learn about a missing world
  before generating, not after. Worlds that ship a client are flagged: every
  player needs the byte-identical file.
- **Dropped settings are a failure, not a footnote.** When a config asks for an
  option the installed world does not have, Archipelago drops it silently and
  generates a seed that looks fine while a player quietly does not get the game
  they configured. That exits non-zero here unless you waive it.
- **A run lock.** Every generation records what it was built from - world files,
  hashes, versions, sources - beside the seed, so a run stays explicable months
  later.
- **Local hosting.** Start the Archipelago server on this machine against the
  seed you just made, with the server console right there. Nothing is uploaded.
- **Publishing, when you want it.** Uploading to archipelago.gg is a separate,
  deliberate step that hands back the links rather than opening a room for you.
- **Light and dark.** Follows the Windows setting by default.

## Requirements

- Windows (the app shells out to `ArchipelagoGenerate.exe` and
  `ArchipelagoServer.exe`, and uses `os.startfile`)
- Python 3.10 or newer, with tkinter - the standard python.org installer has it
- An Archipelago install, by default `C:\ProgramData\Archipelago`

## Getting started

Launch the window:

```
python aplobby_gui.py
```

Or drive it from the command line:

```
python aplobby.py import folder path\to\configs   add configs to the lobby
python aplobby.py list                            show the roster
python aplobby.py generate                        build a seed, no network
python aplobby.py host                            host the newest seed locally
```

`generate` exits 0 on success, 1 if preflight failed, 2 if generation failed,
3 if an import source was unreachable, and 4 if the seed generated but a config
had settings silently dropped.

## Hosting locally

```
python aplobby.py host --port 38281
```

The server binds every interface, so other machines on your network can join at
the address it prints. Patch files are not served: a player on another machine
still needs their own file out of the seed zip.

Stopping is a clean `/exit` rather than a kill, because that is what flushes the
save file.

## Tests

Four suites, all offline except the one that deliberately hosts on loopback:

```
python selftest_lobby.py     the store: identity, history, crash recovery
python selftest_sources.py   the importers, with the network stubbed
python selftest_gui.py       drives the real window with nobody watching
python selftest_serve.py     really starts a server, really connects to it
```

They assert rather than print, so a silent pass is a real pass.

## Your data stays yours

`lobby/`, `runs/`, every `.yaml` and every seed are gitignored. Player configs
carry real names; spoilers are the whole playthrough. Nothing in this repository
contains either.

## Contributing

Changes are welcome as pull requests. Please read [LICENSE](LICENSE) first: this
is source-available rather than open source, and it asks that modifications come
back here instead of being published as a separate version. Fork, branch, open a
PR - that path is explicitly permitted.

If a change you need is not accepted, ask; a narrower permission can be granted
in writing.

## Licence

See [LICENSE](LICENSE). In short: run it freely for Archipelago multiworlds,
share it unmodified, send changes back as pull requests. Not affiliated with or
endorsed by the Archipelago project.
