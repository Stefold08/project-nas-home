#!/bin/bash
# Installa la Videoteca, la dashboard web dei film, sul Raspberry Pi.
# Uso:  bash installa-videoteca.sh     (senza sudo: la password la chiede lo script)
# Puoi rilanciarlo per aggiornare: la chiave TMDB e le impostazioni restano.
set -euo pipefail

APP_DIR="${VIDEOTECA_DIR:-/opt/videoteca}"
LIBRARY="${VIDEOTECA_LIBRARY:-/mnt/film}"
PORT="${VIDEOTECA_PORT:-8080}"

if [ "$(id -u)" -eq 0 ]; then
  RUN_USER="${VIDEOTECA_USER:-${SUDO_USER:-}}"
  SUDO=""
  if [ -z "$RUN_USER" ] || [ "$RUN_USER" = "root" ]; then
    echo "Lancia lo script come utente normale, senza sudo:  bash $0"
    exit 1
  fi
else
  RUN_USER="$(id -un)"
  SUDO="sudo"
fi

echo "Installo la Videoteca in $APP_DIR (utente $RUN_USER, film in $LIBRARY)..."

if ! command -v python3 >/dev/null 2>&1; then
  echo "Installo Python 3..."
  $SUDO apt-get install -y python3
fi

$SUDO mkdir -p "$APP_DIR" "$LIBRARY"
$SUDO chown "$RUN_USER:$RUN_USER" "$APP_DIR" "$LIBRARY"

$SUDO tee "$APP_DIR/videoteca.py" >/dev/null <<'VIDEOTECA_PY_FINE'
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
VIDEOTECA_PY_FINE

$SUDO tee "$APP_DIR/index.html" >/dev/null <<'VIDEOTECA_HTML_FINE'
<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#131b2e">
<title>Videoteca</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:ital,wght@0,400;0,700;1,400&family=Big+Shoulders+Display:wght@600;800&display=swap">
<style>
:root {
  --sala: #131b2e;
  --velluto: #1d2842;
  --velluto-2: #283657;
  --ottone: #d8b068;
  --ottone-chiaro: #f0d08f;
  --luce: #f2ece1;
  --polvere: #a3abbd;
  --mattone: #ec7b66;
  --linea: rgba(242, 236, 225, .16);
  --display: "Big Shoulders Display", "Arial Narrow", "Roboto Condensed", "Helvetica Neue", sans-serif;
  --testo: "Atkinson Hyperlegible", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --bordo: clamp(16px, 4vw, 48px);
  color-scheme: dark;
}
*, *::before, *::after { box-sizing: border-box; }
[hidden] { display: none !important; }
html { background: var(--sala); }
body {
  margin: 0; min-height: 100vh; display: flex; flex-direction: column;
  color: var(--luce); font: 400 17px/1.55 var(--testo);
  -webkit-font-smoothing: antialiased;
}
main { flex: 1; }
body.has-dock main { padding-bottom: 180px; }
a { color: var(--ottone); text-underline-offset: 3px; }
img { display: block; }
h1, h2, h3 { font-weight: 800; }
.visually-hidden {
  position: absolute !important; width: 1px; height: 1px; margin: -1px; padding: 0;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
}

