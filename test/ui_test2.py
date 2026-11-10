#!/usr/bin/env python3
"""Prova nel browser le novità: coda di caricamento, rete che cade, ripresa dopo la ricarica, più film, Mac."""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

APP = Path("/tmp/vt/app")
LIB = Path("/tmp/vt/film")
PORT = 8099
BASE = f"http://127.0.0.1:{PORT}"
SHOTS = Path(__file__).resolve().parent / "schermate"
FAKEBIN = Path(__file__).parent / "fakebin"
GOOD_KEY = "0123456789abcdef0123456789abcdef"
MB = 1024 * 1024
MAC_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15"


def reset():
    shutil.rmtree(LIB, ignore_errors=True)
    LIB.mkdir(parents=True)
    (APP / "config.json").write_text(json.dumps({"library": str(LIB), "port": 8080, "tmdb_key": GOOD_KEY, "language": "it-IT", "reserve_gb": 2}))
    subprocess.run(["chown", "-R", "stefold:stefold", str(APP), str(LIB)], check=True)
    os.chmod(APP / "config.json", 0o600)


def wait_port(port):
    for _ in range(60):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.1)
    raise SystemExit(f"porta {port} non pronta")


def post(path, body, ctype="application/json"):
    data = json.dumps(body).encode() if ctype == "application/json" else body
    req = urllib.request.Request(BASE + path, data=data, method="POST", headers={"Content-Type": ctype})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def get(path):
    with urllib.request.urlopen(BASE + path) as resp:
        return json.loads(resp.read())


def seed():
    def upload(name, title, size=300_000):
        q = urllib.parse.urlencode({"name": name, "title": title})
        return post("/api/upload?" + q, b"\0" * size, "application/octet-stream")["item"]
    matrix = upload("The.Matrix.1999.mkv", "Matrix (1999)", 2_300_000)
    post("/api/info", {"path": matrix["path"], "tmdb_id": 603, "poster": "/matrix_it.jpg"})
    raro = upload("A.Rare.Film.1973.avi", "Film Raro (1973)")
    post("/api/info", {"path": raro["path"], "tmdb_id": 999})
    for name in ("Il.Sorpasso.1962.ITA.DVDrip.avi", "Nuovo.Cinema.Paradiso.1988.720p.mkv"):
        loose = LIB / name
        loose.write_bytes(b"\0" * 200_000)
        shutil.chown(loose, "stefold", "stefold")


def make_file(name, size):
    path = Path("/tmp/vt/da-caricare") / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(os.urandom(size))
    return path


