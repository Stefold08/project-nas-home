#!/usr/bin/env python3
"""Videoteca: una piccola dashboard web per i film del Raspberry Pi.

Usa solo la libreria standard di Python, cosi gira anche su un Pi Zero.
La configurazione sta in config.json, nella stessa cartella dello script.
Descrizioni, durata e copertine arrivano da TMDB (themoviedb.org).
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.1"
APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("VIDEOTECA_CONFIG") or APP_DIR / "config.json")
INDEX_PATH = APP_DIR / "index.html"
TMDB_API = os.environ.get("VIDEOTECA_TMDB_API", "https://api.themoviedb.org/3").rstrip("/")
TMDB_IMG = os.environ.get("VIDEOTECA_TMDB_IMG", "https://image.tmdb.org/t/p/")

VIDEO_EXT = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".mpg", ".mpeg",
    ".ts", ".m2ts", ".webm", ".flv", ".vob", ".3gp",
}
IMAGE_EXT = {".jpg", ".jpeg", ".png"}
GB = 1024 ** 3
CHUNK = 1024 * 1024
MAX_IMAGE = 15 * 1024 * 1024
MAX_JSON = 1024 * 1024
MAX_PIECE = 32 * 1024 * 1024           # un pezzo di caricamento al massimo
UPLOAD_DIRNAME = ".videoteca-caricamenti"
SESSION_TTL = 3 * 24 * 3600            # i caricamenti interrotti restano 3 giorni
POSTER_RE = re.compile(r"^/[A-Za-z0-9_-]+\.(?:jpg|jpeg|png)$")
SID_RE = re.compile(r"^[0-9a-f]{16}$")
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")

DEFAULTS = {
    "library": "/mnt/film",
    "port": 8080,
    "tmdb_key": "",
    "language": "it-IT",
    "reserve_gb": 2,
    "password_salt": "",
    "password_hash": "",
}

for _mime, _ext in (
    ("video/x-matroska", ".mkv"), ("video/mp4", ".m4v"), ("video/mp2t", ".ts"),
    ("video/mp2t", ".m2ts"), ("video/webm", ".webm"), ("video/x-msvideo", ".avi"),
):
    mimetypes.add_type(_mime, _ext)


class AppError(Exception):
    """Errore da mostrare all'utente, con il codice HTTP da restituire."""

    def __init__(self, message, status=400, extra=None):
        super().__init__(message)
        self.status = status
        self.extra = extra or {}


# ---------------------------------------------------------------- configurazione

_cfg_lock = threading.Lock()
_auth_cache = set()


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            cfg.update(data)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        print(f"Non riesco a leggere {CONFIG_PATH}: {exc}. Uso i valori predefiniti.", file=sys.stderr)
    return cfg


CFG = load_config()


def save_config():
    with _cfg_lock:
        tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(CFG, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_PATH)


def language():
    return str(CFG.get("language") or "it-IT")


def server_port():
    return int(os.environ.get("VIDEOTECA_PORT") or CFG.get("port") or 8080)


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt or ""), 50_000).hex()


# ---------------------------------------------------------------- cartella dei film

def library_root():
    return Path(str(CFG.get("library") or DEFAULTS["library"])).resolve()


def resolve_rel(rel):
    """Trasforma un percorso relativo in assoluto, senza mai uscire dalla cartella dei film."""
    if not isinstance(rel, str) or not rel.strip() or "\x00" in rel:
        raise AppError("Percorso non valido.")
    root = library_root()
    path = (root / rel.lstrip("/")).resolve()
    if path == root or root not in path.parents:
        raise AppError("Percorso non valido.", 403)
    return path


def is_video_name(name):
    return not name.startswith(".") and os.path.splitext(name)[1].lower() in VIDEO_EXT


def resolve_video(rel):
    path = resolve_rel(rel)
    if not is_video_name(path.name) or not path.is_file():
        raise AppError("Questo film non c'è più nella cartella. Ricarica la pagina.", 404)
    return path


def meta_path(video):
    # File nascosto: Samba non lo mostra e MiniDLNA lo ignora.
    return video.with_name("." + video.name + ".videoteca.json")


def cover_path(video):
    # "Titolo.jpg" accanto a "Titolo.mkv": MiniDLNA lo usa come copertina sulla TV.
    return video.with_suffix(".jpg")