/* Controlli */
button, .btn {
  -webkit-appearance: none; appearance: none;
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  min-height: 44px; padding: 10px 18px;
  border: 1px solid rgba(242, 236, 225, .28); border-radius: 6px;
  background: transparent; color: var(--luce);
  font: 700 15px/1.1 var(--testo); text-decoration: none; cursor: pointer;
}
button:hover, .btn:hover { border-color: var(--luce); }
button:disabled { opacity: .5; cursor: not-allowed; }
button:disabled:hover { border-color: rgba(242, 236, 225, .28); }
.primary { background: var(--ottone); border-color: var(--ottone); color: #1a1408; }
.primary:hover { background: var(--ottone-chiaro); border-color: var(--ottone-chiaro); }
.primary:disabled:hover { background: var(--ottone); border-color: var(--ottone); }
.danger { color: #f6ab9b; border-color: rgba(236, 123, 102, .55); }
.danger:hover { border-color: var(--mattone); }
.danger.solid { background: var(--mattone); border-color: var(--mattone); color: #200b06; }
.small { min-height: 34px; padding: 6px 12px; font-size: 14px; }
.icon { width: 44px; padding: 0; }
.link-btn {
  min-height: 0; padding: 0; border: 0; border-radius: 0;
  color: var(--ottone); text-decoration: underline; text-underline-offset: 3px;
}
.link-btn:hover { color: var(--ottone-chiaro); }
:focus-visible { outline: 3px solid var(--ottone-chiaro); outline-offset: 3px; }
input, select, textarea {
  width: 100%; min-height: 44px; padding: 10px 12px;
  font: 400 16px/1.4 var(--testo); color: var(--luce);
  background: var(--sala); border: 1px solid rgba(242, 236, 225, .24); border-radius: 6px;
}
input[type="checkbox"] { width: 22px; height: 22px; min-height: 0; padding: 0; margin: 0; accent-color: var(--ottone); }
textarea { resize: vertical; }
select { width: auto; }
input::placeholder { color: #7c869a; }

/* Intestazione */
.marquee {
  display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between;
  gap: 18px 32px; padding: clamp(22px, 4vw, 44px) var(--bordo) 22px;
  border-bottom: 1px solid rgba(216, 176, 104, .3);
}
.brand h1 { margin: 0; font: 800 clamp(58px, 9vw, 116px)/.8 var(--display); color: var(--ottone); }
.tally { margin: 12px 0 0; color: var(--polvere); font-size: 16px; }
.tools { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: flex-end; }
.tools .search input { width: 230px; }
.space { flex: 1 1 100%; display: grid; gap: 8px; }
.meter { display: flex; height: 6px; border-radius: 3px; overflow: hidden; background: var(--velluto-2); }
.meter span { display: block; height: 100%; }
.m-used { background: var(--polvere); }
.m-reserve { background: rgba(216, 176, 104, .45); }
.space-text { margin: 0; font-size: 15px; color: var(--polvere); }
.space-text strong { color: var(--luce); }

/* Avvisi e stati vuoti */
.notice {
  display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 14px 24px;
  margin: 24px var(--bordo) 0; padding: 16px 20px;
  border: 1px solid rgba(216, 176, 104, .5); border-radius: 8px;
}
.notice p { margin: 0; max-width: 70ch; }
.notice .actions { flex-wrap: nowrap; }
.notice.inline { margin: 0 0 18px; }
.empty { padding: 56px var(--bordo); max-width: 760px; }
.empty h2 { margin: 0 0 12px; font: 800 52px/1 var(--display); }
.empty p { margin: 0 0 22px; color: var(--polvere); }
.path { color: var(--luce); font-weight: 700; overflow-wrap: anywhere; }
.no-match, .wall-error { margin: 0; padding: 32px var(--bordo); color: var(--polvere); }
.wall-error { color: var(--mattone); }

/* Muro delle locandine */
.wall {
  list-style: none; margin: 0; padding: 34px var(--bordo) 60px;
  display: grid; grid-template-columns: repeat(auto-fill, minmax(156px, 1fr)); gap: 38px 24px;
}
.wall:empty { display: none; }
.film { min-width: 0; }
.poster {
  display: block; width: 100%; aspect-ratio: 2 / 3; padding: 0;
  border: 0; border-radius: 3px; overflow: hidden;
  background: var(--velluto); color: inherit;
}
.poster:hover { border: 0; outline: 2px solid var(--ottone); outline-offset: 4px; }
.poster img { width: 100%; height: 100%; object-fit: cover; }
.film-title {
  margin: 14px 0 4px; font: 700 16px/1.3 var(--testo); overflow-wrap: anywhere;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.facts { display: flex; flex-wrap: wrap; gap: 2px 14px; margin: 0; font-size: 14px; color: var(--polvere); }
.facts .year { color: var(--ottone); font-weight: 700; }
.blank {
  display: flex; flex-direction: column; justify-content: space-between; gap: 10px;
  width: 100%; height: 100%; padding: 14px 13px 13px; text-align: left;
  background: var(--velluto); border: 1px solid rgba(216, 176, 104, .45); border-radius: 3px;
}
.blank-title { font: 800 clamp(24px, 2.4vw, 32px)/.92 var(--display); color: var(--luce); overflow-wrap: anywhere; }
.blank-year { font: 600 20px/1 var(--display); color: var(--ottone); }

/* Finestre */
dialog {
  width: min(980px, calc(100vw - 24px)); max-height: calc(100dvh - 24px);
  padding: 0; border: 0; border-radius: 10px;
  background: var(--velluto); color: var(--luce);
}
dialog::backdrop { background: rgba(6, 9, 18, .8); }
dialog.small { width: min(500px, calc(100vw - 24px)); }
dialog.medium { width: min(720px, calc(100vw - 24px)); }
.panel { padding: 24px clamp(18px, 3vw, 32px) 26px; }
.panel-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 18px; }
.panel-head h2, .small h2 { margin: 0; font: 800 36px/1 var(--display); }
.close { width: 44px; padding: 0; border-color: transparent; flex: none; }
.close.floating { position: absolute; top: 12px; right: 12px; z-index: 2; background: rgba(19, 27, 46, .75); }

/* Scheda del film */
.detail {
  display: grid; grid-template-columns: minmax(200px, 320px) minmax(0, 1fr);
  gap: clamp(20px, 4vw, 44px); padding: clamp(18px, 3vw, 36px);
}
.detail-poster { margin: 0; aspect-ratio: 2 / 3; border-radius: 3px; overflow: hidden; background: var(--sala); align-self: start; }
.detail-poster img { width: 100%; height: 100%; object-fit: cover; }
.detail-title { margin: 0 52px 8px 0; font: 800 clamp(42px, 5.4vw, 68px)/.9 var(--display); overflow-wrap: anywhere; }
.orig { margin: 0 0 6px; color: var(--polvere); font-style: italic; }
.facts-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(118px, 1fr)); gap: 12px 20px;
  margin: 18px 0; padding: 14px 0; border-top: 1px solid var(--linea); border-bottom: 1px solid var(--linea);
}
.facts-grid dt { font-size: 13px; color: var(--polvere); }
.facts-grid dd { margin: 2px 0 0; font-weight: 700; }
.tagline { margin: 0 0 10px; color: var(--ottone); font-style: italic; }
.overview { margin: 0 0 18px; max-width: 62ch; }
.muted { color: var(--polvere); }
.people { display: grid; grid-template-columns: auto 1fr; gap: 6px 16px; margin: 0 0 18px; }
.people dt { color: var(--polvere); }
.people dd { margin: 0; }
.file-line { margin: 0 0 18px; font-size: 14px; color: var(--polvere); overflow-wrap: anywhere; }
.actions { display: flex; flex-wrap: wrap; gap: 10px; }
.actions.end { justify-content: flex-end; }
.source { margin: 16px 0 0; font-size: 13px; color: var(--polvere); }

/* Procedura guidata */
.steps { display: flex; flex-wrap: wrap; gap: 8px 22px; list-style: none; margin: 12px 0 0; padding: 0; counter-reset: passo; }
.steps li { counter-increment: passo; display: flex; align-items: center; gap: 8px; color: var(--polvere); font-size: 15px; }
.steps li::before {
  content: counter(passo); display: grid; place-items: center; width: 26px; height: 26px;
  border: 1px solid currentColor; border-radius: 50%; font-size: 13px; font-weight: 700;
}
.steps li.done { color: var(--ottone); }
.steps li[aria-current="step"] { color: var(--luce); font-weight: 700; }
.steps li[aria-current="step"]::before { background: var(--ottone); border-color: var(--ottone); color: #1a1408; }
.wiz-body { max-height: min(60vh, 640px); overflow: auto; padding: 6px 4px; margin: 0 -4px; }
.wiz-foot {
  display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 12px;
  margin-top: 20px; padding-top: 18px; border-top: 1px solid var(--linea);
}
.dropzone {
  display: grid; place-items: center; align-content: center; gap: 8px; min-height: 230px; padding: 28px;
  border: 2px dashed rgba(216, 176, 104, .55); border-radius: 10px; text-align: center; cursor: pointer;
}
.dropzone:hover, .dropzone.over { border-color: var(--ottone-chiaro); background: rgba(216, 176, 104, .06); }
.visually-hidden:focus-visible + .dropzone { outline: 3px solid var(--ottone-chiaro); outline-offset: 3px; }
.dz-main { font: 800 36px/1 var(--display); overflow-wrap: anywhere; }
.dz-sub { color: var(--polvere); max-width: 52ch; }
.dropzone.has-file .dz-main { color: var(--ottone); }
.search-form, .row { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; }
.search-form label, .manual label, .row label { display: grid; gap: 6px; }
.grow { flex: 1 1 240px; }
.short { flex: 0 0 110px; }
.lbl { font-size: 14px; color: var(--polvere); }
.results, .batch { list-style: none; margin: 18px 0 0; padding: 0; display: grid; gap: 10px; }
.result {
  display: grid; grid-template-columns: 64px minmax(0, 1fr); align-items: start; gap: 16px;
  width: 100%; padding: 10px; text-align: left; font-weight: 400;
  border-color: var(--linea); border-radius: 8px;
}
.result:hover, .result.picked { border-color: var(--ottone); }
.result.picked { background: rgba(216, 176, 104, .08); }
.result img, .thumb-blank { width: 64px; aspect-ratio: 2 / 3; object-fit: cover; border-radius: 2px; background: var(--sala); }
.thumb-blank { display: block; border: 1px solid var(--linea); }
.r-text { display: grid; gap: 3px; min-width: 0; }
.r-title { font-weight: 700; font-size: 17px; line-height: 1.3; }
.r-sub { color: var(--polvere); font-size: 14px; font-style: italic; }
.r-file { color: var(--polvere); font-size: 13px; overflow-wrap: anywhere; }
.r-over {
  color: var(--polvere); font-size: 14px; line-height: 1.45;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.batch-row {
  display: grid; grid-template-columns: auto 52px minmax(0, 1fr); align-items: center; gap: 14px;
  padding: 10px 12px; border: 1px solid var(--linea); border-radius: 8px;
}
.batch-row.off { opacity: .55; }
.batch-row img, .batch-row .thumb-blank { width: 52px; }
.batch-check { display: grid; place-items: center; width: 34px; height: 34px; cursor: pointer; }
.manual-link { margin: 20px 0 0; }
.manual { display: grid; gap: 14px; margin-top: 22px; padding-top: 18px; border-top: 1px solid var(--linea); }
.manual h3 { margin: 0; font: 800 28px/1 var(--display); }
.picked-summary { margin: 0 0 4px; }
.covers { display: grid; grid-template-columns: repeat(auto-fill, minmax(118px, 1fr)); gap: 20px 14px; margin-top: 16px; }
.cover-opt, .cover-upload { position: relative; display: block; cursor: pointer; }
.cover-opt input { position: absolute; inset: 0; width: 100%; height: 100%; margin: 0; opacity: 0; cursor: pointer; }
.frame {
  display: block; aspect-ratio: 2 / 3; border-radius: 3px; overflow: hidden; background: var(--sala);
  outline: 3px solid transparent; outline-offset: 3px;
}
.frame img { width: 100%; height: 100%; object-fit: cover; }
.cover-opt:hover .frame { outline-color: rgba(216, 176, 104, .5); }
.cover-opt input:checked + .frame { outline-color: var(--ottone); }
.cover-opt input:focus-visible + .frame, .cover-upload input:focus-visible + .frame { outline-color: var(--ottone-chiaro); }
.cap { display: block; margin-top: 8px; font-size: 13px; color: var(--polvere); }
.cover-opt input:checked ~ .cap { color: var(--ottone); font-weight: 700; }
.frame.add {
  display: grid; place-items: center; border: 2px dashed rgba(216, 176, 104, .5);
  font: 800 48px/1 var(--display); color: var(--ottone);
}
.cover-upload:hover .frame.add { border-color: var(--ottone-chiaro); }
.progress { height: 12px; border-radius: 6px; overflow: hidden; background: var(--sala); }
.progress span { display: block; width: 0; height: 100%; background: var(--ottone); transition: width .4s linear; }
.progress.thin { height: 6px; border-radius: 3px; }
.hint { margin: 0 0 12px; color: var(--polvere); font-size: 15px; max-width: 70ch; }
.form-error { margin: 12px 0 0; color: var(--mattone); font-size: 15px; }
.form-error:empty { display: none; }

/* Pannello dei caricamenti */
.dock {
  position: fixed; right: 16px; bottom: 16px; z-index: 5;
  display: flex; flex-direction: column;
  width: min(400px, calc(100vw - 32px)); max-height: min(60vh, 520px);
  background: var(--velluto); border: 1px solid rgba(216, 176, 104, .45); border-radius: 10px;
  box-shadow: 0 24px 60px rgba(0, 0, 0, .55);
}
.dock-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 12px 12px 16px; }
.dock-head h2 { margin: 0; font: 800 24px/1 var(--display); }
.dock-sum { margin: 4px 0 0; font-size: 14px; color: var(--polvere); }
.dock-list { list-style: none; margin: 0; padding: 0 16px 6px; overflow: auto; border-top: 1px solid var(--linea); }
.dock.collapsed .dock-list { display: none; }
.job { padding: 12px 0; border-bottom: 1px solid var(--linea); }
.job:last-child { border-bottom: 0; }
.job-title { margin: 0 0 8px; font-weight: 700; overflow-wrap: anywhere; }
.job-status { margin: 8px 0 0; font-size: 14px; color: var(--polvere); overflow-wrap: anywhere; }
.job.errore .job-status { color: var(--mattone); }
.job.fatto .job-status { color: var(--ottone); }
.job.fatto .progress span { background: var(--polvere); }
.job-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.job-actions label { cursor: pointer; }

/* Impostazioni */
.set-block { padding: 18px 0; border-top: 1px solid var(--linea); }
.set-block h3 { margin: 0 0 8px; font: 700 18px/1.3 var(--testo); }
.status { margin: 0 0 12px; font-size: 15px; }
.addr { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 8px 18px; margin: 0 0 12px; }
.addr > div { display: contents; }
.addr dt { color: var(--polvere); }
.addr dd { margin: 0; font-weight: 700; overflow-wrap: anywhere; }
@media (max-width: 560px) {
  .addr { grid-template-columns: minmax(0, 1fr); gap: 2px; }
  .addr dd { margin-bottom: 10px; }
}
#pwRemove { margin-top: 12px; }

.credits-foot { padding: 20px var(--bordo) 28px; font-size: 13px; color: var(--polvere); border-top: 1px solid var(--linea); }

.toast {
  position: fixed; inset: 16px 16px auto; margin: 0 auto; z-index: 20;
  width: fit-content; max-width: 560px; padding: 12px 18px; border: 0; border-radius: 8px;
  background: var(--luce); color: var(--sala); font-weight: 700; line-height: 1.35;
  box-shadow: 0 12px 30px rgba(0, 0, 0, .45);
}

@media (max-width: 760px) {
  .tools { flex: 1 1 100%; justify-content: flex-start; }
  .tools .search { flex: 1 1 100%; }
  .tools .search input { width: 100%; }
  .tools .sort { flex: 1 1 40%; min-width: 0; }
  .tools .sort select { width: 100%; }
  .tools #addBtn { flex: 1 1 auto; }
  .wall { grid-template-columns: repeat(auto-fill, minmax(132px, 1fr)); gap: 30px 16px; }
  .detail { grid-template-columns: 1fr; }
  .detail-poster { width: min(240px, 70%); }
  .dock { right: 8px; left: 8px; bottom: 8px; width: auto; }
  .toast { inset: 10px 10px auto; padding: 10px 14px; font-size: 15px; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { transition: none !important; animation: none !important; }
}
</style>
</head>
<body>
<header class="marquee">
  <div class="brand">
    <h1>Videoteca</h1>
    <p class="tally" id="tally">Carico i film…</p>
  </div>
  <div class="tools">
    <label class="search">
      <span class="visually-hidden">Cerca tra i tuoi film</span>
      <input type="search" id="q" placeholder="Cerca un film" autocomplete="off">
    </label>
    <label class="sort" id="genreWrap" hidden>
      <span class="visually-hidden">Filtra per genere</span>
      <select id="genre"></select>
    </label>
    <label class="sort">
      <span class="visually-hidden">Ordina i film</span>
      <select id="sort">
        <option value="added">Più recenti</option>
        <option value="title">Per titolo</option>
        <option value="year">Per anno</option>
      </select>
    </label>
    <button class="primary" type="button" id="addBtn">Aggiungi film</button>
    <button class="icon" type="button" id="settingsBtn" aria-label="Impostazioni" title="Impostazioni">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/></svg>
    </button>
  </div>
  <div class="space" id="space" hidden>
    <div class="meter" aria-hidden="true"><span class="m-used" id="mUsed"></span><span class="m-reserve" id="mReserve"></span></div>
    <p class="space-text"><strong id="spaceMain"></strong> <span id="spaceNote"></span></p>
  </div>
</header>

<main>
  <section class="notice" id="keyNotice" hidden>
    <p>Per trovare da sola descrizioni, durata e copertine, la videoteca ha bisogno di una chiave gratuita di TMDB, il database dei film. Ci vogliono un paio di minuti.</p>
    <button class="primary" type="button" id="keyNoticeBtn">Inserisci la chiave</button>
  </section>
  <section class="notice" id="missingNotice" hidden>
    <p id="missingText"></p>
    <div class="actions">
      <button type="button" id="missingHide">Nascondi</button>
      <button class="primary" type="button" id="missingBtn">Trova le info per tutti</button>
    </div>
  </section>
  <p class="wall-error" id="wallError" role="alert" hidden></p>
  <ul class="wall" id="wall" aria-label="I tuoi film"></ul>
  <section class="empty" id="empty" hidden>
    <h2>La videoteca è vuota</h2>
    <p>Aggiungi il primo film da qui, oppure copialo nella cartella condivisa <span class="path" id="sharePath"></span><span id="shareHow"></span>: comparirà da solo.</p>
    <button class="primary" type="button" id="emptyAdd">Aggiungi film</button>
  </section>
  <p class="no-match" id="noMatch" hidden>Nessun film corrisponde alla ricerca.</p>
</main>

<footer class="credits-foot">
  Descrizioni e copertine da <a href="https://www.themoviedb.org" target="_blank" rel="noopener">The Movie Database (TMDB)</a>. Questa app usa le API di TMDB ma non è approvata né certificata da TMDB.
</footer>

<aside class="dock" id="dock" aria-label="Caricamenti" hidden>
  <div class="dock-head">
    <div>
      <h2>Caricamenti</h2>
      <p class="dock-sum" id="dockSum" aria-live="polite"></p>
    </div>
    <button class="small" type="button" id="dockToggle" aria-expanded="true" aria-controls="dockList">Riduci</button>
  </div>
  <ul class="dock-list" id="dockList"></ul>
</aside>

<dialog id="detail" aria-labelledby="detailTitle">
  <button class="close floating" type="button" data-close aria-label="Chiudi">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>
  </button>
  <div class="detail" id="detailBody"></div>
</dialog>

<dialog id="wizard" aria-labelledby="wizTitle">
  <div class="panel">
    <header class="panel-head">
      <div>
        <h2 id="wizTitle"></h2>
        <ol class="steps" id="wizSteps"></ol>
      </div>
      <button class="close" type="button" id="wizClose" aria-label="Chiudi">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
    </header>
    <div class="wiz-body" id="wizBody"></div>
    <footer class="wiz-foot" id="wizFoot"></footer>
  </div>
</dialog>

<dialog id="settings" class="medium" aria-labelledby="setTitle">
  <div class="panel">
    <header class="panel-head">
      <h2 id="setTitle">Impostazioni</h2>
      <button class="close" type="button" data-close aria-label="Chiudi">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
    </header>
    <section class="set-block">
      <h3>Indirizzi</h3>
      <dl class="addr" id="addrList"></dl>
      <p class="hint" id="remoteHint"></p>
    </section>
    <section class="set-block">
      <h3>Chiave TMDB</h3>
      <p class="hint">Serve per trovare descrizioni, durata e copertine, ed è gratuita. Crea un account su <a href="https://www.themoviedb.org/signup" target="_blank" rel="noopener">themoviedb.org</a>, poi in Impostazioni, alla voce API, richiedi una chiave di tipo Developer. Puoi incollare sia la «API Key» sia il «API Read Access Token».</p>
      <p class="status" id="keyStatus"></p>
      <form class="row" id="keyForm">
        <label class="grow"><span class="visually-hidden">Chiave TMDB</span><input type="password" id="keyInput" autocomplete="off" spellcheck="false" placeholder="Incolla qui la chiave"></label>
        <button class="primary" type="submit">Salva chiave</button>
      </form>
      <p class="form-error" id="keyError" role="alert"></p>
    </section>
    <section class="set-block">
      <h3>Password</h3>
      <p class="hint">Facoltativa, ma consigliata se usi la videoteca anche fuori casa. Il browser la chiede la prima volta che apri la videoteca; come nome utente puoi scrivere quello che vuoi.</p>
      <p class="status" id="pwStatus"></p>
      <form class="row" id="pwForm">
        <label class="grow"><span class="visually-hidden">Nuova password</span><input type="password" id="pwInput" autocomplete="new-password" placeholder="Nuova password"></label>
        <button class="primary" type="submit">Salva password</button>
      </form>
      <button type="button" id="pwRemove" hidden>Togli la password</button>
      <p class="form-error" id="pwError" role="alert"></p>
    </section>
    <section class="set-block">
      <h3>Informazioni</h3>
      <p class="hint" id="aboutText"></p>
      <p class="hint">Descrizioni e copertine da <a href="https://www.themoviedb.org" target="_blank" rel="noopener">The Movie Database (TMDB)</a>. Questa app usa le API di TMDB ma non è approvata né certificata da TMDB.</p>
    </section>
  </div>
</dialog>

<dialog id="confirm" class="small" aria-labelledby="confirmTitle">
  <div class="panel">
    <h2 id="confirmTitle"></h2>
    <p id="confirmText"></p>
    <p class="form-error" id="confirmError" role="alert"></p>
    <div class="actions end">
      <button type="button" data-close>Annulla</button>
      <button class="danger solid" type="button" id="confirmYes">Elimina film</button>
    </div>
  </div>
</dialog>

<div class="toast" id="toast" role="status" aria-live="polite" popover="manual"></div>

<script>
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const VIDEO_EXTS = ['.mkv', '.mp4', '.m4v', '.avi', '.mov', '.wmv', '.mpg', '.mpeg', '.ts', '.m2ts', '.webm', '.flv', '.vob', '.3gp'];
const ACCEPT_VIDEO = 'video/*,' + VIDEO_EXTS.join(',');
const PIECE = 8 * 1024 * 1024;
const IS_MAC = /Mac|iPhone|iPad|iPod/.test(navigator.platform || '') || /Macintosh|Mac OS X/.test(navigator.userAgent);
const HAS_POPOVER = typeof HTMLElement !== 'undefined' && 'showPopover' in HTMLElement.prototype;

const store = {
  get(key, fallback) { try { return localStorage.getItem('videoteca.' + key) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem('videoteca.' + key, value); } catch { /* memoria del browser non disponibile */ } },
};
const state = {
  items: [], disk: null, settings: null, sessions: [], query: '', bulk: null,
  sort: store.get('sort', 'added'), genre: store.get('genre', ''),
};

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = value;
    else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat(2)) {
    if (child === null || child === undefined || child === false || child === '') continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

/* ---------- Formati ---------- */
const nf1 = new Intl.NumberFormat('it-IT', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const collator = new Intl.Collator('it', { sensitivity: 'base', numeric: true });

function fmtBytes(n) {
  if (n >= 1073741824) return nf1.format(n / 1073741824) + ' GB';
  return Math.max(1, Math.round(n / 1048576)) + ' MB';
}
function fmtRuntime(min) {
  if (!min) return '';
  const hours = Math.floor(min / 60), mins = min % 60;
  if (!hours) return `${mins} min`;
  return mins ? `${hours} h ${mins} min` : `${hours} h`;
}
function fmtEta(sec) {
  if (sec < 60) return 'meno di un minuto';
  const m = Math.round(sec / 60);
  if (m < 60) return m === 1 ? '1 minuto' : `${m} minuti`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}
function fmtSpeed(bytesPerSecond) {
  if (bytesPerSecond >= 1048576) return `${nf1.format(bytesPerSecond / 1048576)} MB al secondo`;
  return `${Math.max(1, Math.round(bytesPerSecond / 1024))} KB al secondo`;
}
const withYear = (title, year) => year ? `${title} (${year})` : title;
const withoutYear = title => (title || '').replace(/\s*\((?:19|20)\d{2}\)$/, '');
const norm = s => (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
const extOf = name => (name.match(/\.[^.]+$/) || [''])[0].toLowerCase();
const filmCount = n => n === 1 ? '1 film' : `${n} film`;
const sharePath = host => IS_MAC ? `smb://${host}/film` : `\\\\${host}\\film`;

const JUNK = /\b(2160p|1080p|720p|576p|480p|4k|uhd|hdr|hdr10|dv|bluray|blu ray|bdrip|brrip|dvdrip|dvd|webrip|web dl|webdl|web|hdtv|hdrip|x264|x265|h264|h265|hevc|avc|aac|ac3|eac3|dts|ddp|ita|eng|sub|subs|multi|dual|remux|proper|repack|extended|unrated|limited|internal)\b/i;

function guessFromFilename(name) {
  const bare = name.replace(/\.[a-z0-9]{2,4}$/i, '').replace(/[._]+/g, ' ');
  let base = bare, year = '';
  for (const m of bare.matchAll(/(^|[\s\-\[(])((?:19|20)\d{2})(?=$|[\s\-\])])/g)) {
    if (m.index + m[1].length > 0) { year = m[2]; base = bare.slice(0, m.index); }
  }
  const junk = base.search(JUNK);
  if (junk > 0) base = base.slice(0, junk);
  base = base.replace(/[\[(][^\])]*[\])]/g, ' ').replace(/[\s\-–]+$/, '').replace(/\s+/g, ' ').trim();
  return { title: base || bare.trim() || name, year };
}
function displayTitle(item) { return item.meta?.title || guessFromFilename(item.file).title; }

/* ---------- Comunicazione con il Pi ---------- */
async function api(url, opts = {}) {
  const init = { method: 'GET', headers: {} };
  if (opts.json !== undefined) {
    init.method = 'POST'; init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.json);
  } else if (opts.body !== undefined) {
    init.method = 'POST'; init.headers['Content-Type'] = opts.type || 'application/octet-stream'; init.body = opts.body;
  }
  let res;
  try { res = await fetch(url, init); }
  catch {
    const err = new Error('Il Pi non risponde. Controlla di essere collegato alla rete di casa o a Tailscale.');
    err.status = 0;
    throw err;
  }
  let data = {};
  try { data = await res.json(); } catch { /* risposta non JSON */ }
  if (!res.ok) {
    const err = new Error(data.error || `Il Pi ha risposto con un errore (${res.status}).`);
    err.status = res.status; err.data = data;
    throw err;
  }
  return data;
}

function sleep(ms) {
  return new Promise(resolve => {
    const done = () => { clearTimeout(timer); window.removeEventListener('online', done); resolve(); };
    const timer = setTimeout(done, ms);
    window.addEventListener('online', done);
  });
}

function toast(message) {
  const el = $('#toast');
  el.textContent = message;
  if (HAS_POPOVER) { if (el.matches(':popover-open')) el.hidePopover(); el.showPopover(); }
  else el.style.display = 'block';
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    if (HAS_POPOVER) { if (el.matches(':popover-open')) el.hidePopover(); }
    else el.style.display = 'none';
  }, 3600);
}

/* ---------- Libreria ---------- */
async function loadLibrary() {
  try {
    const [lib, settings, uploads] = await Promise.all([api('/api/library'), api('/api/settings'), api('/api/upload/sessions')]);
    state.items = lib.items; state.disk = lib.disk; state.settings = settings; state.sessions = uploads.sessions;
    $('#wallError').hidden = true;
  } catch (err) {
    $('#wallError').textContent = err.message;
    $('#wallError').hidden = false;
  }
  render();
  renderDock();
}

function visibleItems() {
  const q = norm(state.query.trim());
  const list = state.items.filter(it =>
    (!q || norm([displayTitle(it), it.meta?.original_title, it.file].join(' ')).includes(q)) &&
    (!state.genre || (it.meta?.genres || []).includes(state.genre)));
  const byTitle = (a, b) => collator.compare(displayTitle(a), displayTitle(b));
  const sorters = {
    added: (a, b) => b.added - a.added || byTitle(a, b),
    title: byTitle,
    year: (a, b) => (b.meta?.year || '0').localeCompare(a.meta?.year || '0') || byTitle(a, b),
  };
  return list.sort(sorters[state.sort] || sorters.added);
}

function render() {
  const count = state.items.length;
  const total = state.items.reduce((sum, it) => sum + it.size, 0);
  $('#tally').textContent = !state.settings ? '' : count === 0 ? 'Nessun film' : `${filmCount(count)}, ${fmtBytes(total)}`;
  renderSpace();
  renderGenres();
  $('#keyNotice').hidden = !state.settings || state.settings.has_key;
  renderMissing();
  const list = visibleItems();
  $('#wall').replaceChildren(...list.map(card));
  $('#empty').hidden = !(state.settings && count === 0);
  $('#noMatch').hidden = !(count > 0 && list.length === 0);
}

function renderSpace() {
  const d = state.disk;
  if (!d || !d.total) return;
  $('#space').hidden = false;
  const pct = n => `${(100 * n / d.total).toFixed(2)}%`;
  $('#mUsed').style.width = pct(d.used);
  $('#mReserve').style.width = pct(Math.min(d.reserve, d.free));
  $('#spaceMain').textContent = d.usable > 0 ? `Puoi aggiungere ancora ${fmtBytes(d.usable)} di film.` : 'Spazio finito: elimina un film per aggiungerne altri.';
  $('#spaceNote').textContent = `Scheda da ${fmtBytes(d.total)}, di cui ${fmtBytes(d.reserve)} restano sempre liberi per il sistema.`;
}

function renderGenres() {
  const genres = [...new Set(state.items.flatMap(it => it.meta?.genres || []))].sort((a, b) => collator.compare(a, b));
  if (state.genre && !genres.includes(state.genre)) state.genre = '';
  const select = $('#genre');
  select.replaceChildren(h('option', { value: '', text: 'Tutti i generi' }), ...genres.map(g => h('option', { value: g, text: g })));
  select.value = state.genre;
  $('#genreWrap').hidden = genres.length === 0;
}

function hiddenMissing() {
  try { return new Set(JSON.parse(store.get('senzaScheda', '[]'))); } catch { return new Set(); }
}

function renderMissing() {
  const box = $('#missingNotice');
  if (state.bulk) {
    box.hidden = false;
    $('#missingText').textContent = `Cerco le schede su TMDB: ${state.bulk.done} di ${state.bulk.total}…`;
    $('#missingBtn').hidden = $('#missingHide').hidden = true;
    return;
  }
  const ignored = hiddenMissing();
  const missing = state.items.filter(it => !it.meta && !ignored.has(it.path));
  box.hidden = !(state.settings?.has_key && missing.length);
  $('#missingText').textContent = missing.length === 1 ? '1 film non ha ancora una scheda.' : `${missing.length} film non hanno ancora una scheda.`;
  $('#missingBtn').hidden = $('#missingHide').hidden = false;
}

async function findAllInfo() {
  const ignored = hiddenMissing();
  const missing = state.items.filter(it => !it.meta && !ignored.has(it.path));
  if (!missing.length) return;
  state.bulk = { done: 0, total: missing.length, found: 0 };
  renderMissing();
  for (const item of missing) {
    const guess = guessFromFilename(item.file);
    try {
      const { results } = await api(`/api/search?q=${encodeURIComponent(guess.title)}&year=${encodeURIComponent(guess.year)}`);
      if (results.length) {
        const best = results[0];
        const saved = await api('/api/info', { json: { path: item.path, tmdb_id: best.id, poster: best.poster_path || null } });
        replaceItem(saved.item, false);
        state.bulk.found++;
      }
    } catch (err) {
      if (err.status === 412 || err.status === 0) { toast(err.message); break; }
    }
    state.bulk.done++;
    renderMissing();
  }
  const { found, total } = state.bulk;
  state.bulk = null;
  render();
  if (found === total) toast(total === 1 ? 'Scheda trovata' : `Schede trovate per tutti i ${total} film`);
  else toast(`Schede trovate per ${found} film su ${total}. Gli altri puoi sistemarli dalla loro scheda.`);
}

function blankPoster(title, year) {
  return h('span', { class: 'blank' }, h('span', { class: 'blank-title', text: title }), year ? h('span', { class: 'blank-year', text: year }) : null);
}

function coverImage(item, title, alt) {
  const img = h('img', { src: item.cover, alt, loading: 'lazy', decoding: 'async' });
  img.addEventListener('error', () => img.replaceWith(blankPoster(title, item.meta?.year)), { once: true });
  return img;
}

function card(item) {
  const meta = item.meta;
  const title = displayTitle(item);
  const poster = h('button', { class: 'poster', type: 'button', 'aria-label': `Apri la scheda di ${title}`, onclick: () => openDetail(item.path) },
    item.cover ? coverImage(item, title, '') : blankPoster(title, meta?.year));
  let facts;
  if (meta) {
    facts = h('p', { class: 'facts' },
      meta.year ? h('span', { class: 'year', text: meta.year }) : null,
      meta.runtime ? h('span', { text: fmtRuntime(meta.runtime) }) : null);
  } else {
    facts = h('p', { class: 'facts' }, h('button', { class: 'link-btn', type: 'button', onclick: () => openWizard('info', item), text: 'Trova info' }));
  }
  return h('li', { class: 'film' }, poster, h('h2', { class: 'film-title', text: title }), facts);
}

/* ---------- Scheda del film ---------- */
function openDetail(path) {
  const item = state.items.find(x => x.path === path);
  if (!item) return;
  const meta = item.meta || {};
  const title = displayTitle(item);
  const dlg = $('#detail');
  const facts = [];
  if (meta.year) facts.push(['Anno', meta.year]);
  if (meta.runtime) facts.push(['Durata', fmtRuntime(meta.runtime)]);
  if (meta.genres?.length) facts.push(['Genere', meta.genres.join(', ')]);
  if (meta.rating) facts.push(['Voto su TMDB', `${nf1.format(meta.rating)} su 10`]);
  const people = [];
  if (meta.directors?.length) people.push(['Regia', meta.directors.join(', ')]);
  if (meta.cast?.length) people.push(['Con', meta.cast.join(', ')]);
  let overview;
  if (meta.overview) overview = h('p', { class: 'overview', text: meta.overview });
  else if (item.meta) overview = h('p', { class: 'overview muted', text: 'Nessuna descrizione disponibile.' });
  else overview = h('p', { class: 'overview muted', text: 'Questo film non ha ancora una scheda. Usa «Trova info» per scaricare descrizione, durata e copertina.' });

  $('#detailBody').replaceChildren(
    h('figure', { class: 'detail-poster' }, item.cover ? coverImage(item, title, `Copertina di ${title}`) : blankPoster(title, meta.year)),
    h('div', { class: 'detail-info' },
      h('h2', { class: 'detail-title', id: 'detailTitle', text: title }),
      meta.original_title && meta.original_title !== title ? h('p', { class: 'orig', text: meta.original_title }) : null,
      facts.length ? h('dl', { class: 'facts-grid' }, facts.map(([k, v]) => h('div', null, h('dt', { text: k }), h('dd', { text: v })))) : null,
      meta.tagline ? h('p', { class: 'tagline', text: meta.tagline }) : null,
      overview,
      people.length ? h('dl', { class: 'people' }, people.map(([k, v]) => [h('dt', { text: k }), h('dd', { text: v })])) : null,
      h('p', { class: 'file-line', text: `${item.file}, ${fmtBytes(item.size)}` }),
      h('div', { class: 'actions' },
        h('a', { class: 'btn', href: item.url, target: '_blank', rel: 'noopener', text: 'Apri nel browser' }),
        h('a', { class: 'btn', href: item.url, download: item.file, text: 'Scarica' }),
        h('button', { type: 'button', onclick: () => { dlg.close(); openWizard('cover', item); }, text: 'Cambia copertina' }),
        h('button', { type: 'button', onclick: () => { dlg.close(); openWizard('info', item); }, text: item.meta ? 'Cerca di nuovo le info' : 'Trova info' }),
        h('button', { class: 'danger', type: 'button', onclick: () => confirmDelete(item), text: 'Elimina film' })),
      meta.source === 'tmdb' && meta.tmdb_url ? h('p', { class: 'source' }, 'Scheda presa da ', h('a', { href: meta.tmdb_url, target: '_blank', rel: 'noopener', text: 'TMDB' }), '.') : null));
  dlg.showModal();
  dlg.scrollTop = 0;
}

function confirmDelete(item) {
  const dlg = $('#confirm');
  $('#confirmTitle').textContent = `Eliminare ${displayTitle(item)}?`;
  $('#confirmText').textContent = `Il file ${item.file} verrà cancellato dal Pi insieme a copertina e scheda. Non si potrà recuperare.`;
  $('#confirmError').textContent = '';
  const yes = $('#confirmYes');
  yes.disabled = false;
  yes.onclick = async () => {
    yes.disabled = true;
    try {
      const res = await api('/api/delete', { json: { path: item.path } });
      state.disk = res.disk;
      state.items = state.items.filter(x => x.path !== item.path);
      dlg.close();
      $('#detail').close();
      render();
      toast('Film eliminato');
    } catch (err) {
      $('#confirmError').textContent = err.message;
      yes.disabled = false;
    }
  };
  dlg.showModal();
}

function replaceItem(item, rerender = true) {
  const i = state.items.findIndex(x => x.path === item.path);
  if (i >= 0) state.items[i] = item; else state.items.push(item);
  if (rerender) render();
}

/* ---------- Procedura guidata ---------- */
const MODES = {
  add: { title: 'Aggiungi un film', steps: [['file', 'File'], ['film', 'Film'], ['cover', 'Copertina']] },
  batch: { title: 'Aggiungi più film', steps: [['file', 'File'], ['batch', 'Schede']] },
  info: { title: 'Trova le info', steps: [['film', 'Film'], ['cover', 'Copertina']] },
  cover: { title: 'Cambia copertina', steps: [['cover', 'Copertina']] },
};
const wiz = {};

function resetWizard(mode, item) {
  if (wiz.customUrl) URL.revokeObjectURL(wiz.customUrl);
  Object.assign(wiz, {
    mode, item: item || null, step: MODES[mode].steps[0][0],
    files: [], file: null, fileError: '', batch: [], query: '', year: '',
    results: null, searching: false, searchError: '', loadingDetails: false, detailsError: '',
    meta: null, posters: [], loadingPosters: false, postersError: '',
    manual: null, showManual: false,
    cover: null, customBlob: null, customUrl: null, coverError: '', busy: false,
  });
}

function openWizard(mode, item) {
  resetWizard(mode, item);
  $('#wizTitle').textContent = item ? `${MODES[mode].title}: ${displayTitle(item)}` : MODES[mode].title;
  if (item) {
    const guess = guessFromFilename(item.file);
    wiz.query = item.meta?.title || guess.title;
    wiz.year = item.meta?.year || guess.year;
  }
  renderWizard();
  const dlg = $('#wizard');
  if (!dlg.open) dlg.showModal();
  if (mode === 'info') runSearch();
  if (mode === 'cover' && item.meta?.tmdb_id) loadPosters(item.meta.tmdb_id);
}

function closeWizard() {
  if (wiz.busy) { toast('Aspetta che finisca di salvare.'); return; }
  $('#wizard').close();
}

function renderWizard() {
  const steps = MODES[wiz.mode].steps;
  const current = steps.findIndex(s => s[0] === wiz.step);
  const list = $('#wizSteps');
  list.replaceChildren(...steps.map(([, label], i) => h('li', { class: i < current ? 'done' : null, 'aria-current': i === current ? 'step' : null, text: label })));
  list.hidden = steps.length < 2;
  ({ file: stepFile, film: stepFilm, cover: stepCover, batch: stepBatch })[wiz.step]();
}

function renderIfStep(step) {
  if ($('#wizard').open && wiz.step === step) renderWizard();
}

function goStep(step) {
  wiz.step = step;
  renderWizard();
  $('#wizBody').scrollTop = 0;
}

function setBody(...nodes) { $('#wizBody').replaceChildren(...nodes.flat(2).filter(Boolean)); }
function setFoot(...nodes) { $('#wizFoot').replaceChildren(...nodes.flat(2).filter(Boolean)); }
function backButton(step) { return h('button', { type: 'button', onclick: () => goStep(step), text: 'Indietro' }); }
function cancelButton() { return h('button', { type: 'button', onclick: closeWizard, text: 'Annulla' }); }

/* Passo 1: i file */
function stepFile() {
  const input = h('input', { type: 'file', id: 'fileInput', class: 'visually-hidden', multiple: true, accept: ACCEPT_VIDEO, onchange: e => pickFiles([...e.target.files]) });
  const many = wiz.files.length > 1;
  const total = wiz.files.reduce((sum, f) => sum + f.size, 0);
  const zone = h('label', { class: 'dropzone' + (wiz.files.length ? ' has-file' : ''), for: 'fileInput' },
    h('span', { class: 'dz-main', text: many ? `${wiz.files.length} film scelti` : wiz.file ? wiz.file.name : 'Scegli i film da caricare' }),
    h('span', { class: 'dz-sub', text: wiz.files.length
      ? `${fmtBytes(total)} in tutto. Clicca qui per cambiare la scelta.`
      : 'Uno o più file insieme, oppure trascinali qui. Vanno bene mkv, mp4, avi e gli altri formati video.' }));
  for (const type of ['dragenter', 'dragover']) zone.addEventListener(type, e => { e.preventDefault(); zone.classList.add('over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('over'));
  zone.addEventListener('drop', e => { e.preventDefault(); zone.classList.remove('over'); pickFiles([...e.dataTransfer.files]); });
  const room = roomForFilms();
  const below = wiz.fileError
    ? h('p', { class: 'form-error', role: 'alert', text: wiz.fileError })
    : state.disk ? h('p', { class: 'hint', text: `Puoi aggiungere ancora ${fmtBytes(room)} di film.` }) : null;
  setBody(input, zone, below);
  const next = () => {
    if (wiz.mode === 'batch') { goStep('batch'); runBatchMatch(); }
    else { goStep('film'); if (!wiz.results) runSearch(); }
  };
  setFoot(cancelButton(), h('button', { class: 'primary', type: 'button', disabled: !wiz.files.length || !!wiz.fileError, onclick: next, text: 'Avanti' }));
}

function roomForFilms() {
  const waiting = queue.jobs.filter(j => isActive(j) && !j.sid).reduce((sum, j) => sum + j.total, 0);
  return Math.max(0, (state.disk?.usable ?? 0) - waiting);
}

async function pickFiles(files) {
  files = files.filter(Boolean);
  if (!files.length) return;
  try { state.disk = (await api('/api/status')).disk; renderSpace(); } catch { /* uso il dato che ho */ }
  const notVideo = files.filter(f => !VIDEO_EXTS.includes(extOf(f.name)));
  const total = files.reduce((sum, f) => sum + f.size, 0);
  const room = roomForFilms();
  Object.assign(wiz, {
    mode: files.length > 1 ? 'batch' : 'add', files, file: files[0],
    results: null, meta: null, posters: [], manual: null, cover: null, showManual: false,
    batch: files.map(file => ({ file, guess: guessFromFilename(file.name), status: 'attesa', match: null, include: true })),
  });
  const guess = guessFromFilename(files[0].name);
  wiz.query = guess.title; wiz.year = guess.year;
  if (notVideo.length) {
    wiz.fileError = files.length === 1
      ? 'Questo file non sembra un video. Scegli un file mkv, mp4, avi o simile.'
      : `Questi file non sembrano video: ${notVideo.map(f => f.name).join(', ')}.`;
  } else if (state.disk && total > room) {
    wiz.fileError = `${files.length === 1 ? 'Questo film pesa' : 'Questi film pesano in tutto'} ${fmtBytes(total)}, ma puoi aggiungere al massimo ${fmtBytes(room)}. Elimina un film per fare spazio.`;
  } else {
    wiz.fileError = '';
  }
  $('#wizTitle').textContent = MODES[wiz.mode].title;
  renderWizard();
}

/* Passo 2: quale film è */
function stepFilm() {
  const nodes = [];
  if (!state.settings?.has_key) {
    nodes.push(h('div', { class: 'notice inline' },
      h('p', { text: 'Per cercare il film su TMDB serve la chiave gratuita. Inseriscila adesso, oppure compila la scheda a mano qui sotto.' }),
      h('button', { class: 'primary', type: 'button', onclick: openSettings, text: 'Inserisci la chiave' })));
  } else {
    const qInput = h('input', { type: 'text', id: 'wizQuery', value: wiz.query, required: true, autocomplete: 'off' });
    const yInput = h('input', { type: 'text', id: 'wizYear', value: wiz.year, inputmode: 'numeric', pattern: '\\d{4}', maxlength: '4', placeholder: 'es. 1999' });
    nodes.push(h('form', { class: 'search-form', onsubmit: e => { e.preventDefault(); wiz.query = qInput.value; wiz.year = yInput.value; runSearch(); } },
      h('label', { class: 'grow' }, h('span', { class: 'lbl', text: 'Titolo' }), qInput),
      h('label', { class: 'short' }, h('span', { class: 'lbl', text: 'Anno' }), yInput),
      h('button', { class: 'primary', type: 'submit', disabled: wiz.searching, text: wiz.searching ? 'Cerco…' : 'Cerca' })));
    if (wiz.searchError) nodes.push(h('p', { class: 'form-error', role: 'alert', text: wiz.searchError }));
    if (wiz.detailsError) nodes.push(h('p', { class: 'form-error', role: 'alert', text: wiz.detailsError }));
    if (wiz.results && !wiz.results.length) {
      nodes.push(h('p', { class: 'hint', text: 'Nessun film trovato. Prova a scrivere il titolo in un altro modo o in inglese, oppure togli l’anno.' }));
    } else if (wiz.results) {
      nodes.push(h('p', { class: 'hint', text: 'Scegli il film giusto.' }));
      nodes.push(h('ul', { class: 'results' }, wiz.results.map(r => h('li', null,
        h('button', { class: 'result' + (wiz.meta?.tmdb_id === r.id ? ' picked' : ''), type: 'button', disabled: wiz.loadingDetails, onclick: () => chooseResult(r) },
          r.poster ? h('img', { src: r.poster, alt: '', loading: 'lazy' }) : h('span', { class: 'thumb-blank' }),
          h('span', { class: 'r-text' },
            h('span', { class: 'r-title', text: withYear(r.title, r.year) }),
            r.original_title && r.original_title !== r.title ? h('span', { class: 'r-sub', text: r.original_title }) : null,
            r.overview ? h('span', { class: 'r-over', text: r.overview }) : null))))));
    }
  }
  nodes.push(manualSection());
  setBody(nodes);
  setFoot(wiz.mode === 'add' ? backButton('file') : cancelButton(), wiz.loadingDetails ? h('span', { class: 'hint', text: 'Scarico la scheda del film…' }) : null);
}

function manualSection() {
  if (!wiz.showManual) {
    return h('p', { class: 'manual-link' }, h('button', { class: 'link-btn', type: 'button', onclick: () => { wiz.showManual = true; renderWizard(); $('#manTitle')?.focus(); }, text: 'Non lo trovi? Compila la scheda a mano' }));
  }
  const m = wiz.manual || { title: wiz.query, year: wiz.year, runtime: '', overview: '' };
  const form = h('form', { class: 'manual' },
    h('h3', { text: 'Scheda a mano' }),
    h('div', { class: 'row' },
      h('label', { class: 'grow' }, h('span', { class: 'lbl', text: 'Titolo' }), h('input', { name: 'title', id: 'manTitle', required: true, value: m.title })),
      h('label', { class: 'short' }, h('span', { class: 'lbl', text: 'Anno' }), h('input', { name: 'year', value: m.year, inputmode: 'numeric', pattern: '\\d{4}', maxlength: '4' })),
      h('label', { class: 'short' }, h('span', { class: 'lbl', text: 'Minuti' }), h('input', { name: 'runtime', value: m.runtime, inputmode: 'numeric', pattern: '\\d{1,3}', maxlength: '3' }))),
    h('label', null, h('span', { class: 'lbl', text: 'Descrizione' }), h('textarea', { name: 'overview', rows: '4', text: m.overview })),
    h('div', { class: 'actions' }, h('button', { class: 'primary', type: 'submit', text: 'Usa questa scheda' })));
  form.addEventListener('submit', e => {
    e.preventDefault();
    const fd = new FormData(form);
    wiz.manual = { title: fd.get('title').trim(), year: fd.get('year').trim(), runtime: fd.get('runtime').trim(), overview: fd.get('overview').trim() };
    wiz.meta = null; wiz.posters = [];
    wiz.cover = wiz.mode === 'info' && wiz.item?.cover ? { kind: 'keep' } : null;
    goStep('cover');
  });
  return form;
}

async function runSearch() {
  if (!state.settings?.has_key || !wiz.query.trim()) { renderWizard(); return; }
  wiz.searching = true; wiz.searchError = ''; wiz.detailsError = '';
  renderWizard();
  try { wiz.results = (await api(`/api/search?q=${encodeURIComponent(wiz.query)}&year=${encodeURIComponent(wiz.year || '')}`)).results; }
  catch (err) { wiz.searchError = err.message; wiz.results = null; }
  wiz.searching = false;
  renderIfStep('film');
}

async function chooseResult(result) {
  wiz.loadingDetails = true; wiz.detailsError = '';
  renderWizard();
  try {
    const data = await api('/api/tmdb?id=' + encodeURIComponent(result.id));
    wiz.meta = data.meta; wiz.posters = data.posters; wiz.manual = null;
    wiz.cover = data.posters.length ? { kind: 'tmdb', poster: data.posters[0].path } : (wiz.mode === 'info' && wiz.item?.cover ? { kind: 'keep' } : null);
    wiz.loadingDetails = false;
    goStep('cover');
  } catch (err) {
    wiz.loadingDetails = false; wiz.detailsError = err.message;
    renderWizard();
  }
}

/* Passo 3: la copertina */
function langLabel(code) {
  return ({ it: 'In italiano', en: 'In inglese', '': 'Senza scritte' })[code] ?? 'Altra lingua';
}
const sameCover = (a, b) => !!a && !!b && a.kind === b.kind && (a.poster || '') === (b.poster || '');

function coverOption(value, src, caption) {
  return h('label', { class: 'cover-opt' },
    h('input', { type: 'radio', name: 'cover', checked: sameCover(wiz.cover, value), onchange: () => { wiz.cover = value; renderWizard(); } }),
    h('span', { class: 'frame' }, h('img', { src, alt: '', loading: 'lazy' })),
    h('span', { class: 'cap', text: caption }));
}

function stepCover() {
  const nodes = [];
  if (wiz.meta) nodes.push(h('p', { class: 'picked-summary' }, 'Film scelto: ', h('strong', { text: withYear(wiz.meta.title, wiz.meta.year) }), wiz.meta.runtime ? `, ${fmtRuntime(wiz.meta.runtime)}.` : '.'));
  else if (wiz.manual) nodes.push(h('p', { class: 'picked-summary' }, 'Scheda a mano: ', h('strong', { text: withYear(wiz.manual.title, wiz.manual.year) }), '.'));
  nodes.push(h('p', { class: 'hint', text: wiz.mode === 'cover' ? 'Scegli la nuova copertina.' : 'Scegli la copertina. La vedrai qui e sulla TV.' }));
  if (wiz.loadingPosters) nodes.push(h('p', { class: 'hint', text: 'Carico le locandine da TMDB…' }));
  if (wiz.postersError) nodes.push(h('p', { class: 'form-error', text: wiz.postersError }));

  const options = [];
  if (wiz.mode === 'info' && wiz.item?.cover) options.push(coverOption({ kind: 'keep' }, wiz.item.cover, 'Quella attuale'));
  for (const p of wiz.posters) options.push(coverOption({ kind: 'tmdb', poster: p.path }, p.thumb, p.main ? 'Consigliata' : langLabel(p.lang)));
  if (wiz.customUrl) options.push(coverOption({ kind: 'custom' }, wiz.customUrl, 'La tua immagine'));
  const upload = h('label', { class: 'cover-upload' },
    h('input', { type: 'file', accept: 'image/*', class: 'visually-hidden', onchange: e => pickCustom(e.target.files[0]) }),
    h('span', { class: 'frame add', text: '+' }),
    h('span', { class: 'cap', text: wiz.customUrl ? 'Scegli un’altra immagine' : 'Usa un’immagine tua' }));
  nodes.push(h('div', { class: 'covers' }, options, upload));

  if (wiz.mode === 'cover' && !wiz.item?.meta?.tmdb_id) nodes.push(h('p', { class: 'hint', text: 'Per scegliere tra le locandine ufficiali, prima usa «Trova info» dalla scheda del film.' }));
  else if (wiz.mode === 'cover' && !state.settings?.has_key) nodes.push(h('p', { class: 'hint', text: 'Per vedere le locandine ufficiali serve la chiave TMDB nelle impostazioni.' }));
  if (wiz.mode === 'add') nodes.push(h('p', { class: 'hint', text: 'Il film si carica in sottofondo: intanto puoi continuare a usare la videoteca. Se la connessione cade, riparte da dove si era fermato.' }));
  if (wiz.coverError) nodes.push(h('p', { class: 'form-error', role: 'alert', text: wiz.coverError }));
  setBody(nodes);

  const action = { add: ['Carica film', enqueueSingle], info: ['Salva scheda', runSaveInfo], cover: ['Salva copertina', runSaveCover] }[wiz.mode];
  const back = wiz.mode === 'cover' ? cancelButton() : backButton('film');
  const blocked = wiz.busy || (wiz.mode === 'cover' && (!wiz.cover || wiz.cover.kind === 'keep'));
  setFoot(back, h('button', { class: 'primary', type: 'button', disabled: blocked, onclick: action[1], text: wiz.busy ? 'Salvo…' : action[0] }));
}

async function toJpeg(file) {
  const url = URL.createObjectURL(file);
  try {
    const img = await new Promise((resolve, reject) => {
      const el = new Image();
      el.onload = () => resolve(el);
      el.onerror = () => reject(new Error('Non riesco a leggere questa immagine. Prova con un file JPG o PNG.'));
      el.src = url;
    });
    const scale = Math.min(1, 1200 / img.naturalHeight);
    const width = Math.max(1, Math.round(img.naturalWidth * scale));
    const height = Math.max(1, Math.round(img.naturalHeight * scale));
    const canvas = document.createElement('canvas');
    canvas.width = width; canvas.height = height;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#000'; ctx.fillRect(0, 0, width, height);
    ctx.drawImage(img, 0, 0, width, height);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.88));
    if (!blob) throw new Error('Non riesco a convertire questa immagine.');
    return blob;
  } finally {
    URL.revokeObjectURL(url);
  }
}

async function pickCustom(file) {
  if (!file) return;
  wiz.coverError = '';
  try {
    const blob = await toJpeg(file);
    if (wiz.customUrl) URL.revokeObjectURL(wiz.customUrl);
    wiz.customBlob = blob;
    wiz.customUrl = URL.createObjectURL(blob);
    wiz.cover = { kind: 'custom' };
  } catch (err) {
    wiz.coverError = err.message;
  }
  renderWizard();
}

async function loadPosters(id) {
  if (!state.settings?.has_key) return;
  wiz.loadingPosters = true;
  renderWizard();
  try { wiz.posters = (await api('/api/tmdb?id=' + encodeURIComponent(id))).posters; }
  catch (err) { wiz.postersError = err.message; }
  wiz.loadingPosters = false;
  renderIfStep('cover');
}

function chosenInfo() {
  if (wiz.meta) return { tmdb_id: wiz.meta.tmdb_id, poster: wiz.cover?.kind === 'tmdb' ? wiz.cover.poster : null };
  if (wiz.manual) return { manual: wiz.manual };
  return null;
}

async function uploadCustomCover(path) {
  return (await api('/api/cover?path=' + encodeURIComponent(path), { body: wiz.customBlob, type: 'image/jpeg' })).item;
}

async function runSaveInfo() {
  wiz.busy = true; wiz.coverError = '';
  renderWizard();
  try {
    let item = (await api('/api/info', { json: { path: wiz.item.path, ...chosenInfo() } })).item;
    if (wiz.cover?.kind === 'custom') item = await uploadCustomCover(item.path);
    replaceItem(item);
    wiz.busy = false;
    closeWizard();
    toast('Scheda salvata');
  } catch (err) {
    wiz.busy = false; wiz.coverError = err.message;
    renderWizard();
  }
}

async function runSaveCover() {
  if (!wiz.cover || wiz.cover.kind === 'keep') return;
  wiz.busy = true; wiz.coverError = '';
  renderWizard();
  try {
    const item = wiz.cover.kind === 'tmdb'
      ? (await api('/api/cover', { json: { path: wiz.item.path, poster: wiz.cover.poster } })).item
      : await uploadCustomCover(wiz.item.path);
    replaceItem(item);
    wiz.busy = false;
    closeWizard();
    toast('Copertina salvata');
  } catch (err) {
    wiz.busy = false; wiz.coverError = err.message;
    renderWizard();
  }
}

function enqueueSingle() {
  const guess = guessFromFilename(wiz.file.name);
  const title = wiz.meta ? wiz.meta.title : wiz.manual?.title || guess.title;
  const year = wiz.meta ? wiz.meta.year : wiz.manual ? wiz.manual.year : guess.year;
  enqueue({
    file: wiz.file, title: withYear(title, year), label: title, info: chosenInfo(),
    coverBlob: wiz.cover?.kind === 'custom' ? wiz.customBlob : null,
  });
  closeWizard();
  toast(`${title}: caricamento avviato`);
}

/* Più film insieme */
function stepBatch() {
  const nodes = [];
  if (!state.settings?.has_key) {
    nodes.push(h('div', { class: 'notice inline' },
      h('p', { text: 'Senza la chiave TMDB i film vengono caricati senza scheda. Potrai aggiungerle dopo con «Trova le info per tutti».' }),
      h('button', { class: 'primary', type: 'button', onclick: openSettings, text: 'Inserisci la chiave' })));
  } else {
    nodes.push(h('p', { class: 'hint', text: 'Ho cercato ogni film su TMDB e ho scelto il risultato più probabile con la sua copertina. Se una scheda è sbagliata, la correggi dopo con «Cerca di nuovo le info».' }));
  }
  nodes.push(h('ul', { class: 'batch' }, wiz.batch.map(batchRow)));
  setBody(nodes);
  const count = wiz.batch.filter(r => r.include).length;
  const searching = wiz.batch.some(r => r.status === 'cerco' || r.status === 'attesa') && state.settings?.has_key;
  setFoot(backButton('file'), h('button', { class: 'primary', type: 'button', disabled: !count || searching, onclick: enqueueBatch, text: searching ? 'Cerco le schede…' : `Carica ${filmCount(count)}` }));
}

function batchRow(row) {
  const m = row.match;
  const title = m ? withYear(m.title, m.year) : withYear(row.guess.title, row.guess.year);
  let note;
  if (row.status === 'cerco' || (row.status === 'attesa' && state.settings?.has_key)) note = 'Cerco su TMDB…';
  else if (m) note = m.original_title && m.original_title !== m.title ? m.original_title : 'Scheda trovata su TMDB';
  else if (row.status === 'errore') note = row.error;
  else note = state.settings?.has_key ? 'Nessuna scheda trovata: lo carico senza' : 'Lo carico senza scheda';
  return h('li', { class: 'batch-row' + (row.include ? '' : ' off') },
    h('label', { class: 'batch-check' },
      h('input', { type: 'checkbox', checked: row.include, onchange: e => { row.include = e.target.checked; renderWizard(); } }),
      h('span', { class: 'visually-hidden', text: `Carica ${row.file.name}` })),
    m?.poster ? h('img', { src: m.poster, alt: '', loading: 'lazy' }) : h('span', { class: 'thumb-blank' }),
    h('span', { class: 'r-text' },
      h('span', { class: 'r-title', text: title }),
      h('span', { class: 'r-sub', text: note }),
      h('span', { class: 'r-file', text: `${row.file.name}, ${fmtBytes(row.file.size)}` })));
}

async function runBatchMatch() {
  if (!state.settings?.has_key) { renderIfStep('batch'); return; }
  const rows = wiz.batch;
  for (const row of rows) {
    if (row.status === 'fatto') continue;
    row.status = 'cerco';
    renderIfStep('batch');
    try {
      const { results } = await api(`/api/search?q=${encodeURIComponent(row.guess.title)}&year=${encodeURIComponent(row.guess.year)}`);
      row.match = results[0] || null;
      row.status = 'fatto';
    } catch (err) {
      row.status = 'errore'; row.error = err.message;
    }
    renderIfStep('batch');
  }
}

function enqueueBatch() {
  const rows = wiz.batch.filter(r => r.include);
  for (const row of rows) {
    const m = row.match;
    enqueue({
      file: row.file,
      title: m ? withYear(m.title, m.year) : withYear(row.guess.title, row.guess.year),
      label: m ? m.title : row.guess.title,
      info: m ? { tmdb_id: m.id, poster: m.poster_path || null } : null,
      coverBlob: null,
    });
  }
  closeWizard();
  toast(`${filmCount(rows.length)} in coda per il caricamento`);
}

/* ---------- Caricamenti in sottofondo ---------- */
const queue = { jobs: [], running: false, collapsed: false };
let jobSeq = 0;
const isActive = job => ['attesa', 'carico', 'riprovo', 'finisco'].includes(job.state);

function enqueue(spec) {
  queue.jobs.push({
    key: ++jobSeq, state: 'attesa', phase: 'carico', sid: spec.resume || null,
    loaded: 0, current: 0, total: spec.file.size, speed: 0, samples: [],
    error: '', warning: '', item: null, coverSent: false, ...spec,
  });
  renderDock();
  runQueue();
}

async function runQueue() {
  if (queue.running) return;
  queue.running = true;
  try {
    let job;
    while ((job = queue.jobs.find(j => j.state === 'attesa'))) await runJob(job);
  } finally {
    queue.running = false;
    renderDock();
  }
}

async function runJob(job) {
  job.state = 'carico'; job.phase = 'carico'; job.error = ''; job.samples = [];
  renderDock();
  try {
    const session = await retrying(job, () => api('/api/upload/start', { json: {
      name: job.file.name, size: job.file.size, modified: job.file.lastModified,
      title: job.title, info: job.info, resume: job.sid || undefined,
    } }));
    job.sid = session.id; job.loaded = job.current = session.offset;
    state.sessions = state.sessions.filter(s => s.id !== session.id);
    if (job.coverBlob && !job.coverSent) {
      await retrying(job, () => api('/api/upload/cover?id=' + session.id, { body: job.coverBlob, type: 'image/jpeg' }));
      job.coverSent = true;
    }
    renderDock();
    let result = null;
    for (let round = 0; !result && round < 3; round++) {
      await sendPieces(job);
      try {
        result = await finishJob(job);
      } catch (err) {
        if (err.status === 409 && typeof err.data?.offset === 'number') { job.loaded = job.current = err.data.offset; continue; }
        throw err;
      }
    }
    if (!result) throw new Error('Il caricamento non si chiude. Riprova tra poco.');
    job.state = 'fatto'; job.warning = result.warning || ''; job.item = result.item;
    if (result.item) job.label = displayTitle(result.item);
    renderDock();
    toast(job.warning ? `${job.label} è caricato, ma senza scheda` : `${job.label} è nella videoteca`);
    loadLibrary();
  } catch (err) {
    if (job.state === 'annullato') return;
    job.state = 'errore'; job.error = err.message;
    renderDock();
  }
}

async function retrying(job, fn) {
  let failures = 0;
  for (;;) {
    if (job.state === 'annullato') throw new Error('Caricamento annullato.');
    try {
      const result = await fn();
      if (job.state === 'riprovo') { job.state = job.phase; renderDock(); }
      return result;
    } catch (err) {
      if (job.state === 'annullato') throw err;
      const status = err.status ?? 0;
      const fatal = status === 507 || (status && status < 500 && status !== 408 && status !== 429);
      if (fatal) throw err;
      failures++;
      if (failures > 40) throw new Error('Il Pi non risponde da troppo tempo. Premi «Riprova» quando la connessione è tornata: il caricamento ripartirà da dove si è fermato.');
      job.state = 'riprovo';
      renderDock();
      await sleep(Math.min(30000, 1000 * 2 ** Math.min(failures, 5)));
    }
  }
}

async function sendPieces(job) {
  job.phase = 'carico';
  while (job.loaded < job.file.size) {
    const start = job.loaded;
    const end = Math.min(start + PIECE, job.file.size);
    try {
      const res = await retrying(job, () => putPiece(job, start, end));
      job.loaded = job.current = res.offset;
    } catch (err) {
      if (err.status === 409 && typeof err.data?.offset === 'number') { job.loaded = job.current = err.data.offset; continue; }
      throw err;
    }
    fillJobView(job);
  }
}

function putPiece(job, start, end) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    job.xhr = xhr;
    let lastMove = Date.now(), stalled = false;
    const watchdog = setInterval(() => {
      if (Date.now() - lastMove > 60000) { stalled = true; xhr.abort(); }
    }, 5000);
    const fail = (message, status, data) => {
      clearInterval(watchdog);
      const err = new Error(message);
      err.status = status; err.data = data;
      reject(err);
    };
    xhr.open('POST', `/api/upload/chunk?id=${encodeURIComponent(job.sid)}&offset=${start}`);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = e => {
      lastMove = Date.now();
      job.current = start + e.loaded;
      tickSpeed(job);
      fillJobView(job);
    };
    xhr.onload = () => {
      clearInterval(watchdog);
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* niente */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else fail(data.error || `Il Pi ha risposto con un errore (${xhr.status}).`, xhr.status, data);
    };
    xhr.onerror = () => fail('Connessione interrotta.', 0);
    xhr.onabort = () => fail(stalled ? 'Connessione bloccata.' : 'Caricamento annullato.', 0);
    xhr.send(job.file.slice(start, end));
  });
}

