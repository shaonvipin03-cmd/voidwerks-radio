#!/usr/bin/env python3
"""Voidwerks radio, private side: YouTube audio library + share-to-public-station.

Listens on localhost only. nginx exposes it ONLY on the tailnet-only (Tailscale Serve)
copy of the page, never on the public Funnel one.

  GET  /health
  GET  /jobs                 recent download jobs
  GET  /library              downloaded files (title, artist, duration, size)
  POST /add     {"url"}      download audio from a YouTube video or playlist link
  POST /delete  {"name"}     delete a file from the library
  POST /share   {"name","now"}  put a file on the PUBLIC station (next track, or now)
  POST /unshare              empty the public station's shared queue

Sharing writes shared.m3u, which Liquidsoap watches. "now" also skips the current
public track a few seconds later so the shared one starts right away.
"""
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import mutagen

LIB_DIR = os.environ.get("LIB_DIR", "/var/lib/radio-private/library")
STATE_DIR = os.environ.get("STATE_DIR", "/var/lib/radio-private")
SHARED_M3U = os.environ.get("SHARED_M3U", os.path.join(STATE_DIR, "shared.m3u"))
PORT = int(os.environ.get("PORT", "8105"))
SKIP_URL = os.environ.get("SKIP_URL", "http://127.0.0.1:8102/skipother")
SKIP_DELAY = float(os.environ.get("SKIP_DELAY", "3"))
YTDLP = os.environ.get("YTDLP", "yt-dlp")
YTDLP_EXTRA = shlex.split(os.environ.get("YTDLP_EXTRA", ""))
MAX_HOURS = float(os.environ.get("MAX_HOURS", "8"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "1500"))
MAX_LIBRARY_GB = float(os.environ.get("MAX_LIBRARY_GB", "30"))
MAX_PLAYLIST = int(os.environ.get("MAX_PLAYLIST", "100"))
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", str(3 * 3600)))
MAX_QUEUE = 10

AUDIO_EXTS = {".m4a", ".mp3", ".opus", ".ogg", ".webm", ".flac", ".aac", ".wav"}
ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
                 "music.youtube.com", "youtu.be"}
PARTIAL = re.compile(r"\.f\d+\.[A-Za-z0-9]+$|\.temp\.")

JOBS = []                 # newest last, capped
JOBS_LOCK = threading.Lock()
WORK = queue.Queue()
NEXT_ID = [1]
META = {}                 # file name -> (mtime, size, info)


# ---------- helpers ----------

def clean_url(raw):
    """Return (url, is_playlist) or raise ValueError. Only YouTube links are accepted."""
    if not isinstance(raw, str):
        raise ValueError("bad url")
    raw = raw.strip()
    if not (0 < len(raw) <= 300) or not re.fullmatch(r"[\x21-\x7e]+", raw):
        raise ValueError("bad url")
    u = urlparse(raw)
    if u.scheme not in ("http", "https") or (u.hostname or "").lower() not in ALLOWED_HOSTS:
        raise ValueError("only youtube.com / youtu.be links are allowed")
    return raw, u.path.rstrip("/") == "/playlist"


def lib_path(name):
    """Absolute path of a file inside the library, or None."""
    try:
        if (not isinstance(name, str) or not name or name != os.path.basename(name)
                or name.startswith(".") or "\x00" in name):
            return None
        p = os.path.join(LIB_DIR, name)
        return p if os.path.isfile(p) else None
    except Exception:
        return None


def read_info(path):
    d = {"title": "", "artist": "", "duration": 0}
    try:
        f = mutagen.File(path, easy=True)
        if f is not None:
            length = getattr(f.info, "length", 0) if f.info is not None else 0
            d["duration"] = int(length or 0)
            tags = f.tags or {}
            for k in ("title", "artist"):
                v = tags.get(k)
                if v:
                    d[k] = str(v[0])[:200]
    except Exception:
        pass
    return d