def videos_in(folder):
    try:
        return [p for p in folder.iterdir() if p.is_file() and is_video_name(p.name)]
    except OSError:
        return []


def has_own_folder(video):
    return video.parent != library_root() and len(videos_in(video.parent)) == 1


def read_meta(video):
    try:
        with open(meta_path(video), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def atomic_write(target, data):
    tmp = target.with_name("." + target.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, target)


def write_meta(video, meta):
    atomic_write(meta_path(video), json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))


def media_url(path, root):
    return "/media/" + urllib.parse.quote(path.relative_to(root).as_posix())


def item_for(video, root=None):
    root = root or library_root()
    st = video.stat()
    cover = cover_path(video)
    cover_url = None
    if cover.is_file():
        cover_url = media_url(cover, root) + f"?v={int(cover.stat().st_mtime)}"
    return {
        "path": video.relative_to(root).as_posix(),
        "file": video.name,
        "folder": video.parent.relative_to(root).as_posix() if video.parent != root else "",
        "size": st.st_size,
        "added": int(st.st_mtime),
        "meta": read_meta(video),
        "cover": cover_url,
        "url": media_url(video, root),
    }


def scan_library():
    root = library_root()
    if not root.is_dir():
        raise AppError(f"La cartella dei film ({root}) non esiste. Controlla \"library\" in config.json.", 500)
    items = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in filenames:
            if is_video_name(name):
                try:
                    items.append(item_for(Path(dirpath) / name, root))
                except OSError:
                    continue
    return items


def disk_info():
    usage = shutil.disk_usage(library_root())
    reserve = int(float(CFG.get("reserve_gb") or 0) * GB)
    # Lo spazio che serve per finire i caricamenti in corso è già "prenotato".
    pending = sum(max(0, s["size"] - s["offset"]) for s in read_sessions())
    return {
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "reserve": reserve,
        "pending": pending,
        "usable": max(0, usage.free - reserve - pending),
    }


def safe_name(text, fallback="Film"):
    """Nome di file o cartella valido anche su Windows e sul Mac."""
    text = unicodedata.normalize("NFC", str(text or ""))
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:100].strip(" .") or fallback


def unique_path(path):
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def human_size(n):
    if n >= GB:
        return f"{n / GB:.1f} GB".replace(".", ",")
    return f"{max(1, round(n / 1024 ** 2))} MB"


def clean_text(value, limit):
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value or ""))
    return text.strip()[:limit]


def to_int(value):
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- caricamenti a pezzi

_session_locks = {}
_session_locks_guard = threading.Lock()


def uploads_dir():
    return library_root() / UPLOAD_DIRNAME


def session_lock(sid):
    if not isinstance(sid, str) or not SID_RE.match(sid):
        raise AppError("Caricamento non valido.")
    with _session_locks_guard:
        return _session_locks.setdefault(sid, threading.Lock())


def session_files(sid):
    if not isinstance(sid, str) or not SID_RE.match(sid):
        raise AppError("Caricamento non valido.")
    folder = uploads_dir()
    return folder / f"{sid}.json", folder / f"{sid}.part", folder / f"{sid}.jpg"


def load_session(sid):
    info_file, part, cover = session_files(sid)
    try:
        with open(info_file, encoding="utf-8") as fh:
            session = json.load(fh)
    except (OSError, ValueError):
        raise AppError("Questo caricamento non c'è più sul Pi: ricomincialo scegliendo di nuovo il file.", 404)
    session["offset"] = part.stat().st_size if part.exists() else 0
    session["has_cover"] = cover.exists()
    return session


