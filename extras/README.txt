Drop .yaml configs here to include them in every run.

They are copied into each run's Players folder AFTER the lobby pull, so they
survive the pull rather than being wiped by it, and they go through the same
preflight - a game with no installed world still blocks generation.

If a file here has the same name as one pulled from the lobby, this copy is
saved alongside as <name>_extra.yaml rather than replacing it. The lobby's
version is the one the room validated, so it wins the name.

Use --no-extras to skip this folder for one run.
