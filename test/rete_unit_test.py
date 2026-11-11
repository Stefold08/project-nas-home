"""Prove veloci delle parti del controllo della rete che non servono un DNS vero."""
import ipaddress
import os
import sys
import tempfile
import time
from pathlib import Path

QUI = Path(__file__).resolve().parent
os.environ["VIDEOTECA_RETE_IP_CMD"] = str(QUI / "fakebin-rete" / "ip")
os.environ["VIDEOTECA_RETE_OUI"] = str(QUI / "oui-prova.txt")
os.environ["VIDEOTECA_RETE_APPRENDIMENTO"] = "0"
sys.path.insert(0, str(QUI.parent / "videoteca"))
import rete  # noqa: E402

problemi = []


def controlla(condizione, cosa):
    print(("ok       " if condizione else "FALLITO  ") + cosa)
    if not condizione:
        problemi.append(cosa)


cartella = Path(tempfile.mkdtemp())
cfg = {}
r = rete.Rete(cfg, lambda: None, cartella)
r.da = time.time() - 2 * rete.GIORNO
r._leggi_indirizzi()
controlla(r.gateway == "192.0.2.1" and "192.0.2.10" in r.miei_ip, "indirizzi del Pi e del router")
controlla(str(r.rete_locale) == "192.0.2.0/24", f"rete di casa ({r.rete_locale})")

# scansione: bussa agli indirizzi e segna chi c'è (solo gli stati presenti)
r.rete_locale = ipaddress.ip_network("127.0.0.0/29")
inizio = time.time()
vecchio_sleep = rete.time.sleep
rete.time.sleep = lambda s: None
r._scansiona()
rete.time.sleep = vecchio_sleep
visti = {d["mac"] for d in r.dispositivi.values() if d.get("ultimo_visto_rete", 0) >= inizio}
controlla(visti == {"f0:18:98:00:00:05", "00:1a:2b:00:00:07", "00:1a:2b:00:00:08", "00:24:d4:00:00:01"},
          f"la scansione trova i dispositivi presenti ({sorted(visti)})")
controlla("3a:11:22:33:44:55" not in visti, "un dispositivo «STALE» non conta come presente")
pass
router = r.dispositivi["00:24:d4:00:00:01"]
router["ip"] = "192.0.2.1"
controlla(r._nome(router) == "Router di casa", "il router riconosciuto dall'indirizzo")
controlla(not [a for a in r.avvisi if a["dispositivo"] == "00:24:d4:00:00:01"], "nessun avviso «nuovo» per il router")

# un dispositivo in rete che non chiede mai nomi al Pi
d7 = r.dispositivi["00:1a:2b:00:00:07"]
d7["ultimo_dns"] = 0
vista = r.vista()
x7 = next(x for x in vista["dispositivi"] if x["chiave"] == "00:1a:2b:00:00:07")
controlla(x7["senza_dns"], "segnalato chi non usa il DNS del Pi")
controlla(x7["produttore"] == "Samsung Electronics", f"nome del produttore ripulito ({x7['produttore']})")

# avvisi: doppioni, ignorati
d5 = r.dispositivi["f0:18:98:00:00:05"]
r._avviso("pericolo", d5, "Prova", "dettaglio", "pericolo|x|y", rete.GIORNO)
r._avviso("pericolo", d5, "Prova", "dettaglio", "pericolo|x|y", rete.GIORNO)
controlla(len([a for a in r.avvisi if a["titolo"] == "Prova"]) == 1, "niente avvisi doppi nello stesso giorno")
r.ignora(r.avvisi[0]["id"])
r.ultimi.clear()
r._avviso("pericolo", d5, "Prova", "dettaglio", "pericolo|x|y", rete.GIORNO)
controlla(len([a for a in r.avvisi if a["titolo"] == "Prova"]) == 1, "un avviso ignorato non torna")

# Telegram: al massimo 20 messaggi l'ora, poi un riassunto
cfg["rete"] = {"telegram_token": "1", "telegram_chat": 5}
r.inviati.extend([time.time()] * rete.MAX_TELEGRAM_ORA)
r.saltati = 7
r._riassunto_telegram()
controlla(r.coda.empty() and r.saltati == 7, "riassunto rimandato finché l'ora è piena")
r.inviati.clear()
r._riassunto_telegram()
controlla(not r.coda.empty() and "Altri 7 avvisi" in r.coda.get(), "riassunto degli avvisi non mandati")

# salvataggio e rilettura
r._salva()
r2 = rete.Rete({}, lambda: None, cartella)
controlla(len(r2.dispositivi) == len(r.dispositivi) and r2.ignorati == r.ignorati, "stato salvato e riletto")
controlla("pericolo|x|y" in r2.ultimi, "dopo il riavvio ricorda gli avvisi già mandati")

# righe strane del registro non fanno danni
for riga in ["", "dnsmasq: started", "1 2 3", "x y/1 query[A]", "12 1.2.3.4/5 reply example.com is <CNAME>"]:
    try:
        r.elabora(riga)
        esito = True
    except Exception as exc:  # noqa: BLE001
        esito = False
        print(exc)
    controlla(esito, f"riga ignorata senza errori: {riga!r}")

print("\nProblemi: " + ("nessuno" if not problemi else "; ".join(problemi)))
sys.exit(1 if problemi else 0)