async function finishJob(job) {
  job.phase = 'finisco';
  job.state = 'finisco';
  renderDock();
  let attempts = 0;
  try {
    return await retrying(job, () => { attempts++; return api('/api/upload/finish?id=' + encodeURIComponent(job.sid), { json: {} }); });
  } catch (err) {
    // Se la risposta di prima si era persa, il film è già nella videoteca.
    if (err.status === 404 && attempts > 1) return { item: null, warning: '' };
    throw err;
  }
}

function tickSpeed(job) {
  const now = performance.now();
  job.samples.push([now, job.current]);
  while (job.samples.length > 2 && now - job.samples[0][0] > 15000) job.samples.shift();
  const [t0, b0] = job.samples[0];
  job.speed = now - t0 > 1500 ? (job.current - b0) / ((now - t0) / 1000) : 0;
}

async function cancelJob(job) {
  job.state = 'annullato';
  job.xhr?.abort();
  renderDock();
  if (job.sid) {
    try { const res = await api('/api/upload/cancel?id=' + encodeURIComponent(job.sid), { json: {} }); state.disk = res.disk; renderSpace(); }
    catch { /* lo pulirà il Pi */ }
  }
  toast('Caricamento annullato');
}

function retryJob(job) {
  job.state = 'attesa'; job.error = '';
  renderDock();
  runQueue();
}