def save_session(session):
    info_file, _part, _cover = session_files(session["id"])
    data = {k: v for k, v in session.items() if k not in ("offset", "has_cover")}
    atomic_write(info_file, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def read_sessions():
    folder = uploads_dir()
    if not folder.is_dir():
        return []
    sessions = []
    for info_file in folder.glob("*.json"):
        if SID_RE.match(info_file.stem):
            try:
                sessions.append(load_session(info_file.stem))
            except AppError:
                continue
    return sorted(sessions, key=lambda s: s.get("created", 0))


def public_session(session):
    keys = ("id", "name", "size", "offset", "modified", "title", "info", "has_cover", "created", "updated")
    return {k: session.get(k) for k in keys}


def remove_session(sid):
    for path in session_files(sid):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def clean_info(info):
    """Tiene solo i dati validi di una scheda scelta prima del caricamento."""
    if not isinstance(info, dict):
        return None
    if info.get("tmdb_id"):
        poster = info.get("poster")
        return {
            "tmdb_id": to_int(info.get("tmdb_id")),
            "poster": poster if isinstance(poster, str) and POSTER_RE.match(poster) else None,
        }
    manual = info.get("manual")
    if isinstance(manual, dict) and clean_text(manual.get("title"), 200):
        return {"manual": {
            "title": clean_text(manual.get("title"), 200),
            "year": clean_text(manual.get("year"), 4),
            "runtime": clean_text(manual.get("runtime"), 4),
            "overview": clean_text(manual.get("overview"), 5000),
        }}
    return None


def start_upload(payload):
    name = os.path.basename(str(payload.get("name") or ""))
    if os.path.splitext(name)[1].lower() not in VIDEO_EXT:
        raise AppError("Questo file non sembra un video (mkv, mp4, avi...).", 415)
    size = to_int(payload.get("size"))
    if size <= 0:
        raise AppError("Il file è vuoto.")
    modified = to_int(payload.get("modified"))
    title = clean_text(payload.get("title"), 200) or os.path.splitext(name)[0]
    info = clean_info(payload.get("info"))
    now = int(time.time())

    # Stesso file già iniziato? Si riprende da dove era arrivato.
    sessions = read_sessions()
    resume = payload.get("resume")
    match = None
    if resume:
        match = next((s for s in sessions if s["id"] == resume), None)
        if match and (match["name"] != name or match["size"] != size):
            raise AppError(f"Questo non è il file che stavi caricando: scegli {match['name']}.", 409)
    if match is None:
        match = next((s for s in sessions if s["name"] == name and s["size"] == size and s.get("modified") == modified), None)
    if match:
        with session_lock(match["id"]):
            match.update(title=title, updated=now)
            if info is not None or not resume:
                match["info"] = info
            save_session(match)
        return public_session(match)

    disk = disk_info()
    if size > disk["usable"]:
        raise AppError(
            f"Spazio insufficiente: il film pesa {human_size(size)}, "
            f"ma puoi aggiungere al massimo {human_size(disk['usable'])}.", 507)
    if len(sessions) >= 50:
        raise AppError("Ci sono troppi caricamenti interrotti: eliminane qualcuno dal pannello Caricamenti.", 429)
    uploads_dir().mkdir(exist_ok=True)
    session = {"id": secrets.token_hex(8), "name": name, "size": size, "modified": modified,
               "title": title, "info": info, "created": now, "updated": now}
    save_session(session)
    _info_file, part, _cover = session_files(session["id"])
    part.touch()
    os.chmod(part, 0o644)
    return public_session(load_session(session["id"]))


def save_session_cover(sid, data):
    check_image(data)
    with session_lock(sid):
        load_session(sid)  # verifica che esista
        _info_file, _part, cover = session_files(sid)
        atomic_write(cover, data)
    return public_session(load_session(sid))


def finish_upload(sid):
    with session_lock(sid):
        session = load_session(sid)
        _info_file, part, cover = session_files(sid)
        if session["offset"] != session["size"]:
            raise AppError("Il film non è ancora arrivato tutto.", 409, {"offset": session["offset"]})
        root = library_root()
        title = safe_name(session.get("title") or os.path.splitext(session["name"])[0])
        ext = os.path.splitext(session["name"])[1].lower()
        folder = root / title
        folder.mkdir(exist_ok=True)
        target = unique_path(folder / (title + ext))
        os.replace(part, target)
        cover_data = cover.read_bytes() if cover.exists() else None
        remove_session(sid)

    warning = ""
    info = session.get("info")
    if info:
        try:
            save_info({**info, "path": target.relative_to(root).as_posix()})
        except AppError as exc:
            warning = f"Il film è caricato, ma la scheda non è stata salvata: {exc} Riprova dalla sua scheda con «Trova info»."
    if cover_data:
        write_cover(target, cover_data)
        meta = read_meta(target)
        if meta is not None:
            meta["cover"] = "personale"
            write_meta(target, meta)
    return {"item": item_for(target, root), "warning": warning}


def cancel_upload(sid):
    session_files(sid)  # verifica il formato
    with session_lock(sid):
        remove_session(sid)


def clean_leftovers():
    """Al riavvio toglie i pezzi rimasti da caricamenti vecchi o interrotti."""
    root = library_root()
    if not root.is_dir():
        return
    uploads = uploads_dir()
    for dirpath, _dirs, files in os.walk(root):
        if Path(dirpath) == uploads:
            continue
        for name in files:
            if name.startswith(".") and (name.endswith(".part") or name.endswith(".tmp")):
                try:
                    os.unlink(os.path.join(dirpath, name))
                except OSError:
                    pass
    if not uploads.is_dir():
        return
    now = time.time()
    for session in read_sessions():
        if now - (session.get("updated") or 0) > SESSION_TTL:
            remove_session(session["id"])
    for path in uploads.iterdir():
        if path.name.startswith(".") or not SID_RE.match(path.stem) or not (uploads / f"{path.stem}.json").exists():
            try:
                path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- rete

_net_cache = {"time": 0.0, "data": None}


def network_info():
    """Indirizzi del Pi: quelli di casa e, se c'è Tailscale, quello per fuori casa."""
    now = time.time()
    if _net_cache["data"] is not None and now - _net_cache["time"] < 60:
        return _net_cache["data"]
    addresses = []
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr", "show"], capture_output=True, text=True, timeout=3).stdout
        for iface in json.loads(out or "[]"):
            name = iface.get("ifname") or ""
            if name == "lo":
                continue
            for addr in iface.get("addr_info") or []:
                ip = addr.get("local")
                if not ip:
                    continue
                try:
                    remote = ipaddress.ip_address(ip) in TAILSCALE_NET
                except ValueError:
                    remote = False
                addresses.append({"iface": name, "ip": ip, "tailscale": remote or name.startswith("tailscale")})
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    ts_name = ""
    if any(a["tailscale"] for a in addresses) and shutil.which("tailscale"):
        try:
            out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=6).stdout
            ts_name = ((json.loads(out or "{}").get("Self") or {}).get("DNSName") or "").rstrip(".")
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    data = {"addresses": addresses, "tailscale_name": ts_name}
    _net_cache.update(time=now, data=data)
    return data


