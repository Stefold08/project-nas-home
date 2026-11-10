#!/usr/bin/env python3
"""Prova le API della Videoteca contro il finto TMDB."""
import base64
import http.client
import io
import json
import os
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
PORT = 8099
BASE = f"http://127.0.0.1:{PORT}"
GOOD_KEY = "0123456789abcdef0123456789abcdef"
ENV = dict(os.environ, VIDEOTECA_TMDB_API="http://127.0.0.1:9911/3", VIDEOTECA_TMDB_IMG="http://127.0.0.1:9911/t/p/", VIDEOTECA_PORT=str(PORT))

passed = 0


def check(cond, label):
    global passed
    if not cond:
        print("FALLITO:", label)
        sys.exit(1)
    passed += 1
    print("ok  ", label)


def request(path, method="GET", body=None, headers=None, auth=None):
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers or {})
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def jget(path, **kw):
    status, headers, body = request(path, **kw)
    return status, json.loads(body or b"{}")


def jpost(path, obj, **kw):
    status, headers, body = request(path, "POST", json.dumps(obj).encode(), {"Content-Type": "application/json"}, **kw)
    return status, json.loads(body or b"{}")


def start_server():
    proc = subprocess.Popen(["runuser", "-u", "stefold", "--", "env",
                             f"VIDEOTECA_TMDB_API={ENV['VIDEOTECA_TMDB_API']}", f"VIDEOTECA_TMDB_IMG={ENV['VIDEOTECA_TMDB_IMG']}",
                             f"VIDEOTECA_PORT={PORT}", "python3", str(APP / "videoteca.py")],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
            return proc
        except OSError:
            time.sleep(0.1)
    print(proc.stdout.read())
    raise SystemExit("server non partito")


def jpeg(color, size=(400, 600)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


mock = subprocess.Popen([sys.executable, str(Path(__file__).parent / "mock_tmdb.py"), "9911"])
time.sleep(0.6)
server = start_server()
try:
    status, headers, body = request("/")
    check(status == 200 and b"<title>Videoteca</title>" in body and headers["Content-Type"].startswith("text/html"), "pagina principale")

    status, data = jget("/api/library")
    check(status == 200 and data["items"] == [] and data["disk"]["usable"] <= data["disk"]["free"], "libreria vuota + disco")

    status, data = jget("/api/settings")
    check(status == 200 and data["has_key"] is False and data["has_password"] is False, "impostazioni iniziali")

    status, data = jget("/api/search?q=matrix")
    check(status == 412 and "chiave TMDB" in data["error"], "ricerca senza chiave -> messaggio chiaro")

    status, data = jpost("/api/settings", {"tmdb_key": "sbagliata"})
    check(status == 400 and "non accetta la chiave" in data["error"], "chiave sbagliata rifiutata")

    status, data = jpost("/api/settings", {"tmdb_key": GOOD_KEY})
    check(status == 200 and data["has_key"] and data["key_end"] == "cdef", "chiave valida salvata")
    cfg_stat = (APP / "config.json").stat()
    check(oct(cfg_stat.st_mode)[-3:] == "600" and GOOD_KEY in (APP / "config.json").read_text(), "config.json salvato con permessi 600")

    status, data = jget("/api/search?q=matrix&year=1999")
    check(status == 200 and len(data["results"]) == 1 and data["results"][0]["id"] == 603 and data["results"][0]["poster"].endswith("/w185/matrix_it.jpg"), "ricerca Matrix 1999")
    status, data = jget("/api/search?q=matrix&year=2005")
    check(status == 200 and len(data["results"]) == 1, "anno sbagliato -> riprova senza anno")
    status, data = jget("/api/search?q=zzzz")
    check(status == 200 and data["results"] == [], "nessun risultato")

    status, data = jget("/api/tmdb?id=603")
    meta, posters = data["meta"], data["posters"]
    check(meta["runtime"] == 136 and meta["genres"] == ["Azione", "Fantascienza"] and meta["year"] == "1999", "dettagli: durata, generi, anno")
    check(meta["directors"] == ["Lana Wachowski", "Lilly Wachowski"] and len(meta["cast"]) == 6 and meta["rating"] == 8.2, "dettagli: regia, cast, voto")
    check([p["path"] for p in posters] == ["/matrix_it.jpg", "/matrix_clean.jpg", "/matrix_en.jpg", "/matrix_fr.jpg"] and posters[0]["main"] and posters[0]["lang"] == "it", "locandine in ordine (italiano, senza scritte, inglese, altre)")
    status, data = jget("/api/tmdb?id=999")
    check(status == 200 and data["meta"]["overview"].startswith("An English-only") and data["posters"] == [], "descrizione inglese se manca quella italiana")
    status, data = jget("/api/tmdb?id=12345")
    check(status == 404, "film inesistente su TMDB")

    # Caricamento di un film
    payload = os.urandom(3 * 1024 * 1024 + 123)
    q = urllib.parse.urlencode({"name": "The.Matrix.1999.1080p.BluRay.mkv", "title": "Matrix (1999)"})
    status, headers, body = request("/api/upload?" + q, "POST", payload, {"Content-Type": "application/octet-stream"})
    item = json.loads(body)["item"]
    check(status == 200 and item["path"] == "Matrix (1999)/Matrix (1999).mkv" and item["size"] == len(payload), "caricamento film")
    video = LIB / item["path"]
    check(video.read_bytes() == payload and video.stat().st_uid == Path("/home/stefold").stat().st_uid, "file intatto e dell'utente giusto")
    check(not list(video.parent.glob(".*.part")), "nessun file .part rimasto")

    status, data = jpost("/api/info", {"path": item["path"], "tmdb_id": 603, "poster": "/matrix_clean.jpg"})
    item = data["item"]
    cover = video.with_suffix(".jpg")
    check(status == 200 and item["meta"]["title"] == "Matrix" and item["meta"]["cover"] == "tmdb" and item["cover"].startswith("/media/Matrix%20%281999%29/Matrix%20%281999%29.jpg?v="), "scheda + copertina TMDB salvate")
    check(cover.read_bytes()[:3] == b"\xff\xd8\xff" and (video.parent / "folder.jpg").read_bytes() == cover.read_bytes(), "copertina e folder.jpg scritte")
    check((video.parent / ".Matrix (1999).mkv.videoteca.json").is_file(), "scheda salvata in un file nascosto")

    status, headers, body = request(item["cover"])
    check(status == 200 and headers["Content-Type"] == "image/jpeg" and body == cover.read_bytes(), "copertina servita")

    status, headers, body = request(item["url"], headers={"Range": "bytes=100-199"})
    check(status == 206 and headers["Content-Range"] == f"bytes 100-199/{len(payload)}" and body == payload[100:200], "streaming: Range 100-199")
    status, headers, body = request(item["url"], headers={"Range": "bytes=-50"})
    check(status == 206 and body == payload[-50:], "streaming: ultimi 50 byte")
    status, headers, body = request(item["url"], headers={"Range": f"bytes={len(payload) + 10}-"})
    check(status == 416, "streaming: Range fuori dal file")
    status, headers, body = request(item["url"], method="HEAD")
    check(status == 200 and headers["Content-Length"] == str(len(payload)) and body == b"" and headers["Content-Type"] == "video/x-matroska", "HEAD sul video")
    status, headers, body = request(item["url"])
    check(status == 200 and body == payload, "download completo")

    custom = jpeg((200, 30, 30))
    status, headers, body = request("/api/cover?path=" + urllib.parse.quote(item["path"]), "POST", custom, {"Content-Type": "image/jpeg"})
    data = json.loads(body)
    check(status == 200 and cover.read_bytes() == custom and data["item"]["meta"]["cover"] == "personale", "copertina personale")
    status, data = jpost("/api/cover", {"path": item["path"], "poster": "/matrix_en.jpg"})
    check(status == 200 and data["item"]["meta"]["cover"] == "tmdb" and cover.read_bytes() != custom, "copertina cambiata con una di TMDB")
    status, data = jpost("/api/cover", {"path": item["path"], "poster": "/../../etc/passwd"})
    check(status == 400, "copertina con percorso strano rifiutata")
    status, headers, body = request("/api/cover?path=" + urllib.parse.quote(item["path"]), "POST", b"non sono un'immagine", {"Content-Type": "image/jpeg"})
    check(status == 415, "file non immagine rifiutato come copertina")

    # Film copiato a mano nella cartella (come da Samba)
    loose = LIB / "Nuovo.Cinema.Paradiso.1988.ITA.avi"
    loose.write_bytes(os.urandom(1000))
    os.chown(loose, Path("/home/stefold").stat().st_uid, Path("/home/stefold").stat().st_gid)
    status, data = jget("/api/library")
    paths = {i["path"]: i for i in data["items"]}
    check(set(paths) == {"Matrix (1999)/Matrix (1999).mkv", loose.name} and paths[loose.name]["meta"] is None, "film copiato a mano compare senza scheda")
    status, data = jpost("/api/info", {"path": loose.name, "tmdb_id": 11216, "poster": "/paradiso_it.jpg"})
    check(status == 200 and data["item"]["meta"]["directors"] == ["Giuseppe Tornatore"] and loose.with_suffix(".jpg").is_file(), "scheda per il film copiato a mano")
    check(not (LIB / "folder.jpg").exists(), "nessun folder.jpg nella cartella principale")

    # Scheda a mano
    q = urllib.parse.urlencode({"name": "VID_2019_mare.mp4", "title": "Vacanze al mare (2019)"})
    status, headers, body = request("/api/upload?" + q, "POST", b"x" * 5000, {"Content-Type": "application/octet-stream"})
    home = json.loads(body)["item"]
    status, data = jpost("/api/info", {"path": home["path"], "manual": {"title": "Vacanze al mare", "year": "2019", "runtime": "42", "overview": "Il video delle vacanze."}})
    check(status == 200 and data["item"]["meta"]["source"] == "manuale" and data["item"]["meta"]["runtime"] == 42 and data["item"]["cover"] is None, "scheda compilata a mano")
    status, data = jpost("/api/info", {"path": home["path"], "manual": {"title": "  "}})
    check(status == 400 and "titolo" in data["error"], "scheda a mano senza titolo rifiutata")

    # Sicurezza dei percorsi
    for bad in ("/media/../../etc/passwd", "/media/%2e%2e/%2e%2e/etc/passwd", "/media/..%2F..%2Fapp%2Fconfig.json"):
        status, headers, body = request(bad)
        check(status in (403, 404) and b"root:" not in body and GOOD_KEY.encode() not in body, f"percorso bloccato: {bad}")
    status, headers, body = request("/media/" + urllib.parse.quote("Matrix (1999)/.Matrix (1999).mkv.videoteca.json"))
    check(status == 404, "file nascosti non serviti")
    status, data = jpost("/api/delete", {"path": "../app/config.json"})
    check(status == 403 and (APP / "config.json").exists(), "non si cancella fuori dalla cartella dei film")
    status, data = jpost("/api/delete", {"path": "Matrix (1999)/Matrix (1999).jpg"})
    check(status == 404 and cover.exists(), "si cancellano solo video")

    # Controlli sul caricamento
    q = urllib.parse.urlencode({"name": "documento.pdf", "title": "x"})
    status, headers, body = request("/api/upload?" + q, "POST", b"abc", {"Content-Type": "application/octet-stream"})
    check(status == 415, "estensione non video rifiutata")

    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    conn.putrequest("POST", "/api/upload?" + urllib.parse.urlencode({"name": "Enorme.mkv", "title": "Enorme"}))
    conn.putheader("Content-Length", str(10 ** 15))
    conn.endheaders()
    conn.send(b"x" * 1024)
    resp = conn.getresponse()
    data = json.loads(resp.read())
    check(resp.status == 507 and "Spazio insufficiente" in data["error"] and resp.getheader("Connection") == "close", "film troppo grande rifiutato prima di scriverlo")
    check(not (LIB / "Enorme").exists(), "nessuna cartella creata per il film troppo grande")
    conn.close()

    sock = socket.create_connection(("127.0.0.1", PORT))
    path = "/api/upload?" + urllib.parse.urlencode({"name": "Interrotto.mkv", "title": "Interrotto"})
    sock.sendall(f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {5 * 1024 * 1024}\r\n\r\n".encode() + b"y" * (1024 * 1024))
    time.sleep(0.5)
    sock.close()
    time.sleep(1.0)
    check(not (LIB / "Interrotto").exists(), "caricamento interrotto: niente avanzi")

    # Eliminazione
    status, data = jpost("/api/delete", {"path": "Matrix (1999)/Matrix (1999).mkv"})
    check(status == 200 and data["ok"] and not (LIB / "Matrix (1999)").exists(), "eliminazione: video, copertina, scheda e cartella")
    status, data = jpost("/api/delete", {"path": loose.name})
    check(status == 200 and not loose.exists() and not loose.with_suffix(".jpg").exists() and LIB.exists(), "eliminazione di un film nella cartella principale")

    # Password
    status, data = jpost("/api/settings", {"password": "ab"})
    check(status == 400, "password troppo corta rifiutata")
    status, data = jpost("/api/settings", {"password": "segreta"})
    check(status == 200 and data["has_password"], "password impostata")
    status, headers, body = request("/api/library")
    check(status == 401 and headers.get("WWW-Authenticate", "").startswith("Basic"), "senza password -> 401")
    status, headers, body = request("/api/library", auth="chiunque:sbagliata")
    check(status == 401, "password sbagliata -> 401")
    status, headers, body = request("/api/library", auth="chiunque:segreta")
    check(status == 200, "password giusta -> accesso")
    t0 = time.time()
    for _ in range(5):
        request("/api/settings", auth="chiunque:segreta")
    check(time.time() - t0 < 1.0, "password verificata una volta sola (cache)")
    status, data = jpost("/api/settings", {"password": ""}, auth="chiunque:segreta")
    check(status == 200 and not data["has_password"], "password tolta")
    status, headers, body = request("/api/library")
    check(status == 200, "di nuovo accessibile senza password")

    # Avanzi puliti al riavvio
    leftover = LIB / "Vacanze al mare (2019)" / ".Qualcosa.mkv.part"
    leftover.write_bytes(b"pezzo")
    server.terminate(); server.wait(5)
    server = start_server()
    check(not leftover.exists(), "al riavvio i caricamenti interrotti vengono puliti")
    status, data = jget("/api/library")
    check(status == 200 and len(data["items"]) == 1 and data["items"][0]["meta"]["title"] == "Vacanze al mare", "dati conservati dopo il riavvio")
finally:
    server.terminate()
    mock.terminate()
    out = server.stdout.read() if server.stdout else ""
    if "Errore inatteso" in out or "Traceback" in out:
        print("--- log del server ---\n" + out)

print(f"\nTutti i {passed} controlli passati.")