function removeJob(job) {
  queue.jobs = queue.jobs.filter(j => j !== job);
  renderDock();
}

function resumeSession(session, file) {
  if (!file) return;
  if (file.name !== session.name || file.size !== session.size) {
    toast(`Scegli lo stesso file di prima: ${session.name}`);
    return;
  }
  enqueue({ file, title: session.title, label: withoutYear(session.title), info: session.info, coverBlob: null, resume: session.id });
}

async function discardSession(session) {
  try {
    const res = await api('/api/upload/cancel?id=' + encodeURIComponent(session.id), { json: {} });
    state.disk = res.disk;
    state.sessions = state.sessions.filter(s => s.id !== session.id);
    renderSpace();
    renderDock();
    toast('Caricamento eliminato');
  } catch (err) {
    toast(err.message);
  }
}

function jobStatus(job) {
  const done = Math.max(job.current, job.loaded);
  const pct = job.total ? Math.floor(100 * done / job.total) : 0;
  switch (job.state) {
    case 'attesa': return 'In coda';
    case 'carico': {
      let text = `${fmtBytes(done)} di ${fmtBytes(job.total)} (${pct}%)`;
      if (job.speed > 0 && done < job.total) text += `, ${fmtSpeed(job.speed)}, mancano circa ${fmtEta((job.total - done) / job.speed)}`;
      return text;
    }
    case 'riprovo': return `Connessione persa: riprovo da solo. Sul Pi c'è già il ${pct}%.`;
    case 'finisco': return 'Salvo scheda e copertina…';
    case 'fatto': return job.warning || 'Caricato';
    case 'errore': return job.error;
    default: return 'Annullato';
  }
}