# ---------------------------------------------------------------- TMDB

def tmdb_call(path, params=None, key=None):
    key = (key if key is not None else CFG.get("tmdb_key") or "").strip()
    if not key:
        raise AppError("Manca la chiave TMDB: aprila dalle impostazioni e incollala.", 412)
    params = dict(params or {})
    headers = {"Accept": "application/json", "User-Agent": f"Videoteca/{VERSION}"}
    if "." in key:  # "API Read Access Token" (lungo, con i punti)
        headers["Authorization"] = "Bearer " + key
    else:  # "API Key" classica da 32 caratteri
        params["api_key"] = key
    url = TMDB_API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AppError("TMDB non accetta la chiave. Controlla di averla copiata per intero nelle impostazioni.", 400)
        if exc.code == 404:
            raise AppError("Questo film non c'è su TMDB.", 404)
        if exc.code == 429:
            raise AppError("TMDB sta ricevendo troppe richieste. Riprova tra qualche secondo.", 503)
        raise AppError(f"TMDB ha risposto con un errore ({exc.code}). Riprova più tardi.", 502)
    except (urllib.error.URLError, OSError, ValueError):
        raise AppError("Il Pi non riesce a raggiungere TMDB. Controlla che sia collegato a internet.", 502)


def tmdb_image(path, size):
    return f"{TMDB_IMG}{size}{path}" if path else None


def tmdb_search(query, year=""):
    query = (query or "").strip()
    if not query:
        raise AppError("Scrivi il titolo da cercare.")
    params = {"query": query[:150], "language": language(), "include_adult": "false"}
    year = (year or "").strip()
    if re.fullmatch(r"(19|20)\d\d", year):
        params["year"] = year
    results = tmdb_call("/search/movie", params).get("results") or []
    if not results and "year" in params:
        params.pop("year")
        results = tmdb_call("/search/movie", params).get("results") or []
    found = []
    for movie in results[:12]:
        overview = (movie.get("overview") or "").strip()
        if len(overview) > 220:
            overview = overview[:217].rsplit(" ", 1)[0] + "…"
        poster_path = movie.get("poster_path")
        found.append({
            "id": movie.get("id"),
            "title": movie.get("title") or movie.get("original_title") or "",
            "original_title": movie.get("original_title") or "",
            "year": (movie.get("release_date") or "")[:4],
            "poster": tmdb_image(poster_path, "w185"),
            "poster_path": poster_path if isinstance(poster_path, str) and POSTER_RE.match(poster_path) else None,
            "overview": overview,
        })
    return found


