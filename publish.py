"""Publish a generated seed to archipelago.gg and read back its links.

The Host Game page takes a multipart POST with one `file` field and redirects
to /seed/<id>. That page carries the spoiler download and the link that spins
up a room. This does not create the room - it hands back the link so a person
decides when to open it.

    python publish.py path\\to\\AP_123.zip

Standard library only.
"""
from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.request
import uuid

BASE = "https://archipelago.gg"
UPLOAD = f"{BASE}/uploads"
UA = {"User-Agent": "aplobby-publish"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Capture the redirect instead of following it - the Location IS the result."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _Redirected(newurl)


class _Redirected(Exception):
    def __init__(self, url):
        self.url = url
        super().__init__(url)


def _multipart(field: str, filename: str, data: bytes):
    """Encode one file as multipart/form-data. Returns (content_type, body)."""
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {ctype}\r\n\r\n".encode(),
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    return f"multipart/form-data; boundary={boundary}", body


def publish(zip_path: str, timeout: int = 180) -> dict:
    """Upload a seed zip. Returns seed id and the links that page offers."""
    if not os.path.isfile(zip_path):
        raise FileNotFoundError(zip_path)
    data = open(zip_path, "rb").read()
    ctype, body = _multipart("file", os.path.basename(zip_path), data)

    req = urllib.request.Request(UPLOAD, data=body, headers={**UA, "Content-Type": ctype})
    opener = urllib.request.build_opener(_NoRedirect)
    seed_url = None
    try:
        with opener.open(req, timeout=timeout) as resp:
            # No redirect means the upload was rejected and the form came back.
            page = resp.read().decode("utf-8", "replace")
            note = re.search(r'class="[^"]*(?:error|message)[^"]*"[^>]*>(.*?)<', page, re.S)
            raise RuntimeError("upload was not accepted" +
                               (f": {html.unescape(note.group(1)).strip()}" if note else ""))
    except _Redirected as red:
        seed_url = red.url if red.url.startswith("http") else BASE + red.url

    seed_id = seed_url.rstrip("/").rsplit("/", 1)[-1]
    page = urllib.request.urlopen(
        urllib.request.Request(seed_url, headers=UA), timeout=60).read().decode("utf-8", "replace")

    links = {html.unescape(h) for h in re.findall(r'href="([^"]+)"', page)}
    absolute = lambda u: u if u.startswith("http") else BASE + u
    spoiler = next((absolute(u) for u in links if "/dl_spoiler/" in u), None)
    new_room = next((absolute(u) for u in links if "/new_room/" in u), None)
    patches = sorted(absolute(u) for u in links if "/dl_patch/" in u)

    return {
        "seed_id": seed_id,
        "seed_url": seed_url,
        "spoiler_url": spoiler,
        "new_room_url": new_room or f"{BASE}/new_room/{seed_id}",
        "patch_urls": patches,
        "uploaded_bytes": len(data),
        "uploaded_file": os.path.basename(zip_path),
    }


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    try:
        result = publish(sys.argv[1])
    except (urllib.error.URLError, RuntimeError, FileNotFoundError) as exc:
        print(f"publish failed: {exc}")
        return 1
    print(json.dumps(result, indent=2))
    print(f"\nseed    {result['seed_url']}")
    print(f"spoiler {result['spoiler_url']}")
    print(f"room    {result['new_room_url']}   <- opening this creates the room")
    return 0


if __name__ == "__main__":
    sys.exit(main())