function fillJobView(job) {
  if (!job.view) return;
  const done = job.state === 'fatto' ? job.total : Math.max(job.current, job.loaded);
  job.view.bar.style.width = `${job.total ? (100 * done / job.total).toFixed(1) : 0}%`;
  job.view.status.textContent = jobStatus(job);
  renderDockSummary();
}

function jobView(job) {
  const bar = h('span');
  const status = h('p', { class: 'job-status' });
  const actions = [];
  if (isActive(job) && job.state !== 'finisco') actions.push(h('button', { class: 'small', type: 'button', onclick: () => cancelJob(job), text: 'Annulla' }));
  if (job.state === 'errore') {
    actions.push(h('button', { class: 'small primary', type: 'button', onclick: () => retryJob(job), text: 'Riprova' }));
    actions.push(h('button', { class: 'small', type: 'button', onclick: () => cancelJob(job).then(() => removeJob(job)), text: 'Annulla' }));
  }
  if (job.state === 'fatto' && job.item) actions.push(h('button', { class: 'small', type: 'button', onclick: () => openDetail(job.item.path), text: 'Apri la scheda' }));
  if (job.state === 'fatto' || job.state === 'annullato') actions.push(h('button', { class: 'small', type: 'button', onclick: () => removeJob(job), text: 'Togli' }));
  const li = h('li', { class: `job ${job.state}` },
    h('p', { class: 'job-title', text: job.label }),
    h('div', { class: 'progress thin' }, bar),
    status,
    actions.length ? h('div', { class: 'job-actions' }, actions) : null);
  job.view = { bar, status };
  fillJobView(job);
  return li;
}

