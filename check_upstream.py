"""Check installed world files against their upstream GitHub releases.

Most apworlds name their own home somewhere inside themselves - in the setup
docs, a docstring, an issues link. This walks every installed world, harvests
those GitHub URLs, and asks each candidate repo whether it publishes a newer
build of that same world file.

    python check_upstream.py                 # custom_worlds
    python check_upstream.py --all           # bundled worlds too
    python check_upstream.py --download DIR  # fetch newer builds (does not install)

A repo only counts as that world's upstream if it actually publishes an asset
matching the world's own filename - so a link to BizHawk or to Archipelago core
is ignored rather than mistaken for the source.

Set GITHUB_TOKEN to raise the unauthenticated rate limit (60 requests/hour).

Exit codes: 0 everything current or unknown, 3 at least one world is behind.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile

API = "https://api.github.com"
SKIP_REPOS = {
    "archipelagomw/archipelago",      # core, not any single world's upstream
    "tasemulators/bizhawk",           # emulator
    "zamiell/isaac-save-installer",   # tooling
}
TEXT_EXT = (".py", ".md", ".json", ".txt", ".yaml", ".yml")


def gh(url: str):
    req = urllib.request.Request(url, headers={
        "User-Agent": "aplobby-upstream",
        "Accept": "application/vnd.github+json",
    })
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def version_tuple(text: str):
    """Leading numeric version out of a tag or version string, or None.

    'mmx6-v0.4.0' -> (0,4,0);  '1.2.3+hotfix1' -> (1,2,3);  '20.1.13' -> (20,1,13)
    """
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)+)", str(text))
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def read_world(path: str):
    """(slug, manifest, github repos mentioned inside)."""
    slug = os.path.basename(path)[: -len(".apworld")]
    data = open(path, "rb").read()
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return slug, {}, [], len(data)
    names = z.namelist()
    manifest = {}
    j = next((n for n in names if n.endswith("archipelago.json")), None)
    if j:
        try:
            manifest = json.loads(z.read(j))
        except Exception:
            pass
    blob = []
    for n in names:
        if n.endswith(TEXT_EXT):
            try:
                blob.append(z.read(n).decode("utf-8", "replace"))
            except Exception:
                pass
    repos, seen = [], set()
    for owner, repo in re.findall(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
                                  "\n".join(blob)):
        # rstrip takes a character SET, not a suffix: "AP-Kit".rstrip(".git")
        # is "AP-K". Strip the real suffix instead.
        if repo.endswith(".git"):
            repo = repo[: -len(".git")]
        key = f"{owner}/{repo}".lower()
        if key in SKIP_REPOS or key in seen:
            continue
        seen.add(key)
        repos.append(f"{owner}/{repo}")
    return slug, manifest, repos, len(data)


def upstream_release(repo: str, slug: str):
    """Newest release of `repo` publishing an asset for `slug`, or None."""
    try:
        releases = gh(f"{API}/repos/{repo}/releases?per_page=30")
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise RuntimeError("GitHub rate limit reached - set GITHUB_TOKEN") from exc
        return None
    except urllib.error.URLError:
        return None
    best = None
    for rel in releases:
        for asset in rel.get("assets", []):
            name = asset["name"]
            if not name.endswith(".apworld"):
                continue
            # the asset must be this world, not a sibling world in the same repo
            if name[: -len(".apworld")].split("-")[0] != slug:
                continue
            # The asset filename wins over the tag. Some repos tag by CLIENT
            # version while shipping a differently-versioned world - e.g. tag
            # 1.1.2 carrying spire2-1.1.1.apworld. Trusting the tag there
            # reports an upgrade that does not exist.
            stem = name[: -len(".apworld")]
            ver = version_tuple(stem[len(slug):]) or version_tuple(rel["tag_name"])
            cand = {"repo": repo, "tag": rel["tag_name"], "version": ver,
                    "asset": name, "url": asset["browser_download_url"],
                    "published": (rel.get("published_at") or "")[:10]}
            if best is None or (ver or ()) > (best["version"] or ()):
                best = cand
    return best


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare installed worlds to upstream releases.")
    ap.add_argument("--ap", default=r"C:\ProgramData\Archipelago")
    ap.add_argument("--all", action="store_true", help="include the bundled worlds")
    ap.add_argument("--download", metavar="DIR", help="download newer builds here (no install)")
    ap.add_argument("--only", help="comma-separated slugs to check")
    args = ap.parse_args()

    folders = [os.path.join(args.ap, "custom_worlds")]
    if args.all:
        folders.append(os.path.join(args.ap, "lib", "worlds"))

    files = []
    for folder in folders:
        if os.path.isdir(folder):
            files += [os.path.join(folder, f) for f in sorted(os.listdir(folder))
                      if f.endswith(".apworld")]
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        files = [f for f in files
                 if os.path.basename(f)[: -len(".apworld")] in wanted]

    reg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "registry.json")
    registry, no_upstream, anchors = {}, {}, {}
    if os.path.isfile(reg_path):
        blob = json.load(open(reg_path, encoding="utf-8"))
        registry = blob.get("repos", {})
        no_upstream = blob.get("no_upstream", {})
        anchors = blob.get("anchors", {})

    print(f"checking {len(files)} world file(s)\n")
    behind, unknown, current, known_none = [], [], [], []

    for path in files:
        slug, manifest, repos, size = read_world(path)
        have = manifest.get("world_version")
        have_t = version_tuple(have)
        game = manifest.get("game") or "?"

        # An anchor is a version the world states about its engine or its
        # provenance, not about itself. Shown because "no world_version" is not
        # the same as "nothing is known", but never treated as identity.
        anchor = anchors.get(slug)

        def show_anchor():
            if anchor:
                print(f"     {'':22} {'':12} anchor: {anchor['name']} "
                      f"{anchor['version']} ({anchor['kind']})")

        if slug in no_upstream:
            known_none.append(slug)
            print(f"  -  {slug:22} {str(have or '-'):12} no upstream          ({no_upstream[slug]})")
            show_anchor()
            continue

        # registry first: it holds repos a world does not name inside itself
        candidates = ([registry[slug]] if slug in registry else []) + repos

        found = None
        for repo in candidates:
            try:
                found = upstream_release(repo, slug)
            except RuntimeError as exc:
                print(f"\n{exc}")
                return 0
            if found:
                break

        if not found:
            unknown.append(slug)
            hint = repos[0] if repos else "no github link inside"
            print(f"  ?  {slug:22} {str(have or '-'):12} upstream not found   ({hint})")
            show_anchor()
            continue

        newer = bool(found["version"] and have_t and found["version"] > have_t)
        if newer:
            behind.append((slug, have, found))
            print(f"  !  {slug:22} {str(have or '-'):12} -> {found['tag']:16} "
                  f"{found['published']}  {found['repo']}")
            if args.download:
                os.makedirs(args.download, exist_ok=True)
                dest = os.path.join(args.download, f"{slug}-{found['tag']}.apworld")
                with urllib.request.urlopen(urllib.request.Request(
                        found["url"], headers={"User-Agent": "aplobby-upstream"}),
                        timeout=120) as r, open(dest, "wb") as fh:
                    fh.write(r.read())
                print(f"     downloaded -> {dest}")
        else:
            current.append(slug)
            same = "same" if found["version"] == have_t else f"upstream {found['tag']}"
            print(f"  ok {slug:22} {str(have or '-'):12} {same:16} {found['repo']}")
            show_anchor()

    print(f"\n{len(current)} current, {len(behind)} behind, "
          f"{len(known_none)} no upstream, {len(unknown)} unknown")
    if behind:
        print("\nbehind upstream:")
        for slug, have, found in behind:
            print(f"  {slug}: {have} -> {found['tag']}  {found['url']}")
        print("\nA newer world can mean a player's config uses options this build "
              "does not have, which the generator drops silently.")
    if unknown:
        print(f"\nno upstream identified: {', '.join(unknown)}")
        print("Those name no GitHub repo that publishes their world file. Add one to "
              "registry.json, or record it under no_upstream once confirmed.")
    return 3 if behind else 0


if __name__ == "__main__":
    sys.exit(main())