def lib_files():
    out = []
    try:
        names = os.listdir(LIB_DIR)
    except OSError:
        return out
    for n in names:
        if n.startswith(".") or os.path.splitext(n)[1].lower() not in AUDIO_EXTS or PARTIAL.search(n):
            continue
        p = os.path.join(LIB_DIR, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        if not os.path.isfile(p):
            continue
        c = META.get(n)
        if not c or c[0] != st.st_mtime or c[1] != st.st_size:
            c = (st.st_mtime, st.st_size, read_info(p))
            META[n] = c
        item = {"name": n, "size": st.st_size, "added": int(st.st_mtime)}
        item.update(c[2])
        out.append(item)
    out.sort(key=lambda f: f["added"], reverse=True)
    return out


def library_bytes():
    total = 0
    try:
        for n in os.listdir(LIB_DIR):
            try:
                total += os.path.getsize(os.path.join(LIB_DIR, n))
            except OSError:
                pass
    except OSError:
        pass
    return total


def shared_names():
    try:
        with open(SHARED_M3U, "rb") as fh:
            lines = fh.read().decode("utf-8", "surrogateescape").splitlines()
    except OSError:
        return []
    return [os.path.basename(x) for x in lines if x.strip()]


def write_shared(paths):
    # write in place (same inode) so Liquidsoap's file watcher keeps working
    with open(SHARED_M3U, "wb") as fh:
        for p in paths:
            fh.write(p.encode("utf-8", "surrogateescape") + b"\n")


def trigger_skip():
    try:
        req = urllib.request.Request(SKIP_URL, data=b"", method="POST")
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print("skip failed:", e, file=sys.stderr, flush=True)


# ---------- downloads ----------

def run_job(job):
    url, is_playlist = job["url"], job["playlist"]
    cache = os.path.join(STATE_DIR, ".cache")
    cmd = [YTDLP, "--ignore-config", "--no-warnings", "--newline", "--no-colors",
           "-f", "bestaudio[ext=m4a]/bestaudio/best",
           "-x", "--audio-format", "m4a",
           "--embed-metadata", "--no-mtime",
           "--max-filesize", "%dM" % MAX_FILE_MB,
           "--match-filter", "duration<=%d" % int(MAX_HOURS * 3600),
           "--socket-timeout", "30", "--retries", "3",
           "--cache-dir", cache,
           "-P", LIB_DIR,
           "-o", "%(title).120B [%(id)s].%(ext)s"]
    if is_playlist:
        cmd += ["--yes-playlist", "--playlist-end", str(MAX_PLAYLIST)]
    else:
        cmd += ["--no-playlist"]
    cmd += YTDLP_EXTRA
    cmd += ["--", url]          # "--" so a link can never be read as an option

    job["state"] = "running"
    job["line"] = "starting"
    last_error = ""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", bufsize=1, stdin=subprocess.DEVNULL)
    except OSError as e:
        job["state"], job["line"] = "error", "cannot start yt-dlp: %s" % e
        return
    timer = threading.Timer(JOB_TIMEOUT, proc.kill)
    timer.start()
    try:
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("ERROR"):
                last_error = line
            job["line"] = line[:200]
        code = proc.wait()
    finally:
        timer.cancel()
    if code == 0:
        job["state"], job["line"] = "done", "done"
    else:
        job["state"] = "error"
        job["line"] = (last_error or job["line"] or "failed (exit %s)" % code)[:200]


def worker():
    while True:
        job = WORK.get()
        try:
            run_job(job)
        except Exception as e:
            job["state"], job["line"] = "error", str(e)[:200]
        job["finished"] = int(time.time())


def add_job(raw_url):
    url, is_playlist = clean_url(raw_url)
    if library_bytes() > MAX_LIBRARY_GB * 1024 ** 3:
        return 409, {"error": "library is full, delete something first"}
    with JOBS_LOCK:
        pending = sum(1 for j in JOBS if j["state"] in ("queued", "running"))
        if pending >= MAX_QUEUE:
            return 429, {"error": "too many downloads queued"}
        job = {"id": NEXT_ID[0], "url": url, "playlist": is_playlist,
               "state": "queued", "line": "queued", "created": int(time.time())}
        NEXT_ID[0] += 1
        JOBS.append(job)
        del JOBS[:-30]
    WORK.put(job)
    return 200, {"id": job["id"]}


# ---------- http ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "radio-private"

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
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, {"ok": True})
        elif path == "/jobs":
            with JOBS_LOCK:
                jobs = [{k: j.get(k) for k in ("id", "url", "state", "line")} for j in JOBS[-8:]]
            self._send(200, {"jobs": jobs[::-1]})
        elif path == "/library":
            self._send(200, {"files": lib_files(), "bytes": library_bytes(),
                             "limit_gb": MAX_LIBRARY_GB, "shared": shared_names()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        # Browser-side CSRF guard: JSON content type (forces a CORS preflight that we never
        # answer) and, where the browser sends it, a same-origin request.
        site = self.headers.get("Sec-Fetch-Site")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if site not in (None, "same-origin") or ctype != "application/json":
            self._send(403, {"error": "forbidden"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > 8192:
            self._send(413, {"error": "too large"})
            return
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(data, dict):
                raise ValueError
        except Exception:
            self._send(400, {"error": "bad request"})
            return

        path = urlparse(self.path).path
        if path == "/add":
            try:
                code, payload = add_job(data.get("url"))
            except ValueError as e:
                code, payload = 400, {"error": str(e)}
            self._send(code, payload)
        elif path == "/share":
            p = lib_path(data.get("name"))
            if not p:
                self._send(404, {"error": "no such file"})
                return
            if "\n" in p or "\r" in p:
                self._send(400, {"error": "bad file name"})
                return
            write_shared([p])
            if data.get("now") is True:
                threading.Timer(SKIP_DELAY, trigger_skip).start()
            self._send(200, {"ok": True, "now": data.get("now") is True})
        elif path == "/unshare":
            write_shared([])
            self._send(200, {"ok": True})
        elif path == "/delete":
            name = data.get("name")
            p = lib_path(name)
            if not p:
                self._send(404, {"error": "no such file"})
                return
            try:
                if name in shared_names():
                    write_shared([])
                os.remove(p)
                META.pop(name, None)
            except OSError as e:
                self._send(500, {"error": str(e)})
                return
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})


def main():
    os.makedirs(LIB_DIR, exist_ok=True)
    if not os.path.exists(SHARED_M3U):
        open(SHARED_M3U, "wb").close()
        os.chmod(SHARED_M3U, 0o644)
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