reset()
mock = subprocess.Popen([sys.executable, str(Path(__file__).parent / "mock_tmdb.py"), "9911"])
server = subprocess.Popen(["runuser", "-u", "stefold", "--", "env", f"PATH={FAKEBIN}:/usr/local/bin:/usr/bin:/bin",
                           "VIDEOTECA_TMDB_API=http://127.0.0.1:9911/3", "VIDEOTECA_TMDB_IMG=http://127.0.0.1:9911/t/p/",
                           f"VIDEOTECA_PORT={PORT}", "python3", str(APP / "videoteca.py")],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
wait_port(9911)
wait_port(PORT)
seed()
SHOTS.mkdir(exist_ok=True)
for old in SHOTS.glob("*.png"):
    old.unlink()

flaky_file = make_file("The.Matrix.1999.Directors.Cut.mkv", 20 * MB + 123)
resume_file = make_file("Nuovo.Cinema.Paradiso.1988.1080p.mkv", 40 * MB + 77)
batch_a = make_file("The.Matrix.1999.REMUX.mkv", 9 * MB)
batch_b = make_file("Video.Compleanno.Nonna.2021.mp4", 3 * MB)

problems = []
dialogs = []
with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.route("**/fonts.googleapis.com/**", lambda route: route.abort())
    context.route("**/fonts.gstatic.com/**", lambda route: route.abort())
    page = context.new_page()
    page.on("pageerror", lambda exc: problems.append(f"errore JS: {exc}"))
    page.on("console", lambda msg: problems.append(f"console: {msg.text}") if msg.type == "error" and "ERR_" not in msg.text and "Failed to load resource" not in msg.text else None)
    page.on("dialog", lambda d: (dialogs.append(d.type), d.accept()))

    page.goto(BASE)
    page.wait_for_selector(".film")
    tally = page.locator("#tally").inner_text()
    print("Conteggio:", tally)
    assert tally.startswith("4 film, ") and "MB" in tally, tally

    # Filtro per genere, ricordato dopo la ricarica
    genres = page.locator("#genre option").all_inner_texts()
    print("Generi:", genres)
    assert genres == ["Tutti i generi", "Azione", "Documentario", "Fantascienza"], genres
    page.select_option("#genre", "Documentario")
    assert page.locator(".film-title").all_inner_texts() == ["Film Raro"]
    page.reload()
    page.wait_for_selector(".film")
    assert page.locator("#genre").input_value() == "Documentario"
    page.select_option("#genre", "")

    # Trova le info per tutti
    assert page.locator("#missingText").inner_text() == "2 film non hanno ancora una scheda."
    page.screenshot(path=SHOTS / "01-senza-scheda.png")
    page.get_by_role("button", name="Trova le info per tutti").click()
    page.wait_for_function("document.querySelector('#toast').textContent.startsWith('Schede trovate per 1 film su 2')")
    assert page.locator("#missingText").inner_text() == "1 film non ha ancora una scheda."
    assert "Nuovo Cinema Paradiso" in page.locator(".film-title").all_inner_texts()
    page.get_by_role("button", name="Nascondi").click()
    assert page.locator("#missingNotice").is_hidden()

    # Un film con la rete che cade due volte
    chunk_calls = {"n": 0}

    def flaky(route):
        chunk_calls["n"] += 1
        if chunk_calls["n"] in (2, 3):
            route.abort("failed")
        else:
            route.continue_()

    page.route("**/api/upload/chunk**", flaky)
    page.get_by_role("button", name="Aggiungi film").first.click()
    page.set_input_files("#fileInput", str(flaky_file))
    page.get_by_role("button", name="Avanti").click()
    page.wait_for_selector(".result")
    page.locator(".result").first.click()
    page.wait_for_selector(".cover-opt")
    page.get_by_role("button", name="Carica film").click()
    page.wait_for_selector("#wizard[open]", state="detached")
    page.wait_for_selector(".job.riprovo", timeout=20000)
    page.screenshot(path=SHOTS / "02-rete-caduta.png")
    page.wait_for_selector(".job.fatto", timeout=60000)
    page.screenshot(path=SHOTS / "03-caricato.png")
    page.unroute("**/api/upload/chunk**")
    stored = LIB / "Matrix (1999)" / "Matrix (1999) (2).mkv"
    if not stored.exists():
        stored = next(p for p in LIB.rglob("*.mkv") if p.stat().st_size == flaky_file.stat().st_size)
    print("Rete instabile: richieste di pezzi =", chunk_calls["n"], "->", stored.relative_to(LIB))
    assert stored.read_bytes() == flaky_file.read_bytes(), "file diverso dopo i tentativi"
    page.get_by_role("button", name="Togli").click()

    # Ricarica la pagina a metà caricamento, poi riprende
    def slow(route):
        time.sleep(0.8)
        route.continue_()

    page.route("**/api/upload/chunk**", slow)
    page.get_by_role("button", name="Aggiungi film").first.click()
    page.set_input_files("#fileInput", str(resume_file))
    page.get_by_role("button", name="Avanti").click()
    page.wait_for_selector(".result")
    page.locator(".result").first.click()
    page.wait_for_selector(".cover-opt")
    page.get_by_role("button", name="Carica film").click()
    for _ in range(100):
        sessions = get("/api/upload/sessions")["sessions"]
        if sessions and sessions[0]["offset"] >= 16 * MB:
            break
        page.wait_for_timeout(200)
    print("Offset prima della ricarica:", sessions[0]["offset"] // MB, "MB")
    page.unroute("**/api/upload/chunk**")
    page.reload()
    page.wait_for_selector(".job.interrotto")
    assert "beforeunload" in dialogs, dialogs
    status_text = page.locator(".job.interrotto .job-status").inner_text()
    print("Dopo la ricarica:", status_text)
    assert "Interrotto al" in status_text and resume_file.name in status_text
    page.screenshot(path=SHOTS / "04-interrotto.png")
    page.locator(".job.interrotto input[type=file]").set_input_files(str(resume_file))
    page.wait_for_selector(".job.fatto", timeout=60000)
    stored = next(p for p in LIB.rglob("*.mkv") if p.stat().st_size == resume_file.stat().st_size)
    assert stored.read_bytes() == resume_file.read_bytes(), "file diverso dopo la ripresa"
    assert get("/api/upload/sessions")["sessions"] == []
    print("Ripreso e completato:", stored.relative_to(LIB))
    page.get_by_role("button", name="Togli").click()

    # Annullare un caricamento in corso
    page.route("**/api/upload/chunk**", slow)
    page.get_by_role("button", name="Aggiungi film").first.click()
    page.set_input_files("#fileInput", str(flaky_file))
    page.get_by_role("button", name="Avanti").click()
    page.get_by_role("button", name="Non lo trovi? Compila la scheda a mano").click()
    page.get_by_role("button", name="Usa questa scheda").click()
    page.get_by_role("button", name="Carica film").click()
    page.wait_for_selector(".job.carico")
    page.locator(".job.carico").get_by_role("button", name="Annulla").click()
    page.wait_for_selector(".job.annullato")
    page.unroute("**/api/upload/chunk**")
    time.sleep(1.5)
    assert get("/api/upload/sessions")["sessions"] == [], "sessione non cancellata"
    page.get_by_role("button", name="Togli").click()

    # Più film insieme
    page.get_by_role("button", name="Aggiungi film").first.click()
    page.set_input_files("#fileInput", [str(batch_a), str(batch_b)])
    page.wait_for_function("document.querySelector('#wizTitle').textContent === 'Aggiungi più film'")
    page.get_by_role("button", name="Avanti").click()
    page.wait_for_selector("text=Carica 2 film")
    rows = page.locator(".batch-row .r-title").all_inner_texts()
    print("Schede trovate:", rows)
    assert rows == ["Matrix (1999)", "Video Compleanno Nonna (2021)"], rows
    page.screenshot(path=SHOTS / "05-piu-film.png")
    page.get_by_role("button", name="Carica 2 film").click()
    page.wait_for_function("document.querySelectorAll('.job.fatto').length === 2", timeout=60000)
    page.screenshot(path=SHOTS / "06-coda-finita.png")
    lib = get("/api/library")["items"]
    titles = sorted((i["meta"] or {}).get("title", i["file"]) for i in lib)
    print("In videoteca:", titles)
    assert len(lib) == 8, len(lib)

    # Scheda: pulsante Scarica
    page.get_by_role("button", name="Apri la scheda di Film Raro").click()
    download = page.get_by_role("link", name="Scarica")
    assert download.get_attribute("download") == "Film Raro (1973).avi"
    page.keyboard.press("Escape")

    # Tasto / per cercare
    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.id") == "q"

    # Mac: percorso della cartella condivisa e indirizzi
    mac = browser.new_context(viewport={"width": 1280, "height": 900}, user_agent=MAC_UA)
    mac.route("**/fonts.googleapis.com/**", lambda route: route.abort())
    mac.route("**/fonts.gstatic.com/**", lambda route: route.abort())
    mpage = mac.new_page()
    mpage.on("pageerror", lambda exc: problems.append(f"errore JS (Mac): {exc}"))
    mpage.goto(BASE)
    mpage.wait_for_selector(".film")
    mpage.locator("#settingsBtn").click()
    mpage.wait_for_selector("#settings[open]")
    addr = mpage.locator("#addrList").inner_text()
    print("Indirizzi:", addr.replace("\n", " | "))
    assert "http://192.168.1.37:8099" in addr and "smb://127.0.0.1/film" in addr
    assert "Fuori casa\nhttp://100.101.102.103:8099" in addr, "manca l'indirizzo Tailscale in numeri"
    assert addr.index("100.101.102.103") < addr.index("rasberry-daniele-casa.tail1234.ts.net"), "ordine indirizzi"
    mpage.screenshot(path=SHOTS / "07-impostazioni-mac.png")
    mac.close()

    phone = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    phone.route("**/fonts.googleapis.com/**", lambda route: route.abort())
    phone.route("**/fonts.gstatic.com/**", lambda route: route.abort())
    ppage = phone.new_page()
    ppage.on("pageerror", lambda exc: problems.append(f"errore JS (telefono): {exc}"))
    ppage.goto(BASE)
    ppage.wait_for_selector(".film")
    ppage.route("**/api/upload/chunk**", slow)
    ppage.get_by_role("button", name="Aggiungi film").first.click()
    ppage.set_input_files("#fileInput", str(batch_b))
    ppage.get_by_role("button", name="Avanti").click()
    ppage.get_by_role("button", name="Non lo trovi? Compila la scheda a mano").click()
    ppage.get_by_role("button", name="Usa questa scheda").click()
    ppage.get_by_role("button", name="Carica film").click()
    ppage.wait_for_selector(".job.carico")
    box = ppage.locator("#toast").bounding_box()
    print("Avviso sul telefono:", box)
    if not box or box["width"] < 250 or box["height"] > 80:
        problems.append(f"avviso troppo stretto o alto sul telefono: {box}")
    ppage.screenshot(path=SHOTS / "08-telefono-caricamento.png")
    ppage.wait_for_selector(".job.fatto", timeout=60000)
    assert not ppage.evaluate("document.documentElement.scrollWidth > window.innerWidth"), "scorrimento orizzontale sul telefono"
    phone.close()
    browser.close()

server.terminate()
mock.terminate()
log = server.stdout.read()
if "Errore inatteso" in log or "Traceback" in log:
    problems.append("log server:\n" + log)
print("\nProblemi:", problems or "nessuno")
sys.exit(1 if problems else 0)