function sessionView(session) {
  const pct = session.size ? Math.floor(100 * session.offset / session.size) : 0;
  const input = h('input', { type: 'file', class: 'visually-hidden', accept: ACCEPT_VIDEO, onchange: e => resumeSession(session, e.target.files[0]) });
  return h('li', { class: 'job interrotto' },
    h('p', { class: 'job-title', text: withoutYear(session.title) || session.name }),
    h('div', { class: 'progress thin' }, h('span', { style: `width: ${pct}%` })),
    h('p', { class: 'job-status', text: `Interrotto al ${pct}%. Per riprenderlo da lì scegli di nuovo il file ${session.name}.` }),
    h('div', { class: 'job-actions' },
      h('label', { class: 'btn small primary' }, input, 'Riprendi'),
      h('button', { class: 'small', type: 'button', onclick: () => discardSession(session), text: 'Elimina' })));
}

function interruptedSessions() {
  const mine = new Set(queue.jobs.map(j => j.sid).filter(Boolean));
  return state.sessions.filter(s => !mine.has(s.id));
}

function renderDockSummary() {
  const running = queue.jobs.filter(isActive);
  const interrupted = interruptedSessions();
  let text;
  if (running.length) {
    const current = running.find(j => j.state !== 'attesa') || running[0];
    const done = Math.max(current.current, current.loaded);
    const pct = current.total ? Math.floor(100 * done / current.total) : 0;
    text = running.length === 1 ? `${current.label}: ${pct}%` : `${filmCount(running.length)} da caricare, ora ${current.label} al ${pct}%`;
  } else if (interrupted.length) {
    text = interrupted.length === 1 ? '1 caricamento interrotto' : `${interrupted.length} caricamenti interrotti`;
  } else {
    text = 'Tutto caricato';
  }
  $('#dockSum').textContent = text;
}