def tmdb_details(movie_id):
    try:
        movie_id = int(movie_id)
    except (TypeError, ValueError):
        raise AppError("Film non valido.")
    lang = language()
    short = lang.split("-")[0]
    data = tmdb_call(f"/movie/{movie_id}", {
        "language": lang,
        "append_to_response": "credits,images",
        "include_image_language": f"{short},en,null",
    })
    overview = (data.get("overview") or "").strip()
    if not overview and short != "en":
        try:
            overview = (tmdb_call(f"/movie/{movie_id}", {"language": "en-US"}).get("overview") or "").strip()
        except AppError:
            pass
    credits = data.get("credits") or {}
    directors = [c["name"] for c in credits.get("crew") or [] if c.get("job") == "Director" and c.get("name")]
    cast = [c["name"] for c in sorted(credits.get("cast") or [], key=lambda c: c.get("order", 999)) if c.get("name")][:6]

    images = (data.get("images") or {}).get("posters") or []
    posters, seen = [], set()

    def add(file_path, code, main=False):
        if file_path and file_path not in seen:
            seen.add(file_path)
            posters.append({
                "path": file_path,
                "lang": code or "",
                "main": main,
                "thumb": tmdb_image(file_path, "w185"),
                "large": tmdb_image(file_path, "w500"),
            })

    main_poster = data.get("poster_path")
    main_lang = next((p.get("iso_639_1") for p in images if p.get("file_path") == main_poster), "")
    add(main_poster, main_lang, True)

    def rank(p):
        code = p.get("iso_639_1")
        order = 0 if code == short else 1 if code is None else 2 if code == "en" else 3
        return (order, -(p.get("vote_average") or 0))

    for poster in sorted(images, key=rank):
        add(poster.get("file_path"), poster.get("iso_639_1"))

    meta = {
        "source": "tmdb",
        "tmdb_id": movie_id,
        "title": data.get("title") or data.get("original_title") or "",
        "original_title": data.get("original_title") or "",
        "year": (data.get("release_date") or "")[:4],
        "runtime": int(data.get("runtime") or 0),
        "genres": [g["name"] for g in data.get("genres") or [] if g.get("name")],
        "overview": overview,
        "tagline": (data.get("tagline") or "").strip(),
        "rating": round(float(data.get("vote_average") or 0), 1),
        "directors": directors,
        "cast": cast,
        "tmdb_url": f"https://www.themoviedb.org/movie/{movie_id}",
    }
    return meta, posters[:30]


