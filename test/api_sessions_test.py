#!/usr/bin/env python3
"""Prova i caricamenti a pezzi riprendibili e gli indirizzi di rete."""
import http.client
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

APP = Path("/tmp/vt/app")
LIB = Path("/tmp/vt/film")
UP = LIB / ".videoteca-caricamenti"
PORT = 8099
BASE = f"http://127.0.0.1:{PORT}"
GOOD_KEY = "0123456789abcdef0123456789abcdef"
FAKEBIN = Path(__file__).parent / "fakebin"
MB = 1024 * 1024
passed = 0


def check(cond, label):
    global passed
    if not cond:
        print("FALLITO:", label)
        sys.exit(1)
    passed += 1
    print("ok  ", label)


def request(path, method="GET", body=None, headers=None):
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def jpost(path, obj):
    return request(path, "POST", json.dumps(obj).encode(), {"Content-Type": "application/json"})


def piece(sid, offset, data):
    return request(f"/api/upload/chunk?id={sid}&offset={offset}", "POST", data, {"Content-Type": "application/octet-stream"})


def reset():
    shutil.rmtree(LIB, ignore_errors=True)
    LIB.mkdir(parents=True)
    (APP / "config.json").write_text(json.dumps({"library": str(LIB), "port": 8080, "tmdb_key": GOOD_KEY, "language": "it-IT", "reserve_gb": 2}))
    subprocess.run(["chown", "-R", "stefold:stefold", str(APP), str(LIB)], check=True)
    os.chmod(APP / "config.json", 0o600)


