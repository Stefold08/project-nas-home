"""Prova della pagina Rete nel browser. Prima lancia:  python3 rete_test.py --lascia"""
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

QUI = Path(__file__).resolve().parent
sys.path.insert(0, str(QUI))
import dnsq  # noqa: E402
import rete_test  # noqa: E402

FOTO = QUI / "schermate"
FOTO.mkdir(exist_ok=True)
BASE = "http://127.0.0.1:8099"
problemi, errori_js = [], []
MAC_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15"
IPHONE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1"


def controlla(condizione, cosa):
    print(("ok       " if condizione else "FALLITO  ") + cosa, flush=True)
    if not condizione:
        problemi.append(cosa)


def contesto(browser, **opzioni):
    ctx = browser.new_context(**opzioni)
    ctx.route("**/fonts.g*/**", lambda r: r.abort())
    return ctx


def pagina(ctx, url):
    p = ctx.new_page()
    p.on("pageerror", lambda e: errori_js.append(str(e)))
    p.goto(url)
    return p


def main():
    # un avviso nuovo, da leggere, da un dispositivo senza MAC
    dnsq.ask("malware-test.example", "127.0.0.9", server=("127.0.0.1", 5353))
    time.sleep(3)
    spenta = rete_test.avvia_videoteca(porta=8098, attiva=False)
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])

            # ---- computer
            ctx = contesto(b, viewport={"width": 1280, "height": 900}, user_agent=MAC_UA)
            p = pagina(ctx, BASE + "/#rete")
            p.wait_for_selector("#reteAccesa:not([hidden]) .avviso")
            p.wait_for_function("document.querySelectorAll('#dispList .disp').length >= 4")
            controlla(p.locator("#reteFatti .fatto").count() == 3, "tre riquadri di stato")
            controlla(p.locator("#grafico li").count() == 24, "grafico con 24 ore")
            da_leggere = json.loads(urllib.request.urlopen(BASE + "/api/rete/conta").read())["da_leggere"]
            controlla(da_leggere >= 1 and p.locator("#reteBadge").inner_text() == str(da_leggere), f"badge con gli avvisi da leggere ({p.locator('#reteBadge').inner_text()} su {da_leggere})")
            controlla(p.locator("#tabRete").get_attribute("aria-current") == "page", "scheda Rete evidenziata")
            controlla(not p.locator("#addBtn").is_visible() and p.locator("#tgBtn").is_visible(), "strumenti giusti per la vista Rete")
            controlla("da leggere" in p.locator("#tally").inner_text() and "dispositiv" in p.locator("#tally").inner_text(), f"riassunto in alto: {p.locator('#tally').inner_text()}")
            controlla(p.title() == "Rete di casa", "titolo della finestra")
            p.screenshot(path=FOTO / "11-rete.png", full_page=True)

            # rinomina
            riga = p.locator("#dispList .disp", has_text="Samsung").first
            riga.get_by_role("button", name="Rinomina").click()
            p.wait_for_selector("#rinomina[open]")
            p.fill("#rinInput", "Televisore sala")
            p.click("#rinForm button[type=submit]")
            p.wait_for_selector("#rinomina", state="hidden")
            p.wait_for_function("[...document.querySelectorAll('#dispList strong')].some(e => e.textContent === 'Televisore sala')")
            controlla(True, "dispositivo rinominato dalla pagina")

            # Telegram già collegato
            p.click("#tgBtn")
            p.wait_for_selector("#telegram[open]")
            controlla("Collegato" in p.locator("#tgBody").inner_text(), "finestra Telegram: collegato")
            p.screenshot(path=FOTO / "12-telegram-collegato.png")
            p.get_by_role("button", name="Manda una prova").click()
            p.wait_for_function("document.querySelector('#toast').textContent.includes('prova')")
            p.keyboard.press("Escape")

            # segna letti, poi torna ai film
            p.click("#lettiBtn")
            p.wait_for_selector("#reteBadge", state="hidden")
            controlla(True, "avvisi letti: il badge sparisce")
            p.click("#tabFilm")
            p.wait_for_selector("#filmView:not([hidden])")
            controlla(p.locator("#addBtn").is_visible() and not p.locator("#tgBtn").is_visible(), "tornato ai film con i loro strumenti")
            controlla(p.title() == "Videoteca", "titolo dei film")
            p.keyboard.press("/")
            controlla(p.evaluate("document.activeElement.id") == "q", "il tasto / cerca tra i film")
            ctx.close()

            # ---- telefono
            ctx = contesto(b, viewport={"width": 390, "height": 844}, device_scale_factor=2, user_agent=IPHONE_UA, is_mobile=True, has_touch=True)
            p = pagina(ctx, BASE + "/#rete")
            p.wait_for_selector("#reteAccesa:not([hidden]) .disp")
            largo = p.evaluate("document.documentElement.scrollWidth")
            controlla(largo <= 390, f"niente scorrimento orizzontale sul telefono ({largo}px)")
            p.screenshot(path=FOTO / "13-rete-telefono.png", full_page=True)
            ctx.close()

            # ---- controllo spento e collegamento di Telegram da zero
            ctx = contesto(b, viewport={"width": 1280, "height": 900})
            p = pagina(ctx, "http://127.0.0.1:8098/#rete")
            p.wait_for_selector("#reteSpenta:not([hidden])")
            controlla("rete.sh" in p.locator("#reteSpenta").inner_text(), "istruzioni per accendere il controllo")
            p.screenshot(path=FOTO / "14-rete-spenta.png")
            p.click("#tgBtn2")
            p.wait_for_selector("#tgToken")
            p.screenshot(path=FOTO / "15-telegram-passi.png")
            p.fill("#tgToken", "sbagliato")
            p.click("#tgBody button[type=submit]")
            p.wait_for_function("document.querySelector('#tgErrore').textContent.length > 0")
            controlla("token" in p.locator("#tgErrore").inner_text(), "errore chiaro per un token sbagliato")
            p.fill("#tgToken", rete_test.TOKEN)
            p.click("#tgBody button[type=submit]")
            p.wait_for_selector("#tgAttesa")
            controlla("@rete_casa_bot" in p.locator("#tgBody").inner_text(), "dopo il token: apri il bot")
            urllib.request.urlopen(urllib.request.Request(rete_test.TG + "/_start", data=b"{}"), timeout=5).read()
            p.wait_for_function("document.querySelector('#tgBody').textContent.includes('Collegato')", timeout=15000)
            controlla(True, "la pagina si accorge da sola del /start")
            p.wait_for_function("document.querySelector('#tgBtn2').textContent === 'Telegram collegato'", timeout=10000)
            controlla(True, "pulsante aggiornato")
            ctx.close()
            b.close()
    finally:
        spenta.terminate()
    controlla(not errori_js, f"nessun errore JavaScript {errori_js}")
    print("\nProblemi: " + ("nessuno" if not problemi else "; ".join(problemi)))
    return 1 if problemi else 0


if __name__ == "__main__":
    sys.exit(main())
