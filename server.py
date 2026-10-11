#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Filmothek – eine einfache, übersichtliche Filmbibliothek im Browser.

Startet einen kleinen Server auf http://localhost:8642, liest einen Filmordner
(inklusive aller Unterordner) ein, lädt Infos und Cover von TMDB herunter und
spielt Filme mit dem installierten VLC ab.

Benötigt nur Python 3.8 oder neuer – keine Zusatzpakete.
Aufruf:  python3 server.py            (nur Server)
         python3 server.py --browser  (Server + Browser öffnen)
"""
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, "web")
DATA_DIR = os.environ.get("FILMOTHEK_DATA") or os.path.join(APP_DIR, "daten")
IMG_DIR = os.path.join(DATA_DIR, "bilder")
CONFIG_FILE = os.path.join(DATA_DIR, "einstellungen.json")
LIBRARY_FILE = os.path.join(DATA_DIR, "bibliothek.json")

HOST = "127.0.0.1"
PORT = int(os.environ.get("FILMOTHEK_PORT", "8642"))
TMDB_BASE = os.environ.get("FILMOTHEK_TMDB_BASE", "https://api.themoviedb.org/3")
TMDB_IMG = os.environ.get("FILMOTHEK_IMG_BASE", "https://image.tmdb.org/t/p")

VIDEO_EXT = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".mpg", ".mpeg",
    ".ts", ".m2ts", ".iso", ".divx", ".webm", ".flv", ".ogm",
}
SKIP_DIRS = {
    "extras", "featurettes", "trailer", "trailers", "behind the scenes",
    "deleted scenes", "interviews", "sample", "samples", "$recycle.bin",
    "system volume information", "lost+found",
}
JUNK_WORDS = (
    r"2160p|1080p|1080i|720p|576p|480p|4k|uhd|hdr10\+?|hdr|dv|dolby\s?vision|"
    r"blu-?ray|bdrip|brrip|bd|dvdrip|dvd9|dvd5|dvd|webrip|web-?dl|web|hdtv|tvrip|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|xvid|divx|aac|ac3|eac3|dts(-hd)?|truehd|atmos|"
    r"dd5\.?1|dd2\.?0|5\.1|7\.1|remux|extended|unrated|uncut|directors\s?cut|"
    r"remastered|german|deutsch|english|dl|ml|multi|subbed|proper|repack|internal|limited"
)
YEAR_RE = re.compile(r"(?<!\d)(19[0-9]{2}|20[0-9]{2})(?!\d)")
JUNK_RE = re.compile(r"(?<![a-z0-9])(" + JUNK_WORDS + r")(?![a-z0-9])", re.I)
GENERIC_RE = re.compile(r"^(movie|film|video|main|feature|title\s*\d*|vts_\d+.*)$", re.I)

lock = threading.RLock()
library = {"films": {}, "scanned_at": None}
scan_state = {
    "running": False, "phase": "", "done": 0, "total": 0,
    "message": "", "error": None, "finished_at": None,
}


class ScanError(Exception):
    pass


# --------------------------------------------------------------------------
# Dateien & Einstellungen
# --------------------------------------------------------------------------
def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_config():
    cfg = {"library_path": "", "tmdb_key": ""}
    cfg.update(read_json(CONFIG_FILE, {}))
    return cfg


def save_library():
    with lock:
        write_json(LIBRARY_FILE, library)


def norm(text):
    text = unicodedata.normalize("NFD", (text or "").lower().replace("ß", "ss"))
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def make_id(rel):
    return hashlib.sha1(rel.replace(os.sep, "/").lower().encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------
# Titel & Jahr aus dem Dateinamen erraten
# --------------------------------------------------------------------------
def parse_name(stem):
    s = stem.replace("_", " ")
    s = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", s)       # Punkte als Trenner (aber 5.1 bleibt)
    title, year = s, None
    for m in reversed(list(YEAR_RE.finditer(s))):
        if m.start() > 0:                              # "1917 (2019)" -> Titel 1917
            year, title = int(m.group(1)), s[:m.start()]
            break
    j = JUNK_RE.search(title)
    if j and j.start() > 0:
        title = title[:j.start()]
    title = re.sub(r"[\[\(\{][^\]\)\}]*[\]\)\}]?", " ", title)
    title = re.sub(r"[\s\-\(\[\{,]+$", "", title)
    title = re.sub(r"\s+", " ", title).strip(" -")
    return (title or stem.strip()), year


def guess(rel):
    stem = os.path.splitext(os.path.basename(rel))[0]
    parent = os.path.basename(os.path.dirname(rel))
    title, year = parse_name(stem)
    if parent:
        ptitle, pyear = parse_name(parent)
        if GENERIC_RE.match(title):
            return ptitle, pyear
        if not year and pyear and norm(title) and norm(title) in norm(ptitle):
            year = pyear                                 # "Inception (2010)/Inception.mkv"
    return title, year


# --------------------------------------------------------------------------
# Internet (TMDB)
# --------------------------------------------------------------------------
def _ssl_context():
    try:
        import certifi  # optional
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CTX = _ssl_context()


def http_get(url, headers=None, timeout=25):
    headers = {"User-Agent": "Filmothek/1.0", **(headers or {})}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return resp.read()
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, ssl.SSLError) as exc:
        # Python von python.org auf dem Mac hat oft keine Zertifikate – dann curl nehmen
        if "CERTIFICATE" in str(exc).upper() and shutil.which("curl"):
            cmd = ["curl", "-sfL", "--max-time", str(timeout)]
            for k, v in headers.items():
                cmd += ["-H", f"{k}: {v}"]
            out = subprocess.run(cmd + [url], capture_output=True)
            if out.returncode == 0:
                return out.stdout
            if out.returncode == 22:
                raise urllib.error.HTTPError(url, 401, "HTTP-Fehler", None, None)
        raise


class TMDB:
    def __init__(self, key):
        self.key = key.strip()

    def get(self, path, **params):
        params.setdefault("language", "de-DE")
        headers = {"Accept": "application/json"}
        if self.key.startswith("eyJ"):                   # Lese-Token (v4)
            headers["Authorization"] = "Bearer " + self.key
        else:                                            # API-Schlüssel (v3)
            params["api_key"] = self.key
        url = f"{TMDB_BASE}{path}?{urllib.parse.urlencode(params)}"
        return json.loads(http_get(url, headers))

    def check(self):
        try:
            self.get("/configuration")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise ScanError("Der TMDB-Schlüssel wird nicht akzeptiert. Bitte in den Einstellungen prüfen.")
            raise ScanError(f"TMDB antwortet nicht richtig (Fehler {exc.code}).")
        except Exception:
            raise ScanError("Keine Verbindung zum Internet bzw. zu TMDB. Filme werden ohne neue Infos angezeigt.")

    def search(self, query, year=None):
        results = []
        if year:
            results = self.get("/search/movie", query=query, year=year).get("results", [])
        if not results:
            results = self.get("/search/movie", query=query).get("results", [])
        return results


def find_tmdb_id(tmdb, title, year):
    results = tmdb.search(title, year)
    if not results:
        return None
    if year:
        for r in results[:6]:
            if (r.get("release_date") or "")[:4] == str(year):
                return r["id"]
    return results[0]["id"]


def download_image(path, size, prefix, tid):
    if not path:
        return None
    name = f"{prefix}{tid}.jpg"
    dest = os.path.join(IMG_DIR, name)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return name
    data = http_get(f"{TMDB_IMG}/{size}{path}")
    with open(dest + ".part", "wb") as fh:
        fh.write(data)
    os.replace(dest + ".part", dest)
    return name


def fetch_meta(tmdb, tid):
    d = tmdb.get(f"/movie/{tid}", append_to_response="credits,keywords,release_dates")
    overview = d.get("overview") or ""
    if not overview:
        try:
            overview = tmdb.get(f"/movie/{tid}", language="en-US").get("overview") or ""
        except Exception:
            pass
    fsk = None
    for country in (d.get("release_dates") or {}).get("results", []):
        if country.get("iso_3166_1") == "DE":
            for rel in country.get("release_dates", []):
                if rel.get("certification"):
                    fsk = rel["certification"]
                    break
    credits = d.get("credits") or {}
    coll = d.get("belongs_to_collection")
    rd = d.get("release_date") or ""
    meta = {
        "tmdb_id": tid,
        "matched": True,
        "title": d.get("title") or d.get("original_title") or "",
        "original_title": d.get("original_title") or "",
        "year": int(rd[:4]) if rd[:4].isdigit() else None,
        "release_date": rd,
        "runtime": d.get("runtime") or None,
        "genres": [g["name"] for g in d.get("genres", [])],
        "overview": overview,
        "tagline": d.get("tagline") or "",
        "directors": [c["name"] for c in credits.get("crew", []) if c.get("job") == "Director"],
        "cast": [{"name": c.get("name", ""), "role": c.get("character") or ""}
                 for c in credits.get("cast", [])[:14]],
        "keywords": [k["name"] for k in (d.get("keywords") or {}).get("keywords", [])],
        "collection": {"id": coll["id"], "name": coll["name"]} if coll else None,
        "fsk": fsk,
        "rating": round(d.get("vote_average") or 0, 1) or None,
        "poster_path": d.get("poster_path"),
        "backdrop_path": d.get("backdrop_path"),
    }
    for key, size, prefix in (("poster", "w500", "p"), ("backdrop", "w1280", "b")):
        try:
            meta[key] = download_image(meta[key + "_path"], size, prefix, tid)
        except Exception:
            meta[key] = None
    return meta


def needs_work(entry):
    if not entry.get("matched"):
        return True
    for key in ("poster", "backdrop"):
        if entry.get(key + "_path") and not (
                entry.get(key) and os.path.exists(os.path.join(IMG_DIR, entry[key]))):
            return True
    return False


def enrich(entry, tmdb):
    try:
        tid = entry.get("tmdb_id") or find_tmdb_id(tmdb, entry["guess_title"], entry.get("guess_year"))
        if not tid:
            return
        meta = fetch_meta(tmdb, tid)
        with lock:
            entry.update(meta)
            entry.pop("error", None)
    except Exception as exc:
        with lock:
            entry["error"] = str(exc)


# --------------------------------------------------------------------------
# Bibliothek einlesen
# --------------------------------------------------------------------------
def set_state(**kw):
    with lock:
        scan_state.update(kw)


def run_scan():
    cfg = load_config()
    root = (cfg.get("library_path") or "").strip()
    set_state(running=True, phase="Ordner wird durchsucht …", done=0, total=0, message="", error=None)
    try:
        if not root:
            raise ScanError("Es ist noch kein Filmordner eingestellt. Bitte in den Einstellungen auswählen.")
        if not os.path.isdir(root):
            raise ScanError(f"Der Filmordner wurde nicht gefunden:\n{root}\n\nIst die externe Festplatte angesteckt?")

        found = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in SKIP_DIRS]
            for fn in filenames:
                if fn.startswith(".") or os.path.splitext(fn)[1].lower() not in VIDEO_EXT:
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if "sample" in fn.lower() and st.st_size < 300 * 1024 * 1024:
                    continue
                found.append((os.path.relpath(full, root), st))
            set_state(message=f"{len(found)} Filme gefunden")

        with lock:
            old = dict(library["films"])
        new = {}
        for rel, st in found:
            fid = make_id(rel)
            entry = old.get(fid)
            if entry is None:
                title, year = guess(rel)
                entry = {"id": fid, "title": title, "year": year, "guess_title": title,
                         "guess_year": year, "matched": False, "added": st.st_mtime}
            entry["rel"] = rel.replace(os.sep, "/")
            entry["file"] = os.path.basename(rel)
            entry["size"] = st.st_size
            new[fid] = entry
        with lock:
            library["films"] = new
            library["scanned_at"] = time.time()
        save_library()

        warning = ""
        key = (cfg.get("tmdb_key") or "").strip()
        todo = [e for e in new.values() if needs_work(e)]
        if key and todo:
            tmdb = TMDB(key)
            try:
                tmdb.check()
            except ScanError as exc:
                warning = str(exc)
                todo = []
            set_state(phase="Filminfos und Cover werden geladen …", total=len(todo), done=0)
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(enrich, e, tmdb) for e in todo]
                for i, _ in enumerate(as_completed(futures), 1):
                    set_state(done=i)
                    if i % 25 == 0:
                        save_library()
            save_library()
        elif not key:
            warning = "Ohne TMDB-Schlüssel werden keine Cover und Infos geladen (siehe Einstellungen)."

        missing = sum(1 for e in new.values() if not e.get("matched"))
        msg = f"Fertig: {len(new)} Filme in der Bibliothek."
        if key and missing:
            msg += f" Bei {missing} Film(en) wurden keine Infos gefunden."
        set_state(message=msg, error=warning or None)
    except ScanError as exc:
        set_state(error=str(exc), message="")
    except Exception as exc:  # pragma: no cover
        set_state(error=f"Unerwarteter Fehler: {exc}", message="")
    finally:
        set_state(running=False, phase="", finished_at=time.time())


def start_scan():
    with lock:
        if scan_state["running"]:
            return False
        scan_state["running"] = True
    threading.Thread(target=run_scan, daemon=True).start()
    return True


# --------------------------------------------------------------------------
# Abspielen & Mac-Helfer
# --------------------------------------------------------------------------
def find_vlc_windows():
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            exe = os.path.join(base, "VideoLAN", "VLC", "vlc.exe")
            if os.path.exists(exe):
                return exe
    return None


def play_file(path):
    if sys.platform == "darwin":
        r = subprocess.run(["open", "-a", "VLC", path], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("VLC konnte nicht gestartet werden. Ist VLC im Programme-Ordner installiert?")
    elif sys.platform.startswith("win"):
        exe = find_vlc_windows()
        if exe:
            subprocess.Popen([exe, path])
        else:
            os.startfile(path)  # type: ignore[attr-defined]
    else:
        prog = shutil.which("vlc") or shutil.which("xdg-open")
        if not prog:
            raise RuntimeError("VLC wurde nicht gefunden.")
        subprocess.Popen([prog, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def choose_folder_mac():
    script = ('activate\n'
              'POSIX path of (choose folder with prompt "Bitte den Ordner mit den Filmen auswählen:")')
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def list_volumes():
    if sys.platform != "darwin" or not os.path.isdir("/Volumes"):
        return []
    vols = []
    for name in sorted(os.listdir("/Volumes")):
        p = os.path.join("/Volumes", name)
        if os.path.isdir(p) and not os.path.islink(p):
            vols.append({"name": name, "path": p})
    return vols


# --------------------------------------------------------------------------
# Webserver
# --------------------------------------------------------------------------
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
                 ".jpg": "image/jpeg", ".png": "image/png", ".ico": "image/x-icon",
                 ".webmanifest": "application/manifest+json"}
ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}", f"[::1]:{PORT}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "Filmothek/1.0"

    def log_message(self, *args):
        pass

    # ---- Hilfen
    def send_json(self, obj, status=200):
        with lock:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, msg, status=400):
        self.send_json({"error": msg}, status)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def host_ok(self):
        return (self.headers.get("Host") or "") in ALLOWED_HOSTS

    def origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urllib.parse.urlparse(origin).netloc in ALLOWED_HOSTS

    def serve_file(self, base, rel, cache):
        base = os.path.realpath(base)
        full = os.path.realpath(os.path.join(base, rel))
        if not full.startswith(base + os.sep) or not os.path.isfile(full):
            return self.fail("Nicht gefunden", 404)
        with open(full, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(os.path.splitext(full)[1].lower(),
                                                           "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    # ---- GET
    def do_GET(self):
        if not self.host_ok():
            return self.fail("Unbekannter Host", 403)
        url = urllib.parse.urlparse(self.path)
        path, qs = url.path, urllib.parse.parse_qs(url.query)

        if path == "/api/films":
            cfg = load_config()
            root = cfg.get("library_path") or ""
            with lock:
                films = list(library["films"].values())
            return self.send_json({
                "films": films, "scanned_at": library.get("scanned_at"),
                "library_path": root, "drive_ok": bool(root) and os.path.isdir(root),
                "has_key": bool((cfg.get("tmdb_key") or "").strip()),
            })
        if path == "/api/status":
            with lock:
                return self.send_json(dict(scan_state))
        if path == "/api/config":
            cfg = load_config()
            return self.send_json({**cfg, "platform": sys.platform, "volumes": list_volumes()})
        if path == "/api/tmdb-search":
            key = (load_config().get("tmdb_key") or "").strip()
            if not key:
                return self.fail("Bitte zuerst in den Einstellungen einen TMDB-Schlüssel eintragen.")
            q = (qs.get("q") or [""])[0].strip()
            year = (qs.get("year") or [""])[0].strip()
            if not q:
                return self.fail("Bitte einen Titel eingeben.")
            try:
                res = TMDB(key).search(q, int(year) if year.isdigit() else None)
            except Exception:
                return self.fail("Die Suche bei TMDB hat nicht geklappt. Besteht eine Internetverbindung?", 502)
            return self.send_json({"results": [{
                "id": r["id"], "title": r.get("title"), "original_title": r.get("original_title"),
                "year": (r.get("release_date") or "")[:4],
                "thumb": f"{TMDB_IMG}/w92{r['poster_path']}" if r.get("poster_path") else None,
            } for r in res[:8]]})
        if path.startswith("/bilder/"):
            return self.serve_file(IMG_DIR, os.path.basename(path), "public, max-age=604800")
        if path in ("/", "/index.html"):
            return self.serve_file(WEB_DIR, "index.html", "no-cache")
        return self.serve_file(WEB_DIR, path.lstrip("/"), "no-cache")

    # ---- POST
    def do_POST(self):
        if not self.host_ok() or not self.origin_ok():
            return self.fail("Nicht erlaubt", 403)
        path = urllib.parse.urlparse(self.path).path
        data = self.body_json()

        if path == "/api/scan":
            start_scan()
            return self.send_json({"ok": True})

        if path == "/api/config":
            cfg = load_config()
            if "library_path" in data:
                cfg["library_path"] = os.path.expanduser(str(data["library_path"]).strip())
            if "tmdb_key" in data:
                cfg["tmdb_key"] = str(data["tmdb_key"]).strip()
            write_json(CONFIG_FILE, cfg)
            return self.send_json({"ok": True, "folder_exists": os.path.isdir(cfg["library_path"])})

        if path == "/api/choose-folder":
            if sys.platform != "darwin":
                return self.fail("Die Ordnerauswahl gibt es nur auf dem Mac.")
            return self.send_json({"path": choose_folder_mac()})

        if path == "/api/play":
            with lock:
                film = library["films"].get(str(data.get("id")))
            if not film:
                return self.fail("Dieser Film ist nicht in der Bibliothek.", 404)
            root = load_config().get("library_path") or ""
            full = os.path.join(root, *film["rel"].split("/"))
            if not os.path.exists(full):
                if not os.path.isdir(root):
                    return self.fail("Die Festplatte mit den Filmen ist nicht angesteckt. "
                                     "Bitte anstecken und nochmal probieren.", 404)
                return self.fail("Die Filmdatei wurde nicht gefunden. Vielleicht wurde sie verschoben? "
                                 "Bitte die Bibliothek aktualisieren.", 404)
            try:
                play_file(full)
            except Exception as exc:
                return self.fail(str(exc), 500)
            with lock:
                film["last_played"] = time.time()
            save_library()
            return self.send_json({"ok": True})

        if path == "/api/assign":
            key = (load_config().get("tmdb_key") or "").strip()
            with lock:
                film = library["films"].get(str(data.get("id")))
            if not film or not key:
                return self.fail("Zuordnung nicht möglich.")
            try:
                meta = fetch_meta(TMDB(key), int(data.get("tmdb_id")))
            except Exception:
                return self.fail("Die Infos konnten nicht geladen werden.", 502)
            with lock:
                film.update(meta)
                film.pop("error", None)
            save_library()
            return self.send_json({"film": film})

        return self.fail("Unbekannte Aktion", 404)


def main():
    os.makedirs(IMG_DIR, exist_ok=True)
    library.update(read_json(LIBRARY_FILE, {"films": {}, "scanned_at": None}))
    url = f"http://localhost:{PORT}/"
    open_browser = "--browser" in sys.argv
    try:
        httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"Filmothek läuft schon: {url}")
        if open_browser:
            webbrowser.open(url)
        return
    print(f"Filmothek läuft auf {url}  (Beenden mit Ctrl+C)")
    root = load_config().get("library_path") or ""
    if root and os.path.isdir(root):
        start_scan()                                   # neue Filme beim Start automatisch finden
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