def start_server():
    env_path = f"{FAKEBIN}:/usr/local/bin:/usr/bin:/bin"
    proc = subprocess.Popen(["runuser", "-u", "stefold", "--", "env", f"PATH={env_path}",
                             "VIDEOTECA_TMDB_API=http://127.0.0.1:9911/3", "VIDEOTECA_TMDB_IMG=http://127.0.0.1:9911/t/p/",
                             f"VIDEOTECA_PORT={PORT}", "python3", str(APP / "videoteca.py")],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(60):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
            return proc
        except OSError:
            time.sleep(0.1)
    print(proc.stdout.read())
    raise SystemExit("server non partito")


def jpeg(color):
    buf = io.BytesIO()
    Image.new("RGB", (300, 450), color).save(buf, "JPEG")
    return buf.getvalue()


reset()
mock = subprocess.Popen([sys.executable, str(Path(__file__).parent / "mock_tmdb.py"), "9911"])
time.sleep(0.6)
server = start_server()
try:
    data = os.urandom(20 * MB + 7)
    status, before = request("/api/status")
    usable_before = before["disk"]["usable"]

    status, s = jpost("/api/upload/start", {"name": "Grande.Film.2001.mkv", "size": len(data), "modified": 123, "title": "Grande Film (2001)",
                                            "info": {"tmdb_id": 603, "poster": "/matrix_clean.jpg"}})
    sid = s["id"]
    check(status == 200 and s["offset"] == 0 and len(sid) == 16 and UP.is_dir(), "sessione creata nella cartella nascosta")
    status, after = request("/api/status")
    check(usable_before - after["disk"]["usable"] >= len(data) - MB and after["disk"]["pending"] == len(data), "spazio prenotato per il caricamento")
    status, lib = request("/api/library")
    check(lib["items"] == [], "la cartella dei caricamenti non compare tra i film")

    status, r = piece(sid, 0, data[:8 * MB])
    check(status == 200 and r["offset"] == 8 * MB, "primo pezzo da 8 MB")

    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
    conn.request("POST", f"/api/upload/chunk?id={sid}&offset=0", body=data[:8 * MB], headers={"Content-Type": "application/octet-stream"})
    resp = conn.getresponse()
    body = json.loads(resp.read())
    check(resp.status == 409 and body["offset"] == 8 * MB, "pezzo nel punto sbagliato -> 409 con la posizione giusta")
    conn.request("GET", "/api/upload/status?id=" + sid)
    resp = conn.getresponse()
    check(resp.status == 200 and json.loads(resp.read())["offset"] == 8 * MB, "la stessa connessione resta usabile dopo il 409")
    conn.close()

    sock = socket.create_connection(("127.0.0.1", PORT))
    sock.sendall(f"POST /api/upload/chunk?id={sid}&offset={8 * MB} HTTP/1.1\r\nHost: x\r\nContent-Length: {8 * MB}\r\n\r\n".encode() + data[8 * MB:11 * MB])
    time.sleep(0.6)
    sock.close()
    time.sleep(0.8)
    status, st = request("/api/upload/status?id=" + sid)
    check(status == 200 and st["offset"] == 11 * MB, "pezzo interrotto a metà: il Pi tiene quello che è arrivato")

    status, s2 = jpost("/api/upload/start", {"name": "Grande.Film.2001.mkv", "size": len(data), "modified": 123, "title": "Matrix (1999)",
                                             "info": {"tmdb_id": 603, "poster": "/matrix_clean.jpg"}})
    check(status == 200 and s2["id"] == sid and s2["offset"] == 11 * MB and s2["title"] == "Matrix (1999)", "stesso file scelto di nuovo -> riprende da 11 MB")

    status, sessions = request("/api/upload/sessions")
    check(len(sessions["sessions"]) == 1 and sessions["sessions"][0]["offset"] == 11 * MB, "elenco dei caricamenti in sospeso")

    offset = 11 * MB
    while offset < len(data):
        end = min(offset + 8 * MB, len(data))
        status, r = piece(sid, offset, data[offset:end])
        check(status == 200 and r["offset"] == end, f"pezzo {offset // MB}-{end // MB} MB")
        offset = end
    status, r = piece(sid, offset, b"x")
    check(status == 400, "pezzo oltre la fine del file rifiutato")

    custom = jpeg((10, 120, 200))
    status, r = request(f"/api/upload/cover?id={sid}", "POST", custom, {"Content-Type": "image/jpeg"})
    check(status == 200 and r["has_cover"], "copertina personale insieme al film")

    status, done = jpost(f"/api/upload/finish?id={sid}", {})
    item = done["item"]
    video = LIB / item["path"]
    check(status == 200 and item["path"] == "Matrix (1999)/Matrix (1999).mkv" and done["warning"] == "", "caricamento chiuso nella cartella giusta")
    check(video.read_bytes() == data, "il film è identico all'originale, byte per byte")
    check(item["meta"]["title"] == "Matrix" and item["meta"]["cover"] == "personale" and video.with_suffix(".jpg").read_bytes() == custom, "scheda TMDB + copertina personale applicate")
    check((video.parent / "folder.jpg").exists() and not list(UP.iterdir()), "folder.jpg scritto e sessione pulita")
    status, r = jpost(f"/api/upload/finish?id={sid}", {})
    check(status == 404, "chiudere di nuovo -> 404")

    status, s = jpost("/api/upload/start", {"name": "Corto.mp4", "size": 3 * MB, "modified": 1, "title": "Corto"})
    piece(s["id"], 0, os.urandom(MB))
    status, r = jpost(f"/api/upload/finish?id={s['id']}", {})
    check(status == 409 and r["offset"] == MB, "chiudere un caricamento a metà -> 409 con la posizione")
    status, r = jpost("/api/upload/start", {"name": "Altro.mp4", "size": 3 * MB, "modified": 1, "resume": s["id"]})
    check(status == 409 and "Corto.mp4" in r["error"], "riprendere con il file sbagliato -> 409")
    status, r = jpost(f"/api/upload/cancel?id={s['id']}", {})
    check(status == 200 and not list(UP.iterdir()), "caricamento annullato e ripulito")
    status, r = request(f"/api/upload/status?id={s['id']}")
    check(status == 404, "stato di un caricamento annullato -> 404")

    status, r = jpost("/api/upload/start", {"name": "Enorme.mkv", "size": 10 ** 15, "modified": 1, "title": "Enorme"})
    check(status == 507 and "Spazio insufficiente" in r["error"], "film troppo grande rifiutato all'inizio")

    status, now = request("/api/status")
    big = now["disk"]["usable"] - 50 * MB
    status, a = jpost("/api/upload/start", {"name": "A.mkv", "size": big, "modified": 1, "title": "A"})
    status_b, b = jpost("/api/upload/start", {"name": "B.mkv", "size": 100 * MB, "modified": 1, "title": "B"})
    check(status == 200 and status_b == 507, "lo spazio prenotato da un caricamento vale per gli altri")
    jpost(f"/api/upload/cancel?id={a['id']}", {})

    for bad in ("../../etc/passwd", "zz", "0123456789abcdeg"):
        status, r = request("/api/upload/status?id=" + urllib.parse.quote(bad))
        check(status == 400, f"identificativo non valido rifiutato: {bad}")
    status, r = piece("../x", 0, b"abc")
    check(status == 400, "pezzo con identificativo non valido rifiutato")

    status, s = jpost("/api/upload/start", {"name": "Vacanze.mp4", "size": 2 * MB, "modified": 5, "title": "Vacanze (2019)",
                                            "info": {"manual": {"title": "Vacanze", "year": "2019", "runtime": "30", "overview": "Mare."}}})
    piece(s["id"], 0, os.urandom(2 * MB))
    status, done = jpost(f"/api/upload/finish?id={s['id']}", {})
    check(status == 200 and done["item"]["meta"]["source"] == "manuale" and done["item"]["meta"]["runtime"] == 30, "scheda a mano applicata alla fine")

    status, s = jpost("/api/upload/start", {"name": "Sconosciuto.mkv", "size": MB, "modified": 5, "title": "Sconosciuto", "info": {"tmdb_id": 12345}})
    piece(s["id"], 0, os.urandom(MB))
    status, done = jpost(f"/api/upload/finish?id={s['id']}", {})
    check(status == 200 and done["warning"] and done["item"]["meta"] is None and (LIB / done["item"]["path"]).exists(), "se TMDB non risponde il film resta, con un avviso")

    status, s = jpost("/api/upload/start", {"name": "Vecchio.mkv", "size": MB, "modified": 9, "title": "Vecchio"})
    old_sid = s["id"]
    info_file = UP / f"{old_sid}.json"
    meta = json.loads(info_file.read_text())
    meta["updated"] = int(time.time()) - 5 * 24 * 3600
    info_file.write_text(json.dumps(meta))
    (UP / "abcdefabcdefabcd.part").write_bytes(b"orfano")
    status, s = jpost("/api/upload/start", {"name": "Recente.mkv", "size": MB, "modified": 9, "title": "Recente"})
    recent_sid = s["id"]
    status, r = request(f"/media/.videoteca-caricamenti/{recent_sid}.part")
    check(status == 404, "i pezzi in caricamento non sono scaricabili")

    status, results = request("/api/search?q=matrix")
    check(results["results"][0]["poster_path"] == "/matrix_it.jpg", "la ricerca restituisce anche la copertina da usare")
    status, settings = request("/api/settings")
    ips = {(a["iface"], a["ip"], a["tailscale"]) for a in settings["addresses"]}
    check(("eth0", "192.168.1.37", False) in ips and ("tailscale0", "100.101.102.103", True) in ips and settings["tailscale_name"] == "rasberry-daniele-casa.tail1234.ts.net" and settings["port"] == PORT, "indirizzi di casa e di Tailscale nelle impostazioni")

    server.terminate(); server.wait(5)
    server = start_server()
    left = sorted(p.name for p in UP.iterdir())
    check(left == sorted([f"{recent_sid}.json", f"{recent_sid}.part"]), "al riavvio spariscono i caricamenti scaduti e i pezzi orfani")
finally:
    server.terminate()
    mock.terminate()
    out = server.stdout.read() if server.stdout else ""
    if "Errore inatteso" in out or "Traceback" in out:
        print("--- log del server ---\n" + out)

print(f"\nTutti i {passed} controlli passati.")
