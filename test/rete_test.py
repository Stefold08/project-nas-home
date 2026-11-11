"""Prova completa del controllo della rete con un dnsmasq vero.

Serve: dnsmasq (pacchetto dnsmasq-base), l'utente stefold, e la Videoteca installata con
    VIDEOTECA_DIR=/tmp/vt5/app VIDEOTECA_LIBRARY=/tmp/vt5/film VIDEOTECA_USER=stefold VIDEOTECA_NO_SYSTEMD=1 \
    bash installa-videoteca.sh
"""
import json
import os
import random
import signal
import socket
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

QUI = Path(__file__).resolve().parent
sys.path.insert(0, str(QUI))
import dnsq  # noqa: E402

LAB = Path("/tmp/rete-test")
APP = Path(os.environ.get("APP", "/tmp/vt5/app"))
PORTA = 8099
BASE = f"http://127.0.0.1:{PORTA}"
TG = "http://127.0.0.1:9912"
TOKEN = "123456789:AAH" + "x" * 32
problemi = []
processi = []


def controlla(condizione, cosa):
    print(("ok       " if condizione else "FALLITO  ") + cosa, flush=True)
    if not condizione:
        problemi.append(cosa)


def avvia(argomenti, **kw):
    p = subprocess.Popen(argomenti, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    processi.append(p)
    return p


def aspetta_porta(porta, secondi=20):
    fine = time.time() + secondi
    while time.time() < fine:
        try:
            socket.create_connection(("127.0.0.1", porta), timeout=0.3).close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


def api(percorso, dati=None, base=BASE):
    corpo = None if dati is None else json.dumps(dati).encode()
    richiesta = urllib.request.Request(base + percorso, data=corpo,
                                       headers={"Content-Type": "application/json"} if corpo else {})
    try:
        with urllib.request.urlopen(richiesta, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def aspetta(funzione, secondi=20, passo=0.5):
    fine = time.time() + secondi
    while time.time() < fine:
        valore = funzione()
        if valore:
            return valore
        time.sleep(passo)
    return None


def chiedi(src, nome, tipo=1):
    try:
        return dnsq.ask(nome, src, server=("127.0.0.1", 5353), qtype=tipo)
    except OSError:
        return None


def avvisi(tipo=None):
    _, v = api("/api/rete")
    return [a for a in v.get("avvisi", []) if tipo is None or a["tipo"] == tipo]


def messaggi():
    with urllib.request.urlopen(TG + "/_messaggi", timeout=5) as r:
        return json.loads(r.read())


def dnsmasq_pi():
    return avvia(["dnsmasq", "--keep-in-foreground", "--port=5353", "--listen-address=127.0.0.1", "--bind-interfaces",
                  "--no-resolv", "--no-hosts", "--server=127.0.0.1#5354", "--log-queries=extra", "--log-async=50",
                  "--cache-size=1000", "--domain-needed", "--bogus-priv", f"--log-facility={LAB}/dns.log",
                  "--user=root", f"--pid-file={LAB}/dnspi.pid"])


def avvia_videoteca(porta=PORTA, attiva=True):
    ambiente = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "VIDEOTECA_PORT": str(porta),
        "VIDEOTECA_RETE_LOG_CMD": f"tail -n 0 -F {LAB}/dns.log",
        "VIDEOTECA_RETE_IP_CMD": str(QUI / "fakebin-rete" / "ip"),
        "VIDEOTECA_TELEGRAM_API": TG,
        "VIDEOTECA_RETE_LISTA_DOMINI": "http://127.0.0.1:9913/domini.txt",
        "VIDEOTECA_RETE_LISTA_IP": "http://127.0.0.1:9913/ip.txt",
        "VIDEOTECA_RETE_OUI": str(QUI / "oui-prova.txt"),
        "VIDEOTECA_RETE_DNS": "127.0.0.1:5353",
        "VIDEOTECA_RETE_APPRENDIMENTO": "6", "VIDEOTECA_RETE_SCANSIONE": "0", "VIDEOTECA_RETE_CONTROLLO_DNS": "2",
        "VIDEOTECA_RETE_CONF": f"{LAB}/non-esiste.conf",
    }
    if attiva:
        ambiente["VIDEOTECA_RETE_ATTIVA"] = "1"
    if porta != PORTA:
        ambiente["VIDEOTECA_RETE_STATO"] = f"{LAB}/stato-{porta}.json"
        config = json.loads((APP / "config.json").read_text())
        config.pop("rete", None)
        percorso = LAB / f"config-{porta}.json"
        percorso.write_text(json.dumps(config))
        os.chmod(percorso, 0o666)
        ambiente["VIDEOTECA_CONFIG"] = str(percorso)
    argomenti = ["runuser", "-u", "stefold", "--", "env", "-i"] + [f"{k}={v}" for k, v in ambiente.items()]
    p = subprocess.Popen(argomenti + ["python3", "-u", str(APP / "videoteca.py")],
                         stdout=open(LAB / f"videoteca-{porta}.log", "a"), stderr=subprocess.STDOUT)
    processi.append(p)
    controlla(aspetta_porta(porta), f"la Videoteca risponde sulla porta {porta}")
    return p


def casuale(n):
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def main():
    random.seed(7)
    subprocess.run(["pkill", "-x", "dnsmasq"], check=False)
    subprocess.run(["rm", "-rf", str(LAB)], check=True)
    (LAB / "liste").mkdir(parents=True)
    os.chmod(LAB, 0o777)
    (LAB / "dns.log").touch()
    os.chmod(LAB / "dns.log", 0o666)
    riempi = [f"riempitivo{i}.example" for i in range(150)]
    (LAB / "liste" / "domini.txt").write_text("# elenco di prova\n" + "\n".join(riempi + ["malware-test.example"]) + "\n")
    (LAB / "liste" / "ip.txt").write_text("# ip di prova\n" + "\n".join([f"203.0.114.{i}" for i in range(150)] + ["198.51.100.66"]) + "\n")
    for f in ("rete-stato.json",):
        (APP / f).unlink(missing_ok=True)
    subprocess.run(["rm", "-rf", str(APP / "liste")], check=True)

    avvia(["dnsmasq", "--keep-in-foreground", "--port=5354", "--listen-address=127.0.0.1", "--bind-interfaces", "--no-resolv",
           "--no-hosts", "--address=/c2-test.example/198.51.100.66", "--address=/nxtest/", "--address=/#/203.0.113.7",
           "--user=root", f"--pid-file={LAB}/dnsup.pid"])
    dns = dnsmasq_pi()
    avvia([sys.executable, str(QUI / "mock_telegram.py"), "9912"])
    avvia([sys.executable, "-m", "http.server", "9913", "--bind", "127.0.0.1", "--directory", str(LAB / "liste")])
    controlla(aspetta_porta(9912) and aspetta_porta(9913), "finti Telegram ed elenchi pronti")
    time.sleep(0.5)
    controlla(bool(chiedi("127.0.0.5", "www.prova.it")), "il dnsmasq di prova risponde")

    # ---------- la Videoteca con il controllo della rete
    videoteca = avvia_videoteca()
    stato = aspetta(lambda: (lambda v: v if v.get("attiva") and v["liste"]["domini"] > 100 else None)(api("/api/rete")[1]), 30)
    controlla(bool(stato), "controllo acceso ed elenchi scaricati")
    if stato:
        controlla(stato["liste"]["domini"] == 151 and stato["liste"]["ip"] == 151, f"elenchi caricati ({stato['liste']})")
        controlla(stato["apprendimento_fino"] > 0, "periodo di apprendimento in corso")

    # durante l'apprendimento: nessun avviso di dispositivo nuovo
    chiedi("127.0.0.5", "www.repubblica.it")
    chiedi("127.0.0.5", "www.google.com")
    time.sleep(7.5)
    _, v = api("/api/rete")
    controlla(any(d["mac"] == "f0:18:98:00:00:05" for d in v["dispositivi"]), "dispositivo riconosciuto dal MAC")
    controlla(not avvisi("nuovo"), "nessun avviso durante l'apprendimento")
    d5 = next((d for d in v["dispositivi"] if d["mac"] == "f0:18:98:00:00:05"), {})
    controlla(d5.get("produttore") == "Apple", f"produttore dal file OUI ({d5.get('produttore')})")
    controlla(d5.get("totale") == 2, f"attività contata ({d5.get('totale')})")

    # dopo l'apprendimento: dispositivo nuovo con indirizzo privato
    chiedi("127.0.0.6", "www.wikipedia.org")
    nuovo = aspetta(lambda: avvisi("nuovo"), 10)
    controlla(bool(nuovo), "avviso per il dispositivo nuovo")
    if nuovo:
        controlla("Indirizzo privato" in nuovo[0]["dettaglio"], f"testo del nuovo dispositivo: {nuovo[0]['dettaglio'][:90]}")

    # sito pericoloso (due nomi, un solo avviso)
    chiedi("127.0.0.5", "malware-test.example")
    chiedi("127.0.0.5", "download.malware-test.example")
    pericoli = aspetta(lambda: avvisi("pericolo"), 10)
    time.sleep(1)
    pericoli = avvisi("pericolo")
    controlla(len(pericoli) == 1 and "malware-test.example" in pericoli[0]["titolo"], f"un avviso per il sito pericoloso ({len(pericoli)})")

    # server pericoloso dalla risposta
    chiedi("127.0.0.7", "www.c2-test.example")
    server = aspetta(lambda: [a for a in avvisi("pericolo") if "198.51.100.66" in a["titolo"]], 10)
    controlla(bool(server), "avviso per l'indirizzo pericoloso nella risposta")

    # tanti nomi casuali inesistenti (DGA)
    for _ in range(26):
        chiedi("127.0.0.6", f"{casuale(14)}.nxtest")
    casuali = aspetta(lambda: [a for a in avvisi("strano") if "nomi a caso" in a["titolo"]], 10)
    controlla(bool(casuali), "avviso per i nomi casuali inesistenti")
    # nomi inesistenti normali: niente avviso
    for i in range(30):
        chiedi("127.0.0.8", f"stampante-ufficio-{i}.nxtest")
    time.sleep(2)
    controlla(len([a for a in avvisi("strano") if "nomi a caso" in a["titolo"]]) == 1, "niente avviso per nomi inesistenti normali")

    # tunnel DNS: nomi lunghissimi verso lo stesso dominio
    for _ in range(42):
        chiedi("127.0.0.7", f"{casuale(48)}.{casuale(20)}.tunnel-test.example", tipo=16)
    tunnel = aspetta(lambda: [a for a in avvisi("strano") if "tunnel" in a["titolo"]], 10)
    controlla(bool(tunnel), "avviso per il possibile tunnel DNS")

    _, v = api("/api/rete")
    controlla(v["da_leggere"] >= 5, f"avvisi da leggere: {v['da_leggere']}")
    controlla(sum(v["attivita"]) > 100, f"attività della rete contata ({sum(v['attivita'])})")
    controlla(len(v["ore"]) == 24 and len(v["attivita"]) == 24, "24 ore nel grafico")
    controlla(not any("chiave" in a for a in v["avvisi"]), "le chiavi interne degli avvisi non escono")
    _, conta = api("/api/rete/conta")
    controlla(conta["da_leggere"] == v["da_leggere"], "conteggio per il badge")

    # ---------- Telegram
    codice, r = api("/api/rete/telegram", {"token": "ciao"})
    controlla(codice == 400 and "token" in r.get("error", ""), f"token sbagliato rifiutato: {r.get('error')}")
    codice, r = api("/api/rete/telegram", {"token": "987654321:" + "y" * 35})
    controlla(codice == 400 and "non è valido" in r.get("error", ""), f"token che Telegram non conosce: {r.get('error')}")
    codice, r = api("/api/rete/telegram", {"token": TOKEN})
    controlla(codice == 200 and r.get("bot") == "rete_casa_bot" and not r.get("collegato"), f"token accettato ({r})")
    _, r = api("/api/rete/telegram/collega", {})
    controlla(not r.get("collegato"), "non collegato finché non premi Avvia")
    urllib.request.urlopen(urllib.request.Request(TG + "/_start", data=b"{}"), timeout=5).read()
    _, r = api("/api/rete/telegram/collega", {})
    controlla(r.get("collegato") and r.get("nome") == "Daniele", f"collegato dopo /start ({r})")
    controlla(any("Ciao Daniele" in m["text"] for m in messaggi()), "messaggio di benvenuto su Telegram")
    _, r = api("/api/rete/telegram/prova", {})
    controlla(any("Prova riuscita" in m["text"] for m in messaggi()), "messaggio di prova su Telegram")
    config = json.loads((APP / "config.json").read_text())
    controlla(config.get("rete", {}).get("telegram_chat") == 4242, "chat salvata nella configurazione")
    _, v = api("/api/rete")
    controlla("token" not in json.dumps(v["telegram"]).replace('"token": true', ""), "il token non esce dalla pagina")

    # un avviso nuovo arriva su Telegram
    chiedi("127.0.0.8", "malware-test.example")
    arrivato = aspetta(lambda: [m for m in messaggi() if "malware-test.example" in m["text"]], 15)
    controlla(bool(arrivato) and arrivato[0]["chat_id"] == 4242, "l'avviso arriva su Telegram")

    # ---------- nomi, letti, ignorati
    codice, _ = api("/api/rete/nome", {"chiave": "f0:18:98:00:00:05", "nome": "  Telefono   di prova  "})
    _, v = api("/api/rete")
    d5 = next((d for d in v["dispositivi"] if d["chiave"] == "f0:18:98:00:00:05"), {})
    controlla(codice == 200 and d5.get("mostrato") == "Telefono di prova", f"nome del dispositivo ({d5.get('mostrato')})")
    codice, _ = api("/api/rete/nome", {"chiave": "nessuno", "nome": "x"})
    controlla(codice == 404, "nome per un dispositivo che non c'è: 404")
    primo = v["avvisi"][0]
    codice, _ = api("/api/rete/ignora", {"id": primo["id"]})
    _, v = api("/api/rete")
    controlla(codice == 200 and next(a for a in v["avvisi"] if a["id"] == primo["id"])["letto"], "avviso ignorato e segnato come letto")
    api("/api/rete/letti", {})
    _, v = api("/api/rete")
    controlla(v["da_leggere"] == 0, "tutti gli avvisi segnati come letti")

    # ---------- il DNS del Pi si ferma e riparte
    dns.send_signal(signal.SIGTERM)
    dns.wait(5)
    giu = aspetta(lambda: [a for a in avvisi("sistema") if "non risponde" in a["titolo"]], 20)
    controlla(bool(giu), "avviso quando il DNS del Pi non risponde")
    controlla(bool(aspetta(lambda: [m for m in messaggi() if "non risponde" in m["text"]], 10)), "avviso del DNS giù su Telegram")
    dns = dnsmasq_pi()
    su = aspetta(lambda: [a for a in avvisi("sistema") if "di nuovo" in a["titolo"]], 20)
    controlla(bool(su), "avviso quando il DNS del Pi torna")

    # ---------- dopo un riavvio della Videoteca resta tutto
    nuovi_prima = len(avvisi("nuovo"))
    videoteca.terminate()
    videoteca.wait(10)
    videoteca = avvia_videoteca()
    v = aspetta(lambda: (lambda x: x if x.get("attiva") else None)(api("/api/rete")[1]), 15)
    d5 = next((d for d in (v or {}).get("dispositivi", []) if d["chiave"] == "f0:18:98:00:00:05"), {})
    controlla(d5.get("mostrato") == "Telefono di prova", "il nome resta dopo il riavvio")
    controlla(len((v or {}).get("avvisi", [])) >= 7, f"gli avvisi restano dopo il riavvio ({len((v or {}).get('avvisi', []))})")
    controlla(not (v or {}).get("apprendimento_fino"), "dopo il riavvio niente nuovo apprendimento")
    chiedi("127.0.0.6", "malware-test.example")
    time.sleep(3)
    controlla(len(avvisi("nuovo")) == nuovi_prima == 3, f"nessun avviso doppio per dispositivi già conosciuti ({nuovi_prima})")

    # ---------- senza controllo della rete la Videoteca funziona come prima
    spenta = avvia_videoteca(porta=8098, attiva=False)
    codice, v = api("/api/rete", base="http://127.0.0.1:8098")
    controlla(codice == 200 and v["configurata"] is False and v["attiva"] is False, "pagina Rete con il controllo spento")
    codice, _ = api("/api/library", base="http://127.0.0.1:8098")
    controlla(codice == 200, "i film funzionano anche col controllo spento")
    spenta.terminate()
    return videoteca


if __name__ == "__main__":
    try:
        main()
        if "--lascia" in sys.argv:
            print("Lascio accesi i servizi per la prova della pagina.", flush=True)
            processi[:] = []
    finally:
        for p in processi:
            if p.poll() is None:
                p.terminate()
        print("\nProblemi: " + ("nessuno" if not problemi else "; ".join(problemi)))
        sys.exit(1 if problemi else 0)