let guarding = false;
function blockUnload(e) { e.preventDefault(); e.returnValue = ''; }

function renderDock() {
  const dock = $('#dock');
  const interrupted = interruptedSessions();
  const visible = queue.jobs.length + interrupted.length > 0;
  dock.hidden = !visible;
  document.body.classList.toggle('has-dock', visible);
  const active = queue.jobs.some(isActive);
  if (active !== guarding) {
    guarding = active;
    if (active) window.addEventListener('beforeunload', blockUnload);
    else window.removeEventListener('beforeunload', blockUnload);
  }
  if (!visible) return;
  $('#dockList').replaceChildren(...queue.jobs.map(jobView), ...interrupted.map(sessionView));
  dock.classList.toggle('collapsed', queue.collapsed);
  $('#dockToggle').textContent = queue.collapsed ? 'Mostra' : 'Riduci';
  $('#dockToggle').setAttribute('aria-expanded', String(!queue.collapsed));
  renderDockSummary();
}

/* ---------- Impostazioni ---------- */
async function openSettings() {
  try { state.settings = await api('/api/settings'); } catch { /* mostro quello che ho */ }
  fillSettings();
  $('#keyInput').value = '';
  $('#pwInput').value = '';
  $('#settings').showModal();
}

function fillSettings() {
  const s = state.settings || {};
  const port = s.port || location.port || '80';
  const url = host => `http://${host}:${port}`;
  const addresses = s.addresses || [];
  const local = addresses.filter(a => !a.tailscale);
  const remote = addresses.filter(a => a.tailscale);
  const rows = [];
  for (const a of local) rows.push(['In casa', url(a.ip)]);
  for (const a of remote) rows.push(['Fuori casa', url(a.ip)]);
  if (remote.length && s.tailscale_name) rows.push(['Fuori casa, con il nome', url(s.tailscale_name)]);
  if (!rows.length) rows.push(['Questa pagina', location.origin]);
  rows.push(['Cartella condivisa', sharePath(location.hostname)]);
  $('#addrList').replaceChildren(...rows.map(([k, v]) => h('div', null, h('dt', { text: k }), h('dd', null, v.startsWith('http') ? h('a', { href: v, text: v }) : v))));
  $('#remoteHint').textContent = remote.length
    ? 'Gli indirizzi per fuori casa funzionano da qualsiasi rete, sui dispositivi con Tailscale acceso e collegato al tuo account. Quello con i numeri funziona sempre; salvalo nei preferiti.'
    : 'Per usarla anche fuori casa serve Tailscale, una rete privata gratuita: installalo sul Pi e sui dispositivi da cui apri la videoteca, e qui comparirà l’indirizzo da usare.';
  $('#keyStatus').textContent = s.has_key ? `Chiave salvata, finisce con ${s.key_end}. Per cambiarla incollane una nuova.` : 'Nessuna chiave salvata.';
  $('#pwStatus').textContent = s.has_password ? 'La videoteca è protetta da password.' : 'Senza password: chiunque sia sulla rete di casa può aprire la videoteca.';
  $('#pwRemove').hidden = !s.has_password;
  $('#aboutText').textContent = `Videoteca ${s.version || ''}. I film stanno nella cartella ${s.library || '/mnt/film'} del Pi, e ${s.reserve_gb ?? 2} GB della scheda restano sempre liberi per il sistema.`;
  $('#keyError').textContent = '';
  $('#pwError').textContent = '';
}