def check_image(data):
    if len(data) > MAX_IMAGE:
        raise AppError("L'immagine è troppo grande (massimo 15 MB).", 413)
    if not (data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n")):
        raise AppError("Il file non è un'immagine JPEG o PNG.", 415)


def download_image(url):
    request = urllib.request.Request(url, headers={"User-Agent": f"Videoteca/{VERSION}"})
    try:
        with urllib.request.urlopen(request, timeout=40) as resp:
            data = resp.read(MAX_IMAGE + 1)
    except (urllib.error.URLError, OSError):
        raise AppError("Non riesco a scaricare la copertina da TMDB. Riprova.", 502)
    check_image(data)
    return data


# ---------------------------------------------------------------- azioni sui film

def write_cover(video, data):
    atomic_write(cover_path(video), data)
    if has_own_folder(video):
        # Windows e il Finder usano folder.jpg come anteprima della cartella.
        atomic_write(video.parent / "folder.jpg", data)


def valid_poster(poster):
    if not isinstance(poster, str) or not POSTER_RE.match(poster):
        raise AppError("Copertina non valida.")
    return poster


def save_info(payload):
    video = resolve_video(payload.get("path"))
    old = read_meta(video) or {}
    if payload.get("tmdb_id"):
        meta, _posters = tmdb_details(payload["tmdb_id"])
    elif isinstance(payload.get("manual"), dict):
        manual = payload["manual"]
        title = clean_text(manual.get("title"), 200)
        if not title:
            raise AppError("Scrivi almeno il titolo del film.")
        year = clean_text(manual.get("year"), 4)
        meta = {
            "source": "manuale",
            "title": title,
            "original_title": "",
            "year": year if re.fullmatch(r"\d{4}", year) else "",
            "runtime": to_int(manual.get("runtime")),
            "genres": [],
            "overview": clean_text(manual.get("overview"), 5000),
            "tagline": "",
            "rating": 0,
            "directors": [],
            "cast": [],
        }
    else:
        raise AppError("Scegli un film tra i risultati o compila la scheda a mano.")

    poster = payload.get("poster")
    if poster:
        write_cover(video, download_image(tmdb_image(valid_poster(poster), "w780")))
        meta["cover"] = "tmdb"
    elif old.get("cover") and cover_path(video).is_file():
        meta["cover"] = old["cover"]
    meta["updated"] = int(time.time())
    write_meta(video, meta)
    return item_for(video)


def set_cover_from_tmdb(rel, poster):
    video = resolve_video(rel)
    write_cover(video, download_image(tmdb_image(valid_poster(poster), "w780")))
    meta = read_meta(video)
    if meta is not None:
        meta["cover"] = "tmdb"
        write_meta(video, meta)
    return item_for(video)


def set_cover_from_upload(rel, data):
    video = resolve_video(rel)
    check_image(data)
    write_cover(video, data)
    meta = read_meta(video)
    if meta is not None:
        meta["cover"] = "personale"
        write_meta(video, meta)
    return item_for(video)


def delete_movie(rel):
    video = resolve_video(rel)
    folder = video.parent
    own_folder = has_own_folder(video)
    try:
        video.unlink()
    except PermissionError:
        raise AppError("Non ho il permesso di cancellare questo file. Controlla i permessi della cartella.", 403)
    for extra in (meta_path(video), cover_path(video)):
        try:
            extra.unlink()
        except OSError:
            pass
    if own_folder:
        for leftover in ("folder.jpg", ".DS_Store"):
            try:
                (folder / leftover).unlink()
            except OSError:
                pass
        try:
            folder.rmdir()  # solo se è rimasta vuota
        except OSError:
            pass


# ---------------------------------------------------------------- impostazioni

def settings_view():
    key = (CFG.get("tmdb_key") or "").strip()
    view = {
        "has_key": bool(key),
        "key_end": key[-4:] if key else "",
        "has_password": bool(CFG.get("password_hash")),
        "library": str(library_root()),
        "reserve_gb": CFG.get("reserve_gb"),
        "language": language(),
        "version": VERSION,
        "port": server_port(),
    }
    view.update(network_info())
    return view


def update_settings(payload):
    changed = False
    if "tmdb_key" in payload:
        key = str(payload.get("tmdb_key") or "").strip()
        if key:
            tmdb_call("/configuration", {}, key=key)  # verifica che la chiave funzioni
        CFG["tmdb_key"] = key
        changed = True
    if "password" in payload:
        password = str(payload.get("password") or "")
        if password:
            if len(password) < 4:
                raise AppError("Usa una password di almeno 4 caratteri.")
            salt = secrets.token_hex(16)
            CFG["password_salt"] = salt
            CFG["password_hash"] = hash_password(password, salt)
        else:
            CFG["password_salt"] = ""
            CFG["password_hash"] = ""
        _auth_cache.clear()
        changed = True
    if changed:
        save_config()
    return settings_view()


# ---------------------------------------------------------------- server web

class Handler(BaseHTTPRequestHandler):
    server_version = f"Videoteca/{VERSION}"
    protocol_version = "HTTP/1.1"
    timeout = 120

    def log_request(self, code="-", size="-"):
        pass  # niente registro per ogni richiesta: si risparmia la microSD

    def log_error(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---- risposte

    def finish_headers(self):
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.started = True

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.finish_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error_json(self, status, message, extra=None):
        if self.command == "POST" and not self.body_read:
            self.drain_or_close()
        if self.started:
            self.close_connection = True
            return
        try:
            self.send_json({"error": message, **(extra or {})}, status)
        except OSError:
            self.close_connection = True

    def drain_or_close(self):
        """Legge e scarta un corpo non troppo grande, così il browser riceve davvero la risposta."""
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        if 0 <= length <= MAX_PIECE:
            try:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                if remaining == 0:
                    self.body_read = True
                    return
            except OSError:
                pass
        self.close_connection = True

    def authorized(self):
        if not CFG.get("password_hash"):
            return True
        header = self.headers.get("Authorization", "")
        if header and header in _auth_cache:
            return True
        if header.startswith("Basic "):
            try:
                _user, _sep, password = base64.b64decode(header[6:].strip()).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                password = None
            if password is not None and hmac.compare_digest(
                hash_password(password, CFG.get("password_salt")), CFG.get("password_hash") or ""
            ):
                if len(_auth_cache) > 32:
                    _auth_cache.clear()
                _auth_cache.add(header)
                return True
        if self.command == "POST":
            self.drain_or_close()
        body = "Serve la password della Videoteca.".encode("utf-8")
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Videoteca", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.finish_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return False

    # ---- smistamento

    def do_GET(self):
        self.dispatch()

    def do_HEAD(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        self.started = False
        self.body_read = False
        try:
            if not self.authorized():
                return
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)

            def arg(name):
                return (query.get(name) or [""])[0]

            if self.command == "POST":
                self.route_post(url.path, arg)
            else:
                self.route_get(url.path, arg)
        except AppError as exc:
            self.send_error_json(exc.status, str(exc), exc.extra)
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except Exception as exc:  # errore imprevisto: lo registro e rispondo comunque
            self.log_error("Errore inatteso: %r", exc)
            self.send_error_json(500, f"Errore inatteso sul Pi: {exc}")

    def route_get(self, route, arg):
        if route in ("/", "/index.html"):
            return self.send_index()
        if route == "/api/library":
            return self.send_json({"items": scan_library(), "disk": disk_info()})
        if route == "/api/status":
            return self.send_json({"disk": disk_info()})
        if route == "/api/search":
            return self.send_json({"results": tmdb_search(arg("q"), arg("year"))})
        if route == "/api/tmdb":
            meta, posters = tmdb_details(arg("id"))
            return self.send_json({"meta": meta, "posters": posters})
        if route == "/api/settings":
            return self.send_json(settings_view())
        if route == "/api/upload/sessions":
            return self.send_json({"sessions": [public_session(s) for s in read_sessions()]})
        if route == "/api/upload/status":
            return self.send_json(public_session(load_session(arg("id"))))
        if route.startswith("/media/"):
            return self.send_media(urllib.parse.unquote(route[len("/media/"):]))
        raise AppError("Pagina non trovata.", 404)

    def route_post(self, route, arg):
        if route == "/api/upload/start":
            return self.send_json(start_upload(self.read_json()))
        if route == "/api/upload/chunk":
            return self.receive_piece(arg("id"), arg("offset"))
        if route == "/api/upload/cover":
            return self.send_json(save_session_cover(arg("id"), self.read_body(MAX_IMAGE)))
        if route == "/api/upload/finish":
            self.read_body(MAX_JSON)
            return self.send_json(finish_upload(arg("id")))
        if route == "/api/upload/cancel":
            self.read_body(MAX_JSON)
            cancel_upload(arg("id"))
            return self.send_json({"ok": True, "disk": disk_info()})
        if route == "/api/upload":
            return self.send_json({"item": self.receive_upload(arg)})
        if route == "/api/info":
            return self.send_json({"item": save_info(self.read_json())})
        if route == "/api/cover":
            if self.headers.get("Content-Type", "").startswith("application/json"):
                data = self.read_json()
                return self.send_json({"item": set_cover_from_tmdb(data.get("path"), data.get("poster"))})
            return self.send_json({"item": set_cover_from_upload(arg("path"), self.read_body(MAX_IMAGE))})
        if route == "/api/delete":
            delete_movie(self.read_json().get("path"))
            return self.send_json({"ok": True, "disk": disk_info()})
        if route == "/api/settings":
            return self.send_json(update_settings(self.read_json()))
        raise AppError("Pagina non trovata.", 404)

    # ---- lettura delle richieste

    def content_length(self):
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            raise AppError("Richiesta non valida.")
        if length < 0:
            raise AppError("Richiesta non valida.")
        return length

    def read_body(self, limit):
        length = self.content_length()
        if length > limit:
            raise AppError(f"File troppo grande (massimo {human_size(limit)}).", 413)
        data = self.rfile.read(length) if length else b""
        self.body_read = True
        if len(data) != length:
            raise AppError("La richiesta si è interrotta.")
        return data

    def read_json(self):
        raw = self.read_body(MAX_JSON)
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            raise AppError("Richiesta non valida.")
        if not isinstance(data, dict):
            raise AppError("Richiesta non valida.")
        return data

    def receive_piece(self, sid, offset_text):
        """Un pezzo di film: si aggiunge in fondo solo se arriva nel punto giusto."""
        length = self.content_length()
        if length <= 0 or length > MAX_PIECE:
            raise AppError("Pezzo di file non valido.")
        if not re.fullmatch(r"\d+", offset_text or ""):
            raise AppError("Posizione non valida.")
        offset = int(offset_text)
        with session_lock(sid):
            session = load_session(sid)
            if offset != session["offset"]:
                raise AppError("Il Pi aveva già ricevuto un altro punto del file: riparto da lì.", 409, {"offset": session["offset"]})
            if offset + length > session["size"]:
                raise AppError("Questo pezzo va oltre la fine del file.")
            _info_file, part, _cover = session_files(sid)
            try:
                with open(part, "ab") as fh:
                    remaining = length
                    while remaining > 0:
                        chunk = self.rfile.read(min(CHUNK, remaining))
                        if not chunk:
                            raise ConnectionError("pezzo interrotto")
                        fh.write(chunk)
                        remaining -= len(chunk)
            except BaseException:
                self.close_connection = True
                raise
            self.body_read = True
            session["updated"] = int(time.time())
            save_session(session)
        return self.send_json({"offset": offset + length, "size": session["size"]})

    def receive_upload(self, arg):
        """Caricamento in un colpo solo (lo usa chi non passa dalla pagina)."""
        length = self.content_length()
        if length <= 0:
            raise AppError("Il file è vuoto o non è arrivato.")
        original = os.path.basename(arg("name"))
        ext = os.path.splitext(original)[1].lower()
        if ext not in VIDEO_EXT:
            raise AppError("Questo file non sembra un video (mkv, mp4, avi...).", 415)
        disk = disk_info()
        if length > disk["usable"]:
            raise AppError(
                f"Spazio insufficiente: il film pesa {human_size(length)}, "
                f"ma puoi aggiungere al massimo {human_size(disk['usable'])}.", 507)
        root = library_root()
        title = safe_name(arg("title") or os.path.splitext(original)[0])
        folder = root / title
        created = not folder.exists()
        folder.mkdir(exist_ok=True)
        target = unique_path(folder / (title + ext))
        part = folder / ("." + target.name + ".part")
        try:
            with open(part, "wb") as fh:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(CHUNK, remaining))
                    if not chunk:
                        raise ConnectionError("caricamento interrotto")
                    fh.write(chunk)
                    remaining -= len(chunk)
            self.body_read = True
            os.chmod(part, 0o644)
            os.replace(part, target)
        except BaseException:
            self.close_connection = True
            try:
                part.unlink()
            except OSError:
                pass
            if created:
                try:
                    folder.rmdir()
                except OSError:
                    pass
            raise
        return item_for(target, root)

    # ---- file

    def send_index(self):
        try:
            body = INDEX_PATH.read_bytes()
        except OSError:
            raise AppError("Manca il file index.html accanto a videoteca.py.", 500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.finish_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_media(self, rel):
        path = resolve_rel(rel)
        if not path.is_file() or path.name.startswith(".") or UPLOAD_DIRNAME in path.parts:
            raise AppError("File non trovato.", 404)
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end, status = 0, size - 1, 200
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        if match and (match.group(1) or match.group(2)):
            if match.group(1):
                start = int(match.group(1))
                if match.group(2):
                    end = min(int(match.group(2)), size - 1)
            else:
                start = max(0, size - int(match.group(2)))
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.finish_headers()
                return
            status = 206
        length = end - start + 1 if size else 0
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if path.suffix.lower() in IMAGE_EXT:
            self.send_header("Cache-Control", "public, max-age=604800")
        self.finish_headers()
        if self.command == "HEAD" or length == 0:
            return
        with open(path, "rb") as fh:
            self.connection.sendfile(fh, start, length)  # copia diretta dal disco alla rete


def main():
    clean_leftovers()
    server = ThreadingHTTPServer(("0.0.0.0", server_port()), Handler)
    server.daemon_threads = True
    print(f"Videoteca {VERSION} pronta sulla porta {server_port()} (film in {library_root()})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
