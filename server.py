#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Filmothek – eine einfache, übersichtliche Film- und Serienbibliothek im Browser.

Startet einen kleinen Server (nur auf diesem Mac erreichbar), liest einen
Filmordner samt Unterordnern ein, erkennt Filme und Serienfolgen, lädt Infos
und Bilder von TMDB und spielt alles mit dem installierten VLC ab.

Benötigt nur Python 3.8 oder neuer – keine Zusatzpakete.
Aufruf:  python3 server.py            (nur Server)
         python3 server.py --browser  (Server + Browser öffnen)
         python3 server.py --url      (nur die Adresse anzeigen)

Mehrere Benutzerkonten: Jedes Konto bekommt automatisch einen eigenen Port
(VLC soll ja im richtigen Konto aufgehen). Bibliothek, Einstellungen und Bilder
werden gemeinsam genutzt, „gesehen“-Markierungen sind pro Benutzer.
"""
import difflib
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
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, "web")
DATA_DIR = os.environ.get("FILMOTHEK_DATA") or os.path.join(APP_DIR, "daten")
IMG_DIR = os.path.join(DATA_DIR, "bilder")
CONFIG_FILE = os.path.join(DATA_DIR, "einstellungen.json")
LIBRARY_FILE = os.path.join(DATA_DIR, "bibliothek.json")


def _user_dir():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Filmothek")
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Filmothek")
    return os.path.expanduser("~/.local/share/filmothek")


def _default_port():
    try:
        uid = os.getuid()
    except AttributeError:
        return 8642
    return 8642 + (uid - 501 if 501 <= uid < 701 else 0)   # 1. Konto 8642, 2. Konto 8643 …


USER_DIR = os.environ.get("FILMOTHEK_USERDATA") or _user_dir()
PLAYED_FILE = os.path.join(USER_DIR, "gesehen.json")

HOST = "127.0.0.1"
PORT = int(os.environ.get("FILMOTHEK_PORT") or _default_port())
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
DASH_SPLIT = re.compile(r"\s+[-–]\s+")

# ---- Serien-Erkennung
SE_RES = [
    re.compile(r"(?<![a-z0-9])s(\d{1,2})\s?[._-]?\s?e(\d{1,3})(?:[ ._-]?e?\d{1,3})?(?!\d)", re.I),  # S01E02
    re.compile(r"(?<![a-z0-9])(\d{1,2})x(\d{2,3})(?!\d)", re.I),                                      # 1x02
    re.compile(r"(?:staffel|season)\s*0*(\d{1,2})\W{0,3}(?:folge|episode|ep\.?)\s*0*(\d{1,3})(?!\d)", re.I),
]
EP_ONLY_RES = [
    re.compile(r"(?<![a-z])(?:folge|episode|ep\.)\s*0*(\d{1,3})(?!\d)", re.I),   # Folge 12
    re.compile(r"(?<![a-z0-9])e(\d{2,3})(?!\d)", re.I),                         # E05
]
SEASON_DIR_RE = re.compile(r"(?:^|[\s._-])(?:staffel|season|series)[\s._-]*0*(\d{1,2})(?!\d)|^s(\d{1,2})$", re.I)
SHOW_ROOTS = {"serien", "serie", "tv serien", "tv", "series", "tv shows", "tvshows", "fernsehserien", "shows"}
PLACEHOLDER_EP = re.compile(r"^(folge|episode)\s*\d+$", re.I)

lock = threading.RLock()
library = {"films": {}, "shows": {}, "scanned_at": None}
lib_mtime = [0.0]
played = {}
scan_state = {
    "running": False, "phase": "", "done": 0, "total": 0,
    "message": "", "error": None, "finished_at": None,
}


class ScanError(Exception):
    pass


# --------------------------------------------------------------------------
# Dateien, Rechte & Einstellungen
# --------------------------------------------------------------------------
def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _staff_gid():
    if sys.platform != "darwin":
        return None
    try:
        import grp
        return grp.getgrnam("staff").gr_gid
    except Exception:
        return None


def share_path(path, is_dir):
    """Gemeinsam genutzte Dateien für alle Benutzer (Gruppe „staff“) beschreibbar machen."""
    if os.name != "posix":
        return
    try:
        st = os.lstat(path)
        if st.st_uid != os.getuid():
            return
        gid = _staff_gid()
        if gid is not None and st.st_gid != gid:
            os.chown(path, -1, gid)
        want = 0o2775 if is_dir else (st.st_mode & 0o777) | 0o664
        if (st.st_mode & 0o7777) != want:
            os.chmod(path, want)
    except OSError:
        pass


def ensure_dirs():
    global DATA_DIR, IMG_DIR, CONFIG_FILE, LIBRARY_FILE
    os.makedirs(USER_DIR, exist_ok=True)
    try:
        for d in (DATA_DIR, IMG_DIR):
            os.makedirs(d, exist_ok=True)
            share_path(d, True)
        if not os.access(DATA_DIR, os.W_OK):
            raise PermissionError(DATA_DIR)
    except OSError:
        # Gemeinsamer Datenordner nicht beschreibbar -> eigene Bibliothek für dieses Konto
        print(f"Hinweis: {DATA_DIR} ist nicht beschreibbar – verwende {USER_DIR}/daten", file=sys.stderr)
        DATA_DIR = os.path.join(USER_DIR, "daten")
        IMG_DIR = os.path.join(DATA_DIR, "bilder")
        CONFIG_FILE = os.path.join(DATA_DIR, "einstellungen.json")
        LIBRARY_FILE = os.path.join(DATA_DIR, "bibliothek.json")
        os.makedirs(IMG_DIR, exist_ok=True)


def fix_permissions():
    for dirpath, dirnames, filenames in os.walk(DATA_DIR):
        for n in dirnames:
            share_path(os.path.join(dirpath, n), True)
        for n in filenames:
            share_path(os.path.join(dirpath, n), False)


def load_config():
    cfg = {"library_path": "", "tmdb_key": ""}
    cfg.update(read_json(CONFIG_FILE, {}))
    return cfg


def load_library():
    data = read_json(LIBRARY_FILE, None)
    if not isinstance(data, dict):
        return
    data.setdefault("films", {})
    data.setdefault("shows", {})
    with lock:
        library.clear()
        library.update(data)
        try:
            lib_mtime[0] = os.path.getmtime(LIBRARY_FILE)
        except OSError:
            pass


def maybe_reload_library():
    """Hat das andere Benutzerkonto neu eingelesen? Dann die neue Liste übernehmen."""
    if scan_state["running"]:
        return
    try:
        m = os.path.getmtime(LIBRARY_FILE)
    except OSError:
        return
    if m > lib_mtime[0] + 0.001:
        load_library()


def save_library():
    with lock:
        write_json(LIBRARY_FILE, library)
        try:
            lib_mtime[0] = os.path.getmtime(LIBRARY_FILE)
        except OSError:
            pass


def save_played():
    with lock:
        write_json(PLAYED_FILE, played)


def norm(text):
    text = unicodedata.normalize("NFD", str(text or "").lower().replace("ß", "ss"))
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def make_id(rel):
    return hashlib.sha1(rel.replace(os.sep, "/").lower().encode("utf-8")).hexdigest()[:12]


def img_ok(name):
    return bool(name) and os.path.exists(os.path.join(IMG_DIR, name))


# --------------------------------------------------------------------------
# Titel & Jahr aus dem Dateinamen erraten
# --------------------------------------------------------------------------
def spaced(s):
    s = s.replace("_", " ")
    return re.sub(r"(?<!\d)\.|\.(?!\d)", " ", s)       # Punkte als Trenner (aber 5.1 bleibt)


def parse_name(stem):
    s = spaced(stem)
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


def clean(text):
    text = spaced(text or "").strip(" -–._,:")
    if not norm(text):
        return ""
    title = parse_name(text)[0]
    j = JUNK_RE.search(title)
    if j:
        title = title[:j.start()].strip(" -–._,:")
    return title


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


def show_key(name):
    return "tv-" + (norm(name).replace(" ", "-") or "unbekannt")


def strip_show_prefix(stem, show):
    s = spaced(stem)
    parts = DASH_SPLIT.split(s, maxsplit=1)
    if len(parts) == 2 and similarity(show, clean(parts[0])) >= 0.8:
        return clean(parts[1])
    if norm(s).startswith(norm(show) + " "):
        words = len(norm(show).split())
        return clean(" ".join(s.split()[words:]))
    return clean(s)


def make_info(show, year, season, episode, ep_title, strong):
    return {"show_key": show_key(show), "show_name": show, "show_year": year,
            "season": season, "episode": episode, "ep_title": ep_title or None, "strong": strong}


def classify(rel):
    """Erkennt Serienfolgen an Mustern wie S01E02, 1x02, „Folge 3“ oder Ordnern wie „Staffel 1“."""
    parts = rel.replace("\\", "/").split("/")
    dirs, stem = parts[:-1], os.path.splitext(parts[-1])[0]
    season = episode = show = show_year = ep_title = None
    strong = False
    season_idx = None

    for i in range(len(dirs) - 1, -1, -1):              # Ordner „Staffel 2“ / „Alf - Season 2“
        m = SEASON_DIR_RE.search(dirs[i])
        if m:
            season, season_idx, strong = int(m.group(1) or m.group(2)), i, True
            prefix = clean(dirs[i][:m.start()])
            if prefix:
                show, show_year = parse_name(dirs[i][:m.start()])
            elif i > 0:
                show, show_year = parse_name(dirs[i - 1])
            break

    if show is None:                                     # Ordner „Serien/Alf/…“
        for i, d in enumerate(dirs):
            if norm(d) in SHOW_ROOTS:
                strong = True
                if i + 1 < len(dirs):
                    show, show_year = parse_name(dirs[i + 1])
                break

    s = spaced(stem)
    m = None
    for rx in SE_RES:
        m = rx.search(s)
        if m:
            season, episode = int(m.group(1)), int(m.group(2))
            break
    if not m:
        for rx in EP_ONLY_RES:
            m = rx.search(s)
            if m:
                episode = int(m.group(1))
                break

    if m:
        strong = True
        if not show:
            show, show_year = (parse_name(s[:m.start()].strip(" -–._")) if clean(s[:m.start()]) else (None, None))
            if show and dirs and not show_year:
                dname, dyear = parse_name(dirs[-1])
                if similarity(show, dname) >= 0.8:
                    show_year = dyear
        if not show and dirs:
            idx = len(dirs) - 1
            if season_idx == idx:
                idx -= 1
            if idx >= 0:
                show, show_year = parse_name(dirs[idx])
        ep_title = clean(s[m.end():])
    elif show:
        ep_title = strip_show_prefix(stem, show)
        if norm(ep_title) == norm(show):
            ep_title = None
        elif ep_title and ep_title.isdigit() and len(ep_title) <= 3:   # „Dicte 3“
            episode, ep_title = int(ep_title), None
    elif strong:                                         # „Serien/Monk - Mr. Monk und …“
        parts = DASH_SPLIT.split(s, maxsplit=1)
        if len(parts) == 2 and clean(parts[0]):
            show, show_year = parse_name(parts[0])
            ep_title = clean(parts[1])

    if not strong or not show:
        return None
    return make_info(show, show_year, season, episode, ep_title, strong)


# --------------------------------------------------------------------------
# Unscharfer Vergleich (Fuzzy-Matching)
# --------------------------------------------------------------------------
def similarity(a, b, contain=False):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    r = difflib.SequenceMatcher(None, a, b).ratio()
    if contain:                                           # Treffer-Titel steckt ganz im Dateinamen
        ta, tb = a.split(), b.split()
        if len(tb) >= 2 and set(tb) <= set(ta):
            r = max(r, 0.8 + 0.1 * len(tb) / len(ta))
    return r


def query_variants(title, drop_words=True):
    out = []

    def add(q):
        q = (q or "").strip(" -–:,")
        if norm(q) and norm(q) not in [norm(x) for x in out]:
            out.append(q)

    add(title)
    parts = [p for p in re.split(r"\s+[-–]\s+|\s*:\s+", title) if norm(p)]
    if len(parts) > 1:
        for p in reversed(parts):                         # „Doris Day - Pillow Talk“ -> „Pillow Talk“
            add(p)
    words = title.split()
    if drop_words and len(words) >= 3:
        add(" ".join(words[1:]))
        add(" ".join(words[:-1]))
        if len(words) >= 4:
            add(" ".join(words[2:]))
            add(" ".join(words[:-2]))
    return out[:7]


def best_match(tmdb, kind, title, year, threshold, min_sim=0.0):
    if kind == "movie":
        path, ykey, fields, dfield = "/search/movie", "year", ("title", "original_title"), "release_date"
    else:
        path, ykey, fields, dfield = "/search/tv", "first_air_date_year", ("name", "original_name"), "first_air_date"
    variants = query_variants(title, drop_words=(kind == "movie"))
    best = (0.0, 0.0, None)
    for vi, q in enumerate(variants):
        results = []
        if year:
            results = tmdb.get(path, query=q, **{ykey: year}).get("results", [])
        if not results:
            results = tmdb.get(path, query=q).get("results", [])
        for rank, r in enumerate(results[:10]):
            sim = 0.0
            for f in fields:
                sim = max(sim, similarity(title, r.get(f), contain=(kind == "movie")))
                if q is not title:
                    sim = max(sim, 0.97 * similarity(q, r.get(f), contain=(kind == "movie")))
            score = sim
            ry = (r.get(dfield) or "")[:4]
            if year and ry.isdigit():
                d = abs(int(ry) - year)
                score += 0.15 if d == 0 else 0.07 if d == 1 else -0.2
            if vi == 0 and rank == 0:
                score += 0.08
            if score > best[0]:
                best = (score, sim, r["id"])
        if best[0] >= 1.05:
            break
    if best[2] and best[0] >= threshold and best[1] >= min_sim:
        return best[2]
    return None


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
    headers = {"User-Agent": "Filmothek/2.0", **(headers or {})}
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
                raise urllib.error.HTTPError(url, 404, "HTTP-Fehler", None, None)
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
        for attempt in range(3):
            try:
                return json.loads(http_get(url, headers))
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 2:      # zu viele Anfragen – kurz warten
                    time.sleep(2 * (attempt + 1))
                    continue
                raise

    def check(self):
        try:
            self.get("/configuration")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise ScanError("Der TMDB-Schlüssel wird nicht akzeptiert. Bitte in den Einstellungen prüfen.")
            raise ScanError(f"TMDB antwortet nicht richtig (Fehler {exc.code}).")
        except Exception:
            raise ScanError("Keine Verbindung zum Internet bzw. zu TMDB. Filme werden ohne neue Infos angezeigt.")

    def search(self, kind, query, year=None):
        path = "/search/movie" if kind == "movie" else "/search/tv"
        ykey = "year" if kind == "movie" else "first_air_date_year"
        results = []
        if year:
            results = self.get(path, query=query, **{ykey: year}).get("results", [])
        if not results:
            results = self.get(path, query=query).get("results", [])
        return results


def download_image(path, size, name):
    if not path:
        return None
    dest = os.path.join(IMG_DIR, name)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return name
    data = http_get(f"{TMDB_IMG}/{size}{path}")
    part = f"{dest}.{os.getpid()}.part"
    with open(part, "wb") as fh:
        fh.write(data)
    os.replace(part, dest)
    return name


def _overview_fallback(tmdb, path, overview):
    if overview:
        return overview
    try:
        return tmdb.get(path, language="en-US").get("overview") or ""
    except Exception:
        return ""


def fetch_meta(tmdb, tid):
    d = tmdb.get(f"/movie/{tid}", append_to_response="credits,keywords,release_dates")
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
        "overview": _overview_fallback(tmdb, f"/movie/{tid}", d.get("overview")),
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
            meta[key] = download_image(meta[key + "_path"], size, f"{prefix}{tid}.jpg")
        except Exception:
            meta[key] = None
    return meta


def fetch_show_meta(tmdb, tid):
    d = tmdb.get(f"/tv/{tid}", append_to_response="aggregate_credits,keywords,content_ratings")
    fsk = None
    for r in (d.get("content_ratings") or {}).get("results", []):
        if r.get("iso_3166_1") == "DE" and r.get("rating"):
            fsk = r["rating"]
    cast = []
    for c in (d.get("aggregate_credits") or {}).get("cast", [])[:14]:
        roles = c.get("roles") or [{}]
        cast.append({"name": c.get("name", ""), "role": roles[0].get("character") or ""})
    first, last = d.get("first_air_date") or "", d.get("last_air_date") or ""
    run = d.get("episode_run_time") or []
    meta = {
        "tmdb_id": tid,
        "matched": True,
        "title": d.get("name") or d.get("original_name") or "",
        "original_title": d.get("original_name") or "",
        "year": int(first[:4]) if first[:4].isdigit() else None,
        "last_year": int(last[:4]) if last[:4].isdigit() else None,
        "ended": d.get("status") in ("Ended", "Canceled"),
        "genres": [g["name"] for g in d.get("genres", [])],
        "overview": _overview_fallback(tmdb, f"/tv/{tid}", d.get("overview")),
        "tagline": d.get("tagline") or "",
        "creators": [c.get("name", "") for c in d.get("created_by", [])],
        "cast": cast,
        "keywords": [k["name"] for k in (d.get("keywords") or {}).get("results", [])],
        "fsk": fsk,
        "rating": round(d.get("vote_average") or 0, 1) or None,
        "runtime": run[0] if run else None,
        "number_of_seasons": d.get("number_of_seasons"),
        "number_of_episodes": d.get("number_of_episodes"),
        "season_list": [[s["season_number"], s.get("episode_count") or 0] for s in d.get("seasons", [])],
        "poster_path": d.get("poster_path"),
        "backdrop_path": d.get("backdrop_path"),
    }
    for key, size, prefix in (("poster", "w500", "tp"), ("backdrop", "w1280", "tb")):
        try:
            meta[key] = download_image(meta[key + "_path"], size, f"{prefix}{tid}.jpg")
        except Exception:
            meta[key] = None
    return meta


def needs_work(entry):
    if not entry.get("matched"):
        return True
    for key in ("poster", "backdrop"):
        if entry.get(key + "_path") and not img_ok(entry.get(key)):
            return True
    return False


def enrich(entry, tmdb):
    try:
        tid = entry.get("tmdb_id") or best_match(
            tmdb, "movie", entry["guess_title"], entry.get("guess_year"), threshold=0.62)
        if not tid:
            return
        meta = fetch_meta(tmdb, tid)
        with lock:
            entry.update(meta)
            entry.pop("error", None)
    except Exception as exc:
        with lock:
            entry["error"] = str(exc)


# ---- Serien
EP_FIELDS = ("ep_name", "ep_overview", "air_date", "still_path", "still", "ep_matched", "ep_checked")


def reset_episode(e):
    for k in EP_FIELDS:
        e.pop(k, None)
    e["season"], e["episode"] = e.get("c_season"), e.get("c_episode")


def load_season(tmdb, sh, s):
    if s in sh.setdefault("fetched", []):
        return
    try:
        d = tmdb.get(f"/tv/{sh['tmdb_id']}/season/{s}")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        d = {}
    with lock:
        eps = sh.setdefault("eps", {})
        for ep in d.get("episodes", []):
            eps[f"{s}x{ep.get('episode_number')}"] = {
                "name": ep.get("name") or "", "overview": ep.get("overview") or "",
                "still_path": ep.get("still_path"), "air_date": ep.get("air_date") or "",
                "runtime": ep.get("runtime") or None,
            }
        sh["fetched"].append(s)


def show_needs_work(sh, eps):
    if sh.get("rejected"):
        return False
    if not sh.get("matched"):
        return True
    if sh.get("poster_path") and not img_ok(sh.get("poster")):
        return True
    for e in eps:
        if not e.get("ep_checked") or (e.get("still_path") and not img_ok(e.get("still"))):
            return True
    return False


def attach_episodes(tmdb, sh, eps):
    tid = sh.get("tmdb_id")
    if not tid:
        return
    seasons = sorted(n for n, _ in sh.get("season_list") or [])
    regular = [n for n in seasons if n > 0]
    for e in eps:
        if e.get("ep_matched"):
            if e.get("still_path") and not img_ok(e.get("still")):
                try:
                    e["still"] = download_image(e["still_path"], "w300", f"te{tid}_{e['season']}_{e['episode']}.jpg")
                except Exception:
                    pass
            continue
        if e.get("ep_checked"):
            continue
        s, n = e.get("c_season"), e.get("c_episode")
        if n is not None and s is None:
            if len(regular) == 1:
                s = regular[0]
            else:                                        # durchgehende Nummerierung über alle Staffeln
                rest = n
                for num, cnt in sorted(sh.get("season_list") or []):
                    if num == 0 or not cnt:
                        continue
                    if rest <= cnt:
                        s, n = num, rest
                        break
                    rest -= cnt
        ep = None
        if s is not None and n is not None:
            load_season(tmdb, sh, s)
            ep = sh.get("eps", {}).get(f"{s}x{n}")
        guess_title = e.get("ep_title_guess")
        if ep is None and guess_title:                   # Folge anhand des Titels suchen
            for x in ([s] if s is not None else seasons):
                load_season(tmdb, sh, x)
            best, best_key = 0.0, None
            for k, v in sh.get("eps", {}).items():
                if s is not None and not k.startswith(f"{s}x"):
                    continue
                sim = similarity(guess_title, v["name"])
                if sim > best:
                    best, best_key = sim, k
            if best >= 0.6:
                ep = sh["eps"][best_key]
                s, n = (int(x) for x in best_key.split("x"))
        with lock:
            e["season"], e["episode"] = s, n
            e["ep_checked"] = True
            if ep:
                name = ep["name"]
                if PLACEHOLDER_EP.match(name) and guess_title:
                    name = guess_title
                e.update(ep_name=name, ep_overview=ep["overview"], air_date=ep["air_date"],
                         runtime=ep["runtime"] or sh.get("runtime"), still_path=ep["still_path"],
                         ep_matched=True, title=name or e.get("title"))
        if ep and ep.get("still_path"):
            try:
                still = download_image(ep["still_path"], "w300", f"te{tid}_{s}_{n}.jpg")
                with lock:
                    e["still"] = still
            except Exception:
                pass


def enrich_show(sh, eps, tmdb):
    try:
        if not sh.get("matched"):
            strong = sh.get("strong", True)
            tid = sh.get("tmdb_id") or best_match(
                tmdb, "tv", sh["guess_name"], sh.get("guess_year"),
                threshold=0.78 if strong else 0.85, min_sim=0.0 if strong else 0.85)
            if not tid:
                if not strong:
                    with lock:
                        sh["rejected"] = True            # doch keine Serie (z. B. „Doris Day - …“)
                return
            meta = fetch_show_meta(tmdb, tid)
            with lock:
                sh.update(meta)
                sh.update(eps={}, fetched=[])
                sh.pop("error", None)
                for e in eps:
                    reset_episode(e)
        elif sh.get("poster_path") and not img_ok(sh.get("poster")):
            sh["poster"] = download_image(sh["poster_path"], "w500", f"tp{sh['tmdb_id']}.jpg")
        attach_episodes(tmdb, sh, eps)
    except Exception as exc:
        with lock:
            sh["error"] = str(exc)


# --------------------------------------------------------------------------
# Bibliothek einlesen
# --------------------------------------------------------------------------
def set_state(**kw):
    with lock:
        scan_state.update(kw)


def new_movie_entry(fid, rel, added):
    title, year = guess(rel)
    return {"id": fid, "kind": "movie", "title": title, "year": year, "guess_title": title,
            "guess_year": year, "matched": False, "added": added}


def classify_all(found, old_shows):
    infos = {rel: classify(rel) for rel, _ in found}

    # Übrige Videos in einem Ordner mit sicher erkannten Folgen gehören zur selben Serie
    dir_shows = defaultdict(dict)
    for rel, info in infos.items():
        if info and info["strong"]:
            dir_shows[os.path.dirname(rel)][info["show_key"]] = info
    for rel, info in list(infos.items()):
        if info is None:
            ks = dir_shows.get(os.path.dirname(rel))
            if ks and len(ks) == 1:
                ref = next(iter(ks.values()))
                stem = os.path.splitext(os.path.basename(rel))[0]
                infos[rel] = make_info(ref["show_name"], ref["show_year"], ref["season"], None,
                                       strip_show_prefix(stem, ref["show_name"]), True)

    # Mindestens 3 Dateien „Name - Titel“ im selben Ordner: vielleicht eine Serie (wird bei TMDB geprüft)
    groups = defaultdict(list)
    for rel, info in infos.items():
        if info is None:
            parts = DASH_SPLIT.split(spaced(os.path.splitext(os.path.basename(rel))[0]), maxsplit=1)
            if len(parts) == 2 and norm(parts[0]):
                name, year = parse_name(parts[0])
                groups[(os.path.dirname(rel), norm(name))].append((rel, name, year, parts[1]))
    for items in groups.values():
        if len(items) < 3:
            continue
        key = show_key(items[0][1])
        if (old_shows.get(key) or {}).get("rejected"):
            continue
        for rel, name, year, rest in items:
            infos[rel] = make_info(name, year, None, None, clean(rest), False)
    return infos


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
                found.append((os.path.relpath(full, root).replace(os.sep, "/"), st))
            set_state(message=f"{len(found)} Dateien gefunden")

        with lock:
            old = dict(library.get("films", {}))
            old_shows = dict(library.get("shows", {}))
        infos = classify_all(found, old_shows)

        new, shows = {}, {}
        for rel, st in found:
            fid = make_id(rel)
            info = infos[rel]
            kind = "episode" if info else "movie"
            sig = [info["show_key"], info["season"], info["episode"], info["ep_title"]] if info else None
            entry = old.get(fid)
            if entry is not None and (entry.get("kind", "movie") != kind or entry.get("sig") != sig):
                entry = None                             # Einordnung hat sich geändert -> neu
            if entry is None:
                if info:
                    entry = {"id": fid, "kind": "episode", "added": st.st_mtime, "sig": sig,
                             "show_key": info["show_key"], "c_season": info["season"],
                             "c_episode": info["episode"], "season": info["season"],
                             "episode": info["episode"], "ep_title_guess": info["ep_title"],
                             "title": info["ep_title"] or os.path.splitext(os.path.basename(rel))[0]}
                else:
                    entry = new_movie_entry(fid, rel, st.st_mtime)
            entry.pop("last_played", None)
            entry["rel"] = rel
            entry["file"] = os.path.basename(rel)
            entry["size"] = st.st_size
            new[fid] = entry
            if info:
                key = info["show_key"]
                if key not in shows:
                    sh = old_shows.get(key)
                    if sh and sh.get("rejected"):           # jetzt sicher als Serie erkannt
                        sh = None
                    sh = sh or {"key": key, "guess_name": info["show_name"],
                                                "guess_year": info["show_year"], "matched": False}
                    sh["strong"] = bool(sh.get("manual"))
                    shows[key] = sh
                sh = shows[key]
                sh["strong"] = sh["strong"] or info["strong"]
                if not sh.get("guess_year") and info["show_year"]:
                    sh["guess_year"] = info["show_year"]

        with lock:
            library["films"] = new
            library["shows"] = {**{k: v for k, v in old_shows.items() if v.get("rejected")}, **shows}
            library["scanned_at"] = time.time()
        save_library()

        warning = ""
        key = (cfg.get("tmdb_key") or "").strip()
        if key:
            tmdb = TMDB(key)
            try:
                tmdb.check()
            except ScanError as exc:
                warning, tmdb = str(exc), None
            if tmdb:
                by_show = defaultdict(list)
                for e in new.values():
                    if e.get("kind") == "episode":
                        by_show[e["show_key"]].append(e)
                show_jobs = [(sh, by_show[k]) for k, sh in shows.items() if show_needs_work(sh, by_show[k])]
                movie_jobs = [e for e in new.values() if e.get("kind", "movie") == "movie" and needs_work(e)]
                total = len(show_jobs) + len(movie_jobs)
                done = 0
                set_state(phase="Infos und Bilder werden geladen …", total=total, done=0)

                with ThreadPoolExecutor(max_workers=3) as pool:
                    for _ in as_completed([pool.submit(enrich_show, sh, eps, tmdb) for sh, eps in show_jobs]):
                        done += 1
                        set_state(done=done)

                # Abgelehnte „Serien“ werden wieder als Filme behandelt
                for sh, eps in show_jobs:
                    if sh.get("rejected"):
                        total += len(eps)
                        with lock:
                            shows.pop(sh["key"], None)
                            library["shows"][sh["key"]] = {"key": sh["key"], "rejected": True}
                            for e in eps:
                                fresh = new_movie_entry(e["id"], e["rel"], e.get("added"))
                                fresh.update(rel=e["rel"], file=e["file"], size=e["size"])
                                new[e["id"]] = fresh
                                movie_jobs.append(fresh)
                set_state(total=total)
                save_library()

                with ThreadPoolExecutor(max_workers=4) as pool:
                    for i, _ in enumerate(as_completed([pool.submit(enrich, e, tmdb) for e in movie_jobs]), 1):
                        done += 1
                        set_state(done=done)
                        if i % 25 == 0:
                            save_library()
                save_library()
        else:
            warning = "Ohne TMDB-Schlüssel werden keine Cover und Infos geladen (siehe Einstellungen)."

        n_movies = sum(1 for e in new.values() if e.get("kind", "movie") == "movie")
        n_eps = len(new) - n_movies
        n_shows = len({e["show_key"] for e in new.values() if e.get("kind") == "episode"})
        msg = f"Fertig: {n_movies} Filme"
        if n_shows:
            msg += f" und {n_shows} Serie(n) mit {n_eps} Folgen"
        msg += "."
        missing = sum(1 for e in new.values() if e.get("kind", "movie") == "movie" and not e.get("matched"))
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
SHOW_PRIVATE = ("eps", "fetched")


class Handler(BaseHTTPRequestHandler):
    server_version = "Filmothek/2.0"

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
            maybe_reload_library()
            cfg = load_config()
            root = cfg.get("library_path") or ""
            with lock:
                films = [dict(f, last_played=played.get(f["id"])) for f in library["films"].values()]
                shows = {k: {kk: vv for kk, vv in v.items() if kk not in SHOW_PRIVATE}
                         for k, v in library.get("shows", {}).items() if not v.get("rejected")}
            return self.send_json({
                "films": films, "shows": shows, "scanned_at": library.get("scanned_at"),
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
            kind = "tv" if (qs.get("kind") or [""])[0] == "tv" else "movie"
            if not q:
                return self.fail("Bitte einen Titel eingeben.")
            try:
                res = TMDB(key).search(kind, q, int(year) if year.isdigit() else None)
            except Exception:
                return self.fail("Die Suche bei TMDB hat nicht geklappt. Besteht eine Internetverbindung?", 502)
            tkey, okey, dkey = (("title", "original_title", "release_date") if kind == "movie"
                                else ("name", "original_name", "first_air_date"))
            return self.send_json({"results": [{
                "id": r["id"], "title": r.get(tkey), "original_title": r.get(okey),
                "year": (r.get(dkey) or "")[:4],
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
                return self.fail("Die Datei wurde nicht gefunden. Vielleicht wurde sie verschoben? "
                                 "Bitte die Bibliothek aktualisieren.", 404)
            try:
                play_file(full)
            except Exception as exc:
                return self.fail(str(exc), 500)
            with lock:
                played[film["id"]] = time.time()
            save_played()
            return self.send_json({"ok": True})

        if path == "/api/assign":
            key = (load_config().get("tmdb_key") or "").strip()
            try:
                tid = int(data.get("tmdb_id"))
            except (TypeError, ValueError):
                tid = None
            if not key or not tid:
                return self.fail("Zuordnung nicht möglich.")
            tmdb = TMDB(key)

            if data.get("show"):                           # ganze Serie neu zuordnen
                with lock:
                    sh = library.get("shows", {}).get(str(data["show"]))
                    eps = [e for e in library["films"].values() if e.get("show_key") == data["show"]]
                if not sh:
                    return self.fail("Diese Serie ist nicht in der Bibliothek.", 404)
                try:
                    meta = fetch_show_meta(tmdb, tid)
                    with lock:
                        sh.update(meta)
                        sh.update(eps={}, fetched=[], manual=True, strong=True, rejected=False)
                        sh.pop("error", None)
                        for e in eps:
                            reset_episode(e)
                    attach_episodes(tmdb, sh, eps)
                except Exception:
                    return self.fail("Die Infos konnten nicht geladen werden.", 502)
                save_library()
                return self.send_json({"ok": True})

            with lock:
                film = library["films"].get(str(data.get("id")))
            if not film:
                return self.fail("Zuordnung nicht möglich.")
            try:
                meta = fetch_meta(tmdb, tid)
            except Exception:
                return self.fail("Die Infos konnten nicht geladen werden.", 502)
            with lock:
                film.update(meta)
                film["manual"] = True
                film.pop("error", None)
            save_library()
            return self.send_json({"ok": True})

        return self.fail("Unbekannte Aktion", 404)


def watch_for_updates():
    """Nach einem Update (z. B. „git pull“) startet sich die Filmothek von selbst neu."""
    me = os.path.abspath(__file__)
    try:
        start = os.path.getmtime(me)
    except OSError:
        return
    while True:
        time.sleep(15)
        try:
            changed = os.path.getmtime(me) != start
        except OSError:
            continue
        if changed and not scan_state["running"]:
            time.sleep(3)
            args = [a for a in sys.argv[1:] if a != "--browser"]
            os.execv(sys.executable, [sys.executable, me] + args)


def main():
    url = f"http://localhost:{PORT}/"
    if "--url" in sys.argv:
        print(url)
        return
    if os.name == "posix":
        os.umask(0o002)
    ensure_dirs()
    load_library()
    played.update(read_json(PLAYED_FILE, {}))
    if not os.path.exists(PLAYED_FILE):                 # „gesehen“ aus Version 1 übernehmen
        for f in library.get("films", {}).values():
            if f.get("last_played"):
                played[f["id"]] = f["last_played"]
        if played:
            save_played()
    open_browser = "--browser" in sys.argv
    try:
        httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"Filmothek läuft schon: {url}")
        if open_browser:
            webbrowser.open(url)
        return
    print(f"Filmothek läuft auf {url}  (Beenden mit Ctrl+C)")
    threading.Thread(target=fix_permissions, daemon=True).start()
    threading.Thread(target=watch_for_updates, daemon=True).start()
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