$('#keyForm').addEventListener('submit', async e => {
  e.preventDefault();
  const key = $('#keyInput').value.trim();
  if (!key) { $('#keyError').textContent = 'Incolla la chiave prima di salvarla.'; return; }
  const btn = e.submitter || $('#keyForm button');
  btn.disabled = true; btn.textContent = 'Controllo la chiave…';
  try {
    state.settings = await api('/api/settings', { json: { tmdb_key: key } });
    $('#keyInput').value = '';
    fillSettings();
    render();
    toast('Chiave salvata');
    if ($('#wizard').open && wiz.step === 'film') runSearch();
    if ($('#wizard').open && wiz.step === 'batch') runBatchMatch();
  } catch (err) {
    $('#keyError').textContent = err.message;
  } finally {
    btn.disabled = false; btn.textContent = 'Salva chiave';
  }
});

$('#pwForm').addEventListener('submit', async e => {
  e.preventDefault();
  const password = $('#pwInput').value;
  if (password.length < 4) { $('#pwError').textContent = 'Usa una password di almeno 4 caratteri.'; return; }
  try {
    state.settings = await api('/api/settings', { json: { password } });
    toast('Password salvata. Il browser te la chiederà tra un attimo.');
    if (!queue.jobs.some(isActive)) setTimeout(() => location.reload(), 1600);
  } catch (err) {
    $('#pwError').textContent = err.message;
  }
});

$('#pwRemove').addEventListener('click', async () => {
  try {
    state.settings = await api('/api/settings', { json: { password: '' } });
    fillSettings();
    toast('Password tolta');
  } catch (err) {
    $('#pwError').textContent = err.message;
  }
});

/* ---------- Avvio ---------- */
if (!HAS_POPOVER) $('#toast').style.display = 'none';
document.addEventListener('click', e => {
  const closer = e.target.closest('[data-close]');
  if (closer) closer.closest('dialog').close();
});
document.addEventListener('keydown', e => {
  if (e.key === '/' && !e.target.closest('input, textarea, select') && !document.querySelector('dialog[open]')) {
    e.preventDefault();
    $('#q').focus();
  }
});
$('#detail').addEventListener('click', e => { if (e.target === e.currentTarget) e.currentTarget.close(); });
$('#wizard').addEventListener('cancel', e => { if (wiz.busy) { e.preventDefault(); toast('Aspetta che finisca di salvare.'); } });
$('#wizard').addEventListener('close', () => { if (wiz.customUrl) { URL.revokeObjectURL(wiz.customUrl); wiz.customUrl = null; } });
$('#wizClose').addEventListener('click', closeWizard);
$('#addBtn').addEventListener('click', () => openWizard('add'));
$('#emptyAdd').addEventListener('click', () => openWizard('add'));
$('#settingsBtn').addEventListener('click', openSettings);
$('#keyNoticeBtn').addEventListener('click', openSettings);
$('#missingBtn').addEventListener('click', findAllInfo);
$('#missingHide').addEventListener('click', () => {
  const ignored = hiddenMissing();
  for (const it of state.items) if (!it.meta) ignored.add(it.path);
  store.set('senzaScheda', JSON.stringify([...ignored]));
  renderMissing();
});
$('#dockToggle').addEventListener('click', () => { queue.collapsed = !queue.collapsed; renderDock(); });
$('#q').addEventListener('input', e => { state.query = e.target.value; render(); });
$('#sort').value = ['added', 'title', 'year'].includes(state.sort) ? state.sort : 'added';
$('#sort').addEventListener('change', e => { state.sort = e.target.value; store.set('sort', state.sort); render(); });
$('#genre').addEventListener('change', e => { state.genre = e.target.value; store.set('genre', state.genre); render(); });
window.addEventListener('online', () => { if (queue.jobs.some(j => j.state === 'riprovo')) toast('Connessione tornata: riprendo il caricamento'); });
$('#sharePath').textContent = sharePath(location.hostname);
$('#shareHow').textContent = IS_MAC ? ' (nel Finder: menu Vai, poi Connessione al server)' : '';
loadLibrary();
</script>
</body>
</html>
VIDEOTECA_HTML_FINE

if [ ! -f "$APP_DIR/config.json" ]; then
  $SUDO tee "$APP_DIR/config.json" >/dev/null <<EOF
{
  "library": "$LIBRARY",
  "port": $PORT,
  "tmdb_key": "",
  "language": "it-IT",
  "reserve_gb": 2
}
EOF
fi

$SUDO chown -R "$RUN_USER:$RUN_USER" "$APP_DIR"
$SUDO chmod 600 "$APP_DIR/config.json"

if [ "${VIDEOTECA_NO_SYSTEMD:-0}" = "1" ]; then
  echo "File installati. Servizio non configurato (VIDEOTECA_NO_SYSTEMD=1)."
  exit 0
fi

$SUDO tee /etc/systemd/system/videoteca.service >/dev/null <<EOF
[Unit]
Description=Videoteca, la dashboard dei film
After=network-online.target local-fs.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 $APP_DIR/videoteca.py
Restart=on-failure
RestartSec=5
UMask=0022
Nice=5

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable videoteca >/dev/null 2>&1
$SUDO systemctl restart videoteca

printf "Avvio la Videoteca"
ok=0
for _ in $(seq 1 30); do
  if curl -s -o /dev/null "http://127.0.0.1:$PORT/api/settings"; then ok=1; break; fi
  printf "."
  sleep 1
done
echo

if [ "$ok" = "1" ]; then
  IP="$(hostname -I | tr ' ' '\n' | grep -E '^[0-9]+\.' | grep -v '^100\.' | head -n 1 || true)"
  TS_IP=""
  if command -v tailscale >/dev/null 2>&1; then
    TS_IP="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
  fi
  echo "Fatto! Apri la Videoteca dal browser del Mac o del telefono:"
  echo "  in casa:     http://${IP:-$(hostname).local}:$PORT"
  if [ -n "$TS_IP" ]; then
    echo "  fuori casa:  http://$TS_IP:$PORT   (con Tailscale acceso)"
  fi
else
  echo "La Videoteca non risponde. Guarda cosa è successo con:  journalctl -u videoteca -n 30"
  exit 1
fi
