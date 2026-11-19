#!/usr/bin/env python3
"""Voidwerks radio control: artist search + station selection.

- Scans MUSIC_DIR once (tags cached), keeps an in-memory artist index.
- GET  /artists?q=...   search artists (empty q = biggest artists)
- GET  /selection       what the station is currently restricted to
- POST /select          {"artists": [...]} restrict the station ([] = all music)

Selecting writes an .m3u that Liquidsoap watches, then skips the current track
so the change is heard within a few seconds. Listens on localhost only; nginx
fronts it.
"""
import json
import os
import sys
import threading
import time
import unicodedata
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import mutagen

MUSIC_DIR = os.environ.get("MUSIC_DIR", "/mnt/music/FLAC1")
STATE_DIR = os.environ.get("STATE_DIR", "/var/lib/radio-control")
PORT = int(os.environ.get("PORT", "8103"))
SKIP_URL = os.environ.get("SKIP_URL", "http://127.0.0.1:8102/skip")
SKIP_DELAY = float(os.environ.get("SKIP_DELAY", "3"))
COOLDOWN = float(os.environ.get("COOLDOWN", "5"))
RESCAN_SECONDS = 30 * 60
EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac"}

M3U = os.path.join(STATE_DIR, "selected.m3u")
SEL_FILE = os.path.join(STATE_DIR, "selection.json")
CACHE_FILE = os.path.join(STATE_DIR, "index-cache.json")

LOCK = threading.Lock()
INDEX = {}          # normalized name -> [display name, [paths]]
INDEXING = True
SCANNED = 0
SELECTION = {"artists": [], "tracks": 0, "at": 0}
LAST_SELECT = 0.0


def norm(s):
    return unicodedata.normalize("NFKC", s).casefold().strip()


def read_names(path):
    """Artist + album artist names from a file's tags, de-duplicated."""
    try:
        f = mutagen.File(path, easy=True)
    except Exception:
        return []
    if f is None or not getattr(f, "tags", None):
        return []
    seen, out = set(), []
    for key in ("artist", "albumartist"):
        try:
            values = f.tags.get(key) or []
        except Exception:
            values = []
        for v in values:
            v = str(v).strip()
            k = norm(v)
            if k and k not in seen:
                seen.add(k)
                out.append(v)
    return out


def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def scan_once():
    global INDEX, INDEXING, SCANNED
    INDEXING = True
    SCANNED = 0
    cache = load_cache()
    new_cache = {}
    idx = {}
    for root, _dirs, files in os.walk(MUSIC_DIR, followlinks=True):
        for fn in files:
            if os.path.splitext(fn)[1].lower() not in EXTS:
                continue
            p = os.path.join(root, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            c = cache.get(p)
            if c and c[0] == st.st_mtime and c[1] == st.st_size:
                names = c[2]
            else:
                names = read_names(p)
            new_cache[p] = [st.st_mtime, st.st_size, names]
            for name in names:
                ent = idx.setdefault(norm(name), [name, []])
                ent[1].append(p)
            SCANNED += 1
    with LOCK:
        INDEX = idx
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(new_cache, fh, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        print("cache save failed:", e, file=sys.stderr)
    INDEXING = False
    print("indexed %d files, %d artists" % (SCANNED, len(idx)), flush=True)


def scan_loop():
    while True:
        try:
            scan_once()
        except Exception as e:
            print("scan failed:", e, file=sys.stderr, flush=True)
            globals()["INDEXING"] = False
        time.sleep(RESCAN_SECONDS)


def search(q, limit=40):
    qn = norm(q)
    with LOCK:
        idx = INDEX
    items = []
    for k, (name, paths) in idx.items():
        if not qn:
            items.append((0, -len(paths), name, len(paths)))
        elif qn in k:
            items.append((0 if k.startswith(qn) else 1, len(k), name, len(paths)))
    items.sort(key=lambda t: t[:3])
    return [{"name": n, "tracks": c} for _a, _b, n, c in items[:limit]]


def write_m3u(paths):
    # write in place (same inode) so Liquidsoap's file watcher keeps working
    with open(M3U, "wb") as fh:
        for p in paths:
            fh.write(p.encode("utf-8", "surrogateescape") + b"\n")


def trigger_skip():
    try:
        req = urllib.request.Request(SKIP_URL, data=b"", method="POST")
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print("skip failed:", e, file=sys.stderr, flush=True)


def apply_selection(names):
    """Returns (status, payload)."""
    global SELECTION, LAST_SELECT
    with LOCK:
        now = time.time()
        if now - LAST_SELECT < COOLDOWN:
            return 429, {"error": "cooldown"}
        idx = INDEX
        picked, paths, seen = [], [], set()
        for n in names[:100]:
            ent = idx.get(norm(str(n)))
            if not ent:
                continue
            picked.append(ent[0])
            for p in ent[1]:
                if p not in seen:
                    seen.add(p)
                    paths.append(p)
        if names and not paths:
            return 400, {"error": "no matching artists"}
        LAST_SELECT = now
        write_m3u(paths)
        SELECTION = {"artists": picked, "tracks": len(paths), "at": int(now)}
        try:
            with open(SEL_FILE, "w", encoding="utf-8") as fh:
                json.dump(SELECTION, fh, ensure_ascii=False)
        except Exception as e:
            print("selection save failed:", e, file=sys.stderr)
        sel = dict(SELECTION)
    # give Liquidsoap a moment to reload the playlist, then skip to apply it
    threading.Timer(SKIP_DELAY, trigger_skip).start()
    return 200, sel


class Handler(BaseHTTPRequestHandler):
    server_version = "radio-control"

    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/artists":
            q = (parse_qs(u.query).get("q") or [""])[0][:100]
            self._send(200, {"artists": search(q), "indexing": INDEXING, "scanned": SCANNED})
        elif u.path == "/selection":
            self._send(200, dict(SELECTION, indexing=INDEXING))
        elif u.path == "/health":
            self._send(200, {"ok": True, "indexing": INDEXING, "artists": len(INDEX)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/select":
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > 65536:
            self._send(413, {"error": "too large"})
            return
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
            names = data.get("artists", [])
            if not isinstance(names, list):
                raise ValueError
        except Exception:
            self._send(400, {"error": "bad request"})
            return
        code, payload = apply_selection(names)
        self._send(code, payload)


def main():
    global SELECTION
    os.makedirs(STATE_DIR, exist_ok=True)
    if not os.path.exists(M3U):
        open(M3U, "wb").close()
        os.chmod(M3U, 0o644)
    try:
        with open(SEL_FILE, encoding="utf-8") as fh:
            SELECTION = json.load(fh)
    except Exception:
        pass
    threading.Thread(target=scan_loop, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
