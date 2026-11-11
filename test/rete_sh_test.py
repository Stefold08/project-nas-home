"""Prova di rete.sh con apt, systemctl e journalctl finti e un dnsmasq vero sulla porta 53 (serve root)."""
import os
import subprocess
import sys
from pathlib import Path

QUI = Path(__file__).resolve().parent
SCRIPT = QUI.parent / "rete.sh"
LAB = Path("/tmp/rete-sh")
CONF = Path("/etc/dnsmasq.d/videoteca.conf")
OVERRIDE = Path("/etc/systemd/system/dnsmasq.service.d/videoteca.conf")
problemi = []


def controlla(condizione, cosa):
    print(("ok       " if condizione else "FALLITO  ") + cosa, flush=True)
    if not condizione:
        problemi.append(cosa)


def lancia(*argomenti, entrata=""):
    ambiente = dict(os.environ, PATH=f"{QUI / 'fakebin-retesh'}:/usr/sbin:/usr/bin:/bin", VIDEOTECA_DIR="/tmp/vt5/app")
    return subprocess.run(["bash", str(SCRIPT), *argomenti], input=entrata, capture_output=True, text=True,
                          env=ambiente, timeout=120)


subprocess.run(["pkill", "-x", "dnsmasq"], check=False)
subprocess.run(["rm", "-rf", str(LAB)], check=True)
LAB.mkdir()
try:
    (LAB / "porta-occupata").touch()
    r = lancia()
    controlla(r.returncode == 1 and "già usata" in r.stdout and not CONF.exists(), "si ferma se la porta 53 è occupata")
    (LAB / "porta-occupata").unlink()

    r = lancia()
    print(r.stdout[-1600:])
    controlla(r.returncode == 0, f"accensione riuscita (codice {r.returncode}) {r.stderr[-300:]}")
    controlla(CONF.exists() and "log-queries=extra" in CONF.read_text() and "server=9.9.9.9" in CONF.read_text(),
              "configurazione di dnsmasq scritta")
    controlla(OVERRIDE.exists() and "Restart=always" in OVERRIDE.read_text(), "dnsmasq riparte da solo se si ferma")
    registro = (LAB / "registro").read_text()
    controlla("apt-get install -y dnsmasq" in registro and "systemctl enable dnsmasq" in registro, "dnsmasq installato e abilitato")
    controlla("MAC" in r.stdout and "192.168.1.37" in r.stdout and "http://192.168.1.254" in r.stdout, "istruzioni per l'Iliadbox con IP e router")
    controlla("fd12:3456::37" in r.stdout and "2a01:e0a:1:2::99" not in r.stdout, "indirizzo IPv6 stabile (non quello temporaneo)")
    controlla("http://192.168.1.37:8080/#rete" in r.stdout, "indirizzo della pagina Rete")
    controlla("Attenzione" not in r.stdout, "il DNS registra le richieste")

    r = lancia("--spegni", entrata="no\n")
    controlla(r.returncode == 1 and CONF.exists(), "senza conferma non spegne niente")
    r = lancia("--spegni", entrata="si\n")
    controlla(r.returncode == 0 and not CONF.exists() and not OVERRIDE.exists(), "spegnimento con conferma")
    controlla("systemctl disable --now dnsmasq" in (LAB / "registro").read_text(), "dnsmasq fermato e disabilitato")
    r = lancia("--boh")
    controlla(r.returncode == 2 and "Uso" in r.stdout, "opzione sconosciuta")
finally:
    CONF.unlink(missing_ok=True)
    OVERRIDE.unlink(missing_ok=True)
    subprocess.run(["pkill", "-x", "dnsmasq"], check=False)

print("\nProblemi: " + ("nessuno" if not problemi else "; ".join(problemi)))
sys.exit(1 if problemi else 0)
