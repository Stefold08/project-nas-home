"""Controllo della rete di casa per la Videoteca.

Il Pi fa da DNS per la casa con dnsmasq (lo accende rete.sh). Questo modulo legge
il registro delle richieste, riconosce i dispositivi, confronta i nomi con
l'elenco dei siti pericolosi e manda un avviso su Telegram quando trova qualcosa
di strano. Non guarda il contenuto del traffico: solo quali nomi e indirizzi
chiede ogni dispositivo.
"""

import array
import bisect
import collections
import http.client
import ipaddress
import json
import math
import os
import queue
import random
import re
import secrets
import shlex
import socket
import ssl
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DNSMASQ_CONF = Path(os.environ.get("VIDEOTECA_RETE_CONF") or "/etc/dnsmasq.d/videoteca.conf")
LOG_CMD = os.environ.get("VIDEOTECA_RETE_LOG_CMD") or "journalctl -u dnsmasq -f -n 0 -o cat"
IP_CMD = os.environ.get("VIDEOTECA_RETE_IP_CMD") or "ip"
TELEGRAM_API = (os.environ.get("VIDEOTECA_TELEGRAM_API") or "https://api.telegram.org").rstrip("/")
URL_DOMINI = (os.environ.get("VIDEOTECA_RETE_LISTA_DOMINI")
              or "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/wildcard/tif.mini-onlydomains.txt")
URL_IP = (os.environ.get("VIDEOTECA_RETE_LISTA_IP")
          or "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/ips/tif.txt")
OUI_PATH = Path(os.environ.get("VIDEOTECA_RETE_OUI") or "/usr/share/ieee-data/oui.txt")
DNS_PROVA = os.environ.get("VIDEOTECA_RETE_DNS") or "127.0.0.1:53"
APPRENDIMENTO = int(os.environ.get("VIDEOTECA_RETE_APPRENDIMENTO") or 3600)
SCANSIONE = int(os.environ.get("VIDEOTECA_RETE_SCANSIONE") or 600)
CONTROLLO_DNS = int(os.environ.get("VIDEOTECA_RETE_CONTROLLO_DNS") or 300)
FORZA_ATTIVA = os.environ.get("VIDEOTECA_RETE_ATTIVA") == "1"

ORA = 3600
GIORNO = 24 * ORA
FINESTRA = 600                  # i comportamenti strani si contano su 10 minuti
SOGLIA_CASUALI = 20             # nomi inesistenti dall'aspetto casuale in 10 minuti
SOGLIA_TUNNEL = 40              # nomi lunghissimi verso lo stesso dominio in 10 minuti
MAX_AVVISI = 300
MAX_TELEGRAM_ORA = 20
MAX_LISTA = 60 * 1024 * 1024
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
MAC_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
NOMI_LOCALI = (".arpa", ".local", ".lan", ".home", ".internal", ".localdomain", ".localhost")
SUFFISSI_DOPPI = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "com.au", "net.au", "org.au", "co.nz",
    "co.jp", "ne.jp", "com.br", "com.cn", "com.tr", "co.in", "co.za", "com.mx", "com.ar",
    "gov.it", "edu.it", "co.it",
}
# Servizi che usano apposta nomi lunghissimi (antivirus e liste antispam): non sono tunnel.
TUNNEL_NORMALI = {
    "sophosxl.net", "mcafee.com", "trendmicro.com", "spamhaus.org", "spamcop.net",
    "surbl.org", "uribl.com", "dnswl.org", "barracudacentral.org", "kaspersky.com",
}
ICONE = {"pericolo": "⚠️", "strano": "\U0001f50e", "nuovo": "\U0001f195", "sistema": "ℹ️"}
VOCALI = set("aeiouy")
STATI_PRESENTI = {"REACHABLE", "DELAY", "PROBE", "PERMANENT", "NOARP"}


class ReteErrore(Exception):
    """Errore da mostrare nella pagina Rete, con il codice HTTP da restituire."""

    def __init__(self, messaggio, status=400):
        super().__init__(messaggio)
        self.status = status


class TelegramErrore(Exception):
    def __init__(self, messaggio, codice=0, attesa=0):
        super().__init__(messaggio)
        self.codice = codice
        self.attesa = attesa


# ---------------------------------------------------------------- elenchi

class ElencoDomini:
    """Elenco dei domini pericolosi, tenuto in memoria come impronte ordinate (8 byte l'uno)."""

    def __init__(self, impronte=None):
        self.impronte = impronte if impronte is not None else array.array("q")

    @classmethod
    def da_file(cls, path):
        valori = []
        with open(path, encoding="utf-8", errors="replace") as fh:
            for riga in fh:
                riga = riga.strip().lower()
                if not riga or riga[0] in "#!" or " " in riga:
                    continue
                if riga.startswith("*."):
                    riga = riga[2:]
                valori.append(hash(riga))
        valori.sort()
        return cls(array.array("q", valori))

    def __len__(self):
        return len(self.impronte)

    def trova(self, nome):
        """Restituisce la parte del nome che sta nell'elenco (il nome o un suo dominio padre)."""
        etichette = nome.split(".")
        for i in range(len(etichette) - 1):
            candidato = ".".join(etichette[i:])
            impronta = hash(candidato)
            j = bisect.bisect_left(self.impronte, impronta)
            if j < len(self.impronte) and self.impronte[j] == impronta:
                return candidato
        return None


class ElencoIP:
    """Elenco degli indirizzi IPv4 pericolosi, come numeri ordinati (4 byte l'uno)."""

    def __init__(self, valori=None):
        self.valori = valori if valori is not None else array.array("I")

    @classmethod
    def da_file(cls, path):
        valori = []
        with open(path, encoding="utf-8", errors="replace") as fh:
            for riga in fh:
                riga = riga.strip()
                if not riga or riga[0] == "#":
                    continue
                testo = riga.split()[0]
                if IPV4_RE.match(testo):
                    try:
                        valori.append(struct.unpack("!I", socket.inet_aton(testo))[0])
                    except OSError:
                        pass
        valori.sort()
        return cls(array.array("I", valori))

    def __len__(self):
        return len(self.valori)

    def contiene(self, ip):
        if not IPV4_RE.match(ip):
            return False
        try:
            numero = struct.unpack("!I", socket.inet_aton(ip))[0]
        except OSError:
            return False
        j = bisect.bisect_left(self.valori, numero)
        return j < len(self.valori) and self.valori[j] == numero


# ---------------------------------------------------------------- piccoli aiuti

def analizza_riga(riga):
    """Legge una riga del registro di dnsmasq (log-queries=extra).

    Restituisce ("query", client, nome, tipo) oppure ("risposta", client, nome, risposta),
    oppure None per le righe che non interessano.
    """
    testo = riga.strip()
    if "]: " in testo[:80]:                       # riga di file: 'Nov 11 21:46:09 dnsmasq[263]: ...'
        testo = testo.split("]: ", 1)[1]
    parti = testo.split(" ")
    if len(parti) < 4 or not parti[0].isdigit() or "/" not in parti[1]:
        return None
    client = normalizza_ip(parti[1].rsplit("/", 1)[0])
    azione = parti[2]
    nome = parti[3].rstrip(".").lower()
    if azione.startswith("query[") and azione.endswith("]"):
        return ("query", client, nome, azione[6:-1])
    if azione in ("reply", "cached", "config") and len(parti) >= 6 and parti[4] == "is":
        return ("risposta", client, nome, parti[5])
    return None


def normalizza_ip(ip):
    ip = ip.split("%", 1)[0].lower()
    if ip.startswith("::ffff:") and IPV4_RE.match(ip[7:]):
        return ip[7:]
    return ip


def dominio_base(nome):
    etichette = nome.split(".")
    if len(etichette) >= 3 and ".".join(etichette[-2:]) in SUFFISSI_DOPPI:
        return ".".join(etichette[-3:])
    return ".".join(etichette[-2:])


def sembra_casuale(etichetta):
    """Vero per etichette tipo 'xjwqkdhbvzpt' o 'a1b2c3d4e5': lunghe, disordinate, senza vocali."""
    s = etichetta.lower()
    if len(s) < 10 or not s.isascii():
        return False
    lettere = [c for c in s if c.isalpha()]
    if len(lettere) < 6:
        return False
    conta = collections.Counter(s)
    entropia = -sum(n / len(s) * math.log2(n / len(s)) for n in conta.values())
    vocali = sum(c in VOCALI for c in lettere) / len(lettere)
    cifre = sum(c.isdigit() for c in s)
    return entropia >= 3.0 and (vocali < 0.3 or cifre >= 3)


def nome_locale(nome):
    return "." not in nome or nome.endswith(NOMI_LOCALI)


def domanda_dns(nome, tipo=1):
    ident = random.randint(0, 0xFFFF)
    testa = struct.pack(">HHHHHH", ident, 0x0100, 1, 0, 0, 0)
    corpo = b"".join(bytes([len(p)]) + p.encode("ascii") for p in nome.strip(".").split(".")) + b"\0"
    return ident, testa + corpo + struct.pack(">HH", tipo, 1)


def chiedi_dns(server, nome, timeout=3.0):
    host, _, porta = server.rpartition(":")
    ident, pacchetto = domanda_dns(nome)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(pacchetto, (host, int(porta)))
        dati, _ = s.recvfrom(4096)
    if len(dati) < 12 or struct.unpack(">H", dati[:2])[0] != ident:
        raise OSError("risposta DNS non valida")
    return dati


def _salta_nome(dati, i):
    while True:
        n = dati[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:
            return i + 2
        i += n + 1


def indirizzi_a(dati):
    domande, risposte = struct.unpack(">HH", dati[4:8])
    i = 12
    for _ in range(domande):
        i = _salta_nome(dati, i) + 4
    trovati = []
    for _ in range(risposte):
        i = _salta_nome(dati, i)
        tipo, _classe, _ttl, lunghezza = struct.unpack(">HHIH", dati[i:i + 10])
        i += 10
        if tipo == 1 and lunghezza == 4:
            trovati.append(socket.inet_ntoa(dati[i:i + 4]))
        i += lunghezza
    return trovati


class _HTTPSVersoIP(http.client.HTTPSConnection):
    """Connessione HTTPS a un indirizzo già risolto, con il nome giusto per il certificato."""

    def __init__(self, host, ip, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port or 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _post_senza_dns_di_casa(url, corpo, timeout):
    """Se il DNS del Pi è giù, raggiunge comunque Telegram chiedendo l'indirizzo a Quad9 o Cloudflare."""
    parti = urllib.parse.urlsplit(url)
    percorso = parti.path + ("?" + parti.query if parti.query else "")
    for server in ("9.9.9.9:53", "1.1.1.1:53"):
        try:
            indirizzi = indirizzi_a(chiedi_dns(server, parti.hostname))
        except (OSError, struct.error, IndexError):
            continue
        for ip in indirizzi:
            con = _HTTPSVersoIP(parti.hostname, ip, timeout)
            try:
                con.request("POST", percorso, body=corpo, headers={"Content-Type": "application/json"})
                risposta = con.getresponse()
                return risposta.status, risposta.read()
            except OSError:
                continue
            finally:
                con.close()
    raise OSError(f"non trovo l'indirizzo di {parti.hostname}")


def pulisci_produttore(nome):
    nome = re.sub(r"[\s,]+(inc\.?|co\.?,?\s*ltd\.?|ltd\.?|corporation|corp\.?|gmbh|llc|s\.?p\.?a\.?|limited|b\.v\.|ag)$", "",
                  nome.strip(), flags=re.I)
    return nome.strip(" ,.") or "Produttore sconosciuto"


def scrivi_atomico(path, dati):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(dati)
    os.replace(tmp, path)


# ---------------------------------------------------------------- il controllo della rete

class Rete:
    def __init__(self, cfg, salva_cfg, cartella):
        self.cfg = cfg
        self.salva_cfg = salva_cfg
        self.cartella = Path(cartella)
        self.file_stato = Path(os.environ.get("VIDEOTECA_RETE_STATO") or self.cartella / "rete-stato.json")
        self.cartella_liste = self.cartella / "liste"
        self.lock = threading.RLock()
        self.lock_salva = threading.Lock()
        self.dispositivi = {}
        self.avvisi = []
        self.ignorati = set()
        self.rete_ore = {}
        self.da = 0.0
        self.liste_info = {"domini": 0, "ip": 0, "aggiornate": 0, "errore": ""}
        self.domini = ElencoDomini()
        self.ip_pericolosi = ElencoIP()
        self.ultimi = {}
        self.nx = collections.defaultdict(collections.deque)
        self.lunghi = collections.defaultdict(collections.deque)
        self.ip_mac = {}
        self.ultima_lettura_vicini = 0.0
        self.miei_ip = {"127.0.0.1", "::1"}
        self.gateway = ""
        self.rete_locale = None
        self.attiva = False
        self.processo = None
        self.registro = {"stato": "spento", "errore": ""}
        self.dns = {"ok": None, "fallimenti": 0, "avvisato": False}
        self.tabella_oui = None
        self.produttori = {}
        self.coda = queue.Queue()
        self.inviati = collections.deque()
        self.saltati = 0
        self.telegram_errore = ""
        self.telegram_attesa = 0.0
        self.sporco = False
        self.avviato = False
        self._carica_stato()

    # ---- configurazione e stato su disco

    def _c(self):
        c = self.cfg.get("rete")
        if not isinstance(c, dict):
            c = {}
            self.cfg["rete"] = c
        return c

    def _carica_stato(self):
        try:
            with open(self.file_stato, encoding="utf-8") as fh:
                dati = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            print(f"Rete: non riesco a leggere {self.file_stato}: {exc}", flush=True)
            return
        self.da = float(dati.get("da") or 0)
        for chiave, d in (dati.get("dispositivi") or {}).items():
            d["ore"] = {int(h): n for h, n in (d.get("ore") or {}).items()}
            self.dispositivi[chiave] = d
        self.avvisi = list(dati.get("avvisi") or [])[:MAX_AVVISI]
        self.ignorati = set(dati.get("ignorati") or [])
        self.rete_ore = {int(h): n for h, n in (dati.get("rete_ore") or {}).items()}
        self.liste_info.update(dati.get("liste") or {})
        for a in reversed(self.avvisi):
            self.ultimi[a.get("chiave", "")] = a.get("ts", 0)

    def _salva(self):
        limite = time.time() - 2 * GIORNO
        with self.lock:
            dispositivi = {}
            for chiave, d in self.dispositivi.items():
                copia = dict(d)
                copia["ore"] = {str(h): n for h, n in d.get("ore", {}).items() if h >= limite}
                dispositivi[chiave] = copia
            dati = {
                "versione": 1, "da": self.da, "dispositivi": dispositivi, "avvisi": self.avvisi,
                "ignorati": sorted(self.ignorati), "liste": self.liste_info,
                "rete_ore": {str(h): n for h, n in self.rete_ore.items() if h >= limite},
            }
            self.sporco = False
        try:
            with self.lock_salva:
                scrivi_atomico(self.file_stato, json.dumps(dati, ensure_ascii=False).encode("utf-8"))
        except OSError as exc:
            print(f"Rete: non riesco a salvare {self.file_stato}: {exc}", flush=True)

    # ---- avvio e ciclo principale

    def avvia(self):
        if self.avviato:
            return
        self.avviato = True
        threading.Thread(target=self._ciclo, name="rete-ciclo", daemon=True).start()
        threading.Thread(target=self._postino, name="rete-telegram", daemon=True).start()

    def configurata(self):
        return FORZA_ATTIVA or DNSMASQ_CONF.exists()

    def _accendi(self):
        self.attiva = True
        if not self.da:
            self.da = time.time()
            self.sporco = True
        self._carica_produttori()
        self._leggi_indirizzi()
        self._carica_liste()
        threading.Thread(target=self._leggi_registro, name="rete-dns", daemon=True).start()
        print("Rete: controllo della rete acceso.", flush=True)

    def _spegni(self):
        self.attiva = False
        self.registro = {"stato": "spento", "errore": ""}
        if self.processo and self.processo.poll() is None:
            self.processo.terminate()
        print("Rete: controllo della rete spento.", flush=True)

    def _ciclo(self):
        prossimo = collections.defaultdict(float)
        prossimo["salva"] = time.time() + 300
        while True:
            try:
                ora = time.time()
                if self.configurata() and not self.attiva:
                    self._accendi()
                elif not self.configurata() and self.attiva:
                    self._spegni()
                if self.attiva:
                    if ora >= prossimo["indirizzi"]:
                        self._leggi_indirizzi()
                        prossimo["indirizzi"] = ora + 600
                    if ora >= prossimo["liste"]:
                        prossimo["liste"] = ora + self._aggiorna_liste()
                    if SCANSIONE and ora >= prossimo["scansione"]:
                        prossimo["scansione"] = ora + SCANSIONE
                        self._scansiona()
                    if ora >= prossimo["dns"]:
                        prossimo["dns"] = ora + CONTROLLO_DNS
                        self._controlla_dns()
                    self._pulisci(ora)
                self._riassunto_telegram()
                if self.sporco and ora >= prossimo["salva"]:
                    prossimo["salva"] = ora + 300
                    self._salva()
            except Exception as exc:  # il controllo della rete non deve mai fermare la Videoteca
                print(f"Rete: errore nel ciclo: {exc!r}", flush=True)
            time.sleep(2)

    # ---- lettura del registro di dnsmasq

    def _leggi_registro(self):
        while self.attiva:
            ultima = ""
            try:
                self.processo = subprocess.Popen(
                    shlex.split(LOG_CMD), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, errors="replace", bufsize=1)
            except OSError as exc:
                self.registro = {"stato": "errore", "errore": f"Non riesco a leggere il registro del DNS: {exc}"}
                time.sleep(30)
                continue
            self.registro = {"stato": "ok", "errore": ""}
            for riga in self.processo.stdout:
                if not self.attiva:
                    break
                try:
                    if not self.elabora(riga):
                        ultima = riga.strip() or ultima
                except Exception as exc:
                    print(f"Rete: riga non capita {riga!r}: {exc!r}", flush=True)
            if self.processo.poll() is None:
                self.processo.terminate()
            codice = self.processo.wait()
            if self.attiva:
                self.registro = {"stato": "errore",
                                 "errore": f"Il registro del DNS si è interrotto ({ultima[-200:] or codice}). Riprovo da solo."}
                time.sleep(15)

    def elabora(self, riga, ora=None):
        """Elabora una riga del registro. Restituisce True se era una richiesta o una risposta."""
        letta = analizza_riga(riga)
        if not letta:
            return False
        tipo, client, nome, extra = letta
        ora = ora or time.time()
        chiave = self._chiave_per_ip(client, ora)
        with self.lock:
            d = self._dispositivo(chiave, client, ora)
            if tipo == "query":
                self._conta(d, ora)
                self._controlla_nome(d, nome, ora)
            elif extra == "NXDOMAIN":
                self._registra_nx(d, nome, ora)
            else:
                self._controlla_ip(d, nome, extra, ora)
        return True

    # ---- dispositivi

    def _chiave_per_ip(self, ip, ora):
        if ip in self.miei_ip:
            return "pi"
        mac = self.ip_mac.get(ip)
        if not mac and ora - self.ultima_lettura_vicini > 5:
            self._leggi_vicini()
            mac = self.ip_mac.get(ip)
        if mac:
            return mac
        try:
            indirizzo = ipaddress.ip_address(ip)
        except ValueError:
            return "ip:" + ip
        if indirizzo in TAILSCALE_V4 or indirizzo in TAILSCALE_V6:
            return "ts:" + ip
        if indirizzo.version == 6:
            return "ipv6"
        return "ip:" + ip

    def _dispositivo(self, chiave, ip, ora):
        d = self.dispositivi.get(chiave)
        nuovo = d is None
        if nuovo:
            mac = chiave if MAC_RE.match(chiave) else ""
            d = {"chiave": chiave, "mac": mac, "nome": "", "produttore": self.produttore(mac) if mac else "",
                 "ip": "", "ip6": "", "primo": ora, "ultimo": ora, "ultimo_dns": 0, "ultimo_visto_rete": 0, "ore": {}}
            self.dispositivi[chiave] = d
            self.sporco = True
        if ip and ip not in self.miei_ip:
            if ":" in ip:
                d["ip6"] = ip
            elif chiave != "pi":
                d["ip"] = ip
        d["ultimo"] = max(d.get("ultimo", 0), ora)
        if nuovo and d["mac"] and self.da and ora >= self.da + APPRENDIMENTO and self._tipo(d) == "dispositivo":
            self._avviso("nuovo", d, "Nuovo dispositivo in rete",
                         f"{self._chi(d)} si è collegato per la prima volta. Se non lo riconosci, "
                         "controlla chi è; se è tuo, dagli un nome nella pagina Rete della Videoteca.",
                         f"nuovo|{d['mac']}", None)
        return d

    def _nome(self, d):
        if not d:
            return "Il Pi"
        if d.get("nome"):
            return d["nome"]
        chiave = d.get("chiave", "")
        if chiave == "pi":
            return "Questo Pi"
        if chiave == "ipv6":
            return "Dispositivi IPv6 non riconosciuti"
        if chiave.startswith("ts:"):
            return f"Dispositivo Tailscale {chiave[3:]}"
        if self.gateway and d.get("ip") == self.gateway:
            return "Router di casa"
        return d.get("produttore") or "Dispositivo sconosciuto"

    def _chi(self, d):
        ip = d.get("ip") or d.get("ip6") or ""
        return f"«{self._nome(d)}»" + (f" ({ip})" if ip and not d.get("chiave", "").startswith(("pi", "ts:")) else "")

    def _tipo(self, d):
        chiave = d.get("chiave", "")
        if chiave == "pi":
            return "pi"
        if chiave.startswith("ts:"):
            return "tailscale"
        if self.gateway and d.get("ip") == self.gateway:
            return "router"
        return "dispositivo"

    def _carica_produttori(self):
        """Legge una volta sola l'elenco dei produttori di schede di rete (pacchetto ieee-data)."""
        tabella, nomi = {}, {}
        try:
            with open(OUI_PATH, encoding="utf-8", errors="replace") as fh:
                for riga in fh:
                    if "(hex)" not in riga:
                        continue
                    prefisso, _, nome = riga.partition("(hex)")
                    prefisso = prefisso.strip().replace("-", "")
                    if len(prefisso) == 6:
                        try:
                            nome = nome.strip()
                            tabella[int(prefisso, 16)] = nomi.setdefault(nome, nome)
                        except ValueError:
                            pass
        except OSError:
            pass
        self.tabella_oui = tabella

    def produttore(self, mac):
        if mac in self.produttori:
            return self.produttori[mac]
        if int(mac[:2], 16) & 2:
            nome = "Indirizzo privato (telefono, tablet o PC)"
        else:
            if self.tabella_oui is None:
                self._carica_produttori()
            grezzo = self.tabella_oui.get(int(mac[:8].replace(":", ""), 16), "")
            nome = pulisci_produttore(grezzo) if grezzo else "Produttore sconosciuto"
        if len(self.produttori) > 500:
            self.produttori.clear()
        self.produttori[mac] = nome
        return nome

    def _comando_ip(self, *argomenti):
        try:
            uscita = subprocess.run([IP_CMD, "-j", *argomenti], capture_output=True, text=True, timeout=10).stdout
            return json.loads(uscita or "[]")
        except (OSError, ValueError, subprocess.SubprocessError):
            return []

    def _leggi_vicini(self):
        self.ultima_lettura_vicini = time.time()
        trovati = []
        if len(self.ip_mac) > 4000:
            self.ip_mac.clear()
        for v in self._comando_ip("neigh", "show"):
            mac = (v.get("lladdr") or "").lower()
            ip = normalizza_ip(v.get("dst") or "")
            stati = set(v.get("state") or [])
            if not MAC_RE.match(mac) or not ip or mac == "00:00:00:00:00:00" or stati & {"FAILED", "INCOMPLETE"}:
                continue
            self.ip_mac[ip] = mac
            trovati.append((ip, mac, bool(stati & STATI_PRESENTI)))
        return trovati

    def _leggi_indirizzi(self):
        miei = {"127.0.0.1", "::1"}
        interfacce = self._comando_ip("addr", "show")
        for interfaccia in interfacce:
            for a in interfaccia.get("addr_info") or []:
                if a.get("local"):
                    miei.add(normalizza_ip(a["local"]))
        self.miei_ip = miei
        rotte = self._comando_ip("route", "show", "default")
        if rotte:
            self.gateway = rotte[0].get("gateway") or ""
            dev = rotte[0].get("dev") or ""
            for interfaccia in interfacce:
                if interfaccia.get("ifname") != dev:
                    continue
                for a in interfaccia.get("addr_info") or []:
                    if a.get("family") == "inet" and a.get("local"):
                        try:
                            self.rete_locale = ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}").network
                        except (ValueError, KeyError):
                            pass
                        break

    def _scansiona(self):
        """Bussa a ogni indirizzo della rete di casa e guarda chi risponde (serve a trovare tutti i dispositivi)."""
        rete = self.rete_locale
        if rete is None or rete.num_addresses > 1024:
            return
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            for host in rete.hosts():
                ip = str(host)
                if ip in self.miei_ip:
                    continue
                try:
                    s.sendto(b"\0", (ip, 9))
                except OSError:
                    pass
        time.sleep(9)
        ora = time.time()
        vicini = self._leggi_vicini()
        with self.lock:
            for ip, mac, presente in vicini:
                if not presente or ":" in ip:
                    continue
                d = self._dispositivo(mac, ip, ora)
                d["ultimo_visto_rete"] = ora

    # ---- che cosa controlla

    def _conta(self, d, ora):
        ora_intera = int(ora // ORA * ORA)
        ore = d.setdefault("ore", {})
        ore[ora_intera] = ore.get(ora_intera, 0) + 1
        self.rete_ore[ora_intera] = self.rete_ore.get(ora_intera, 0) + 1
        d["ultimo_dns"] = ora
        self.sporco = True

    def _controlla_nome(self, d, nome, ora):
        if nome_locale(nome):
            return
        trovato = self.domini.trova(nome)
        if trovato:
            self._avviso("pericolo", d, f"Sito pericoloso: {nome}",
                         f"{self._chi(d)} ha cercato {nome}, che è nell'elenco dei siti pericolosi "
                         "(malware, phishing o truffe). Se non sai cos'è, controlla quel dispositivo con un antivirus.",
                         f"pericolo|{d['chiave']}|{trovato}", GIORNO)
        base = dominio_base(nome)
        if base in TUNNEL_NORMALI or not nome.endswith("." + base):
            return
        sotto = nome[: -len(base) - 1]
        if len(sotto) >= 50 or any(len(e) >= 40 for e in sotto.split(".")):
            finestra = self.lunghi[(d["chiave"], base)]
            finestra.append(ora)
            while finestra and ora - finestra[0] > FINESTRA:
                finestra.popleft()
            if len(finestra) >= SOGLIA_TUNNEL:
                self._avviso("strano", d, f"Possibile tunnel DNS verso {base}",
                             f"{self._chi(d)} ha mandato {len(finestra)} richieste con nomi lunghissimi a {base} "
                             "in 10 minuti: alcuni malware usano questo trucco per far uscire dati di nascosto. "
                             "Se è un programma che conosci, puoi ignorare l'avviso.",
                             f"tunnel|{d['chiave']}|{base}", 6 * ORA)

    def _registra_nx(self, d, nome, ora):
        if nome_locale(nome):
            return
        base = dominio_base(nome)
        if not sembra_casuale(base.split(".")[0]):
            return
        finestra = self.nx[d["chiave"]]
        finestra.append((ora, nome))
        while finestra and ora - finestra[0][0] > FINESTRA:
            finestra.popleft()
        distinti = {n for _, n in finestra}
        if len(distinti) >= SOGLIA_CASUALI:
            self._avviso("strano", d, "Tanti nomi a caso che non esistono",
                         f"{self._chi(d)} ha cercato {len(distinti)} nomi inesistenti dall'aspetto casuale in 10 minuti "
                         f"(per esempio {nome}). Alcuni malware lo fanno per trovare il loro server: "
                         "controlla quel dispositivo con un antivirus.",
                         f"casuali|{d['chiave']}", 6 * ORA)

    def _controlla_ip(self, d, nome, risposta, ora):
        if not IPV4_RE.match(risposta) or not self.ip_pericolosi.contiene(risposta):
            return
        self._avviso("pericolo", d, f"Server pericoloso: {risposta}",
                     f"{self._chi(d)} ha chiesto {nome}, che porta a {risposta}: un indirizzo nell'elenco dei server "
                     "pericolosi. Può essere un falso allarme se il sito sta su un server condiviso.",
                     f"ip|{d['chiave']}|{risposta}", GIORNO)

    def _pulisci(self, ora):
        with self.lock:
            for chiave in [k for k, f in self.nx.items() if not f or ora - f[-1][0] > FINESTRA]:
                del self.nx[chiave]
            for chiave in [k for k, f in self.lunghi.items() if not f or ora - f[-1] > FINESTRA]:
                del self.lunghi[chiave]

    # ---- avvisi

    def _avviso(self, tipo, d, titolo, dettaglio, chiave, ripeti):
        if chiave in self.ignorati:
            return
        ora = time.time()
        ultimo = self.ultimi.get(chiave)
        if ultimo and (ripeti is None or ora - ultimo < ripeti):
            return
        self.ultimi[chiave] = ora
        avviso = {"id": secrets.token_hex(6), "ts": ora, "tipo": tipo, "dispositivo": d.get("chiave", "") if d else "",
                  "titolo": titolo, "dettaglio": dettaglio, "chiave": chiave, "letto": False}
        self.avvisi.insert(0, avviso)
        del self.avvisi[MAX_AVVISI:]
        self.sporco = True
        print(f"Rete: avviso {tipo}: {titolo}", flush=True)
        self._accoda(f"{ICONE.get(tipo, '')} {titolo}\n{dettaglio}")

    def _controlla_dns(self):
        try:
            chiedi_dns(DNS_PROVA, "example.com")
            funziona = True
        except OSError:
            funziona = False
        with self.lock:
            stato = self.dns
            if funziona:
                if stato["avvisato"]:
                    self._avviso("sistema", self.dispositivi.get("pi"), "Il DNS del Pi funziona di nuovo",
                                 "In casa si naviga di nuovo normalmente.", f"dns-su|{int(time.time())}", None)
                stato.update(ok=True, fallimenti=0, avvisato=False)
            else:
                stato["ok"] = False
                stato["fallimenti"] += 1
                if stato["fallimenti"] >= 3 and not stato["avvisato"]:
                    stato["avvisato"] = True
                    self._avviso("sistema", self.dispositivi.get("pi"), "Il DNS del Pi non risponde",
                                 "In casa internet potrebbe non funzionare. Riavvia il Pi; se non basta, "
                                 "togli il Pi dai server DNS dell'Iliadbox.", "dns-giu", 6 * ORA)

    # ---- elenchi dei siti pericolosi

    def _scarica(self, url, path):
        richiesta = urllib.request.Request(url, headers={"User-Agent": "Videoteca-rete"})
        with urllib.request.urlopen(richiesta, timeout=90) as risposta:
            dati = risposta.read(MAX_LISTA + 1)
        if len(dati) > MAX_LISTA:
            raise ValueError("file troppo grande")
        righe = sum(1 for r in dati.splitlines() if r.strip() and not r.startswith(b"#"))
        if righe < 100:
            raise ValueError("il file scaricato è quasi vuoto")
        scrivi_atomico(path, dati)

    def _aggiorna_liste(self, forza=False):
        """Scarica gli elenchi se hanno più di un giorno. Restituisce fra quanti secondi riprovare."""
        self.cartella_liste.mkdir(parents=True, exist_ok=True)
        errori = []
        cambiati = False
        for url, nome in ((URL_DOMINI, "domini.txt"), (URL_IP, "ip.txt")):
            path = self.cartella_liste / nome
            fresco = path.exists() and time.time() - path.stat().st_mtime < 23 * ORA
            if fresco and not forza:
                continue
            try:
                self._scarica(url, path)
                cambiati = True
            except (OSError, ValueError, urllib.error.URLError) as exc:
                errori.append(f"{nome}: {exc}")
        if cambiati or not len(self.domini):
            self._carica_liste()
        with self.lock:
            self.liste_info["errore"] = ("Non riesco ad aggiornare l'elenco dei siti pericolosi: " + "; ".join(errori)) if errori else ""
            self.sporco = True
        return ORA if errori else GIORNO

    def _carica_liste(self):
        domini, ip = self.domini, self.ip_pericolosi
        path_domini, path_ip = self.cartella_liste / "domini.txt", self.cartella_liste / "ip.txt"
        try:
            if path_domini.exists():
                domini = ElencoDomini.da_file(path_domini)
            if path_ip.exists():
                ip = ElencoIP.da_file(path_ip)
        except OSError as exc:
            print(f"Rete: non riesco a leggere gli elenchi: {exc}", flush=True)
            return
        with self.lock:
            self.domini, self.ip_pericolosi = domini, ip
            self.liste_info.update(domini=len(domini), ip=len(ip),
                                   aggiornate=path_domini.stat().st_mtime if path_domini.exists() else 0)

    # ---- Telegram

    def _tg_collegato(self):
        c = self._c()
        return bool(c.get("telegram_token") and c.get("telegram_chat"))

    def _tg(self, metodo, dati=None, token=None, timeout=20):
        token = token or self._c().get("telegram_token")
        if not token:
            raise TelegramErrore("Telegram non è collegato.")
        url = f"{TELEGRAM_API}/bot{token}/{metodo}"
        corpo = json.dumps(dati or {}).encode("utf-8")
        richiesta = urllib.request.Request(url, data=corpo, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(richiesta, timeout=timeout) as r:
                risposta = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                risposta = json.loads(exc.read() or b"{}")
            except ValueError:
                risposta = {}
            risposta.setdefault("ok", False)
            risposta.setdefault("error_code", exc.code)
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.gaierror) and url.startswith("https://"):
                try:
                    _stato, testo = _post_senza_dns_di_casa(url, corpo, timeout)
                    risposta = json.loads(testo or b"{}")
                except (OSError, ValueError) as altro:
                    raise TelegramErrore(f"non riesco a raggiungere Telegram ({altro})") from altro
            else:
                raise TelegramErrore(f"non riesco a raggiungere Telegram ({exc.reason})") from exc
        except (OSError, ValueError) as exc:
            raise TelegramErrore(f"non riesco a raggiungere Telegram ({exc})") from exc
        if not risposta.get("ok"):
            parametri = risposta.get("parameters") or {}
            raise TelegramErrore(risposta.get("description") or "Telegram ha rifiutato la richiesta",
                                 risposta.get("error_code") or 0, parametri.get("retry_after") or 0)
        return risposta.get("result")

    def _accoda(self, testo):
        if self._tg_collegato():
            self.coda.put(testo)

    def _postino(self):
        while True:
            testo = self.coda.get()
            ora = time.time()
            while self.inviati and ora - self.inviati[0] > ORA:
                self.inviati.popleft()
            if len(self.inviati) >= MAX_TELEGRAM_ORA:
                self.saltati += 1
                continue
            for tentativo in range(8):
                chat = self._c().get("telegram_chat")
                if not chat:
                    break
                try:
                    self._tg("sendMessage", {"chat_id": chat, "text": testo, "disable_web_page_preview": True})
                    self.inviati.append(time.time())
                    self.telegram_errore = ""
                    break
                except TelegramErrore as exc:
                    if exc.codice == 429:
                        time.sleep(min(300, exc.attesa or 30))
                    elif exc.codice in (400, 401, 403, 404):
                        self.telegram_errore = ("Telegram non accetta i messaggi: forse hai bloccato il bot o cancellato la chat. "
                                                f"Ricollegalo dalla pagina Rete. ({exc})")
                        break
                    else:
                        self.telegram_errore = f"Ultimo invio non riuscito: {exc}. Riprovo da solo."
                        time.sleep(min(900, 20 * 2 ** tentativo))

    def _riassunto_telegram(self):
        if not self.saltati:
            return
        ora = time.time()
        while self.inviati and ora - self.inviati[0] > ORA:
            self.inviati.popleft()
        if len(self.inviati) < MAX_TELEGRAM_ORA:
            n, self.saltati = self.saltati, 0
            self._accoda(f"Altri {n} avvisi nell'ultima ora: per non riempirti il telefono non te li ho mandati. "
                         "Li trovi nella pagina Rete della Videoteca.")

    def _vista_telegram(self):
        c = self._c()
        return {"collegato": self._tg_collegato(), "token": bool(c.get("telegram_token")),
                "bot": c.get("telegram_bot") or "", "nome": c.get("telegram_nome") or "",
                "errore": self.telegram_errore}

    def telegram_imposta(self, token):
        token = (token or "").strip()
        if not TOKEN_RE.match(token):
            raise ReteErrore("Questo non sembra il token di un bot: copialo intero dal messaggio di @BotFather "
                             "(numeri, due punti e poi lettere e numeri).")
        try:
            info = self._tg("getMe", token=token)
        except TelegramErrore as exc:
            if exc.codice in (401, 404):
                raise ReteErrore("Telegram dice che il token non è valido: ricopialo da @BotFather.") from exc
            raise ReteErrore(f"Non riesco a parlare con Telegram: {exc}.", 502) from exc
        c = self._c()
        c.update(telegram_token=token, telegram_bot=(info or {}).get("username") or "",
                 telegram_chat=None, telegram_nome="")
        self.salva_cfg()
        self.telegram_attesa = time.time()
        self.telegram_errore = ""
        return self._vista_telegram()

    def telegram_collega(self):
        c = self._c()
        if not c.get("telegram_token"):
            raise ReteErrore("Prima incolla il token del bot.")
        try:
            aggiornamenti = self._tg("getUpdates", {"timeout": 0, "allowed_updates": ["message"]}) or []
        except TelegramErrore as exc:
            raise ReteErrore(f"Non riesco a parlare con Telegram: {exc}.", 502) from exc
        soglia = (self.telegram_attesa or time.time()) - 600
        chat, nome, ultimo_id = None, "", None
        for u in aggiornamenti:
            ultimo_id = u.get("update_id", ultimo_id)
            messaggio = u.get("message") or {}
            dati_chat = messaggio.get("chat") or {}
            if dati_chat.get("type") == "private" and messaggio.get("date", 0) >= soglia:
                chat = dati_chat.get("id")
                nome = (messaggio.get("from") or {}).get("first_name") or ""
        if ultimo_id is not None:
            try:
                self._tg("getUpdates", {"offset": ultimo_id + 1, "timeout": 0})
            except TelegramErrore:
                pass
        if chat is None:
            return self._vista_telegram()
        c.update(telegram_chat=chat, telegram_nome=nome)
        self.salva_cfg()
        self.telegram_errore = ""
        saluto = f"Ciao {nome}!" if nome else "Ciao!"
        try:
            self._tg("sendMessage", {"chat_id": chat, "text": f"{saluto} Sono il controllo della rete di casa: "
                                                             "ti scrivo qui se trovo qualcosa di strano."})
        except TelegramErrore as exc:
            self.telegram_errore = f"Collegato, ma il primo messaggio non è partito: {exc}."
        return self._vista_telegram()

    def telegram_prova(self):
        c = self._c()
        if not self._tg_collegato():
            raise ReteErrore("Collega prima Telegram.")
        try:
            self._tg("sendMessage", {"chat_id": c["telegram_chat"],
                                     "text": "Prova riuscita: gli avvisi della rete di casa arrivano qui."})
        except TelegramErrore as exc:
            raise ReteErrore(f"Il messaggio di prova non è partito: {exc}.", 502) from exc
        self.telegram_errore = ""
        return self._vista_telegram()

    def telegram_scollega(self):
        c = self._c()
        for chiave in ("telegram_token", "telegram_bot", "telegram_chat", "telegram_nome"):
            c.pop(chiave, None)
        self.salva_cfg()
        self.telegram_errore = ""
        return self._vista_telegram()

    # ---- per la pagina Rete

    def conta(self):
        with self.lock:
            return {"attiva": self.attiva, "configurata": self.configurata(),
                    "da_leggere": sum(1 for a in self.avvisi if not a.get("letto"))}

    def vista(self):
        ora = time.time()
        ora_intera = int(ora // ORA * ORA)
        ore = [ora_intera - (23 - i) * ORA for i in range(24)]
        with self.lock:
            dispositivi = []
            for d in self.dispositivi.values():
                attivita = [d.get("ore", {}).get(h, 0) for h in ore]
                senza_dns = (self._tipo(d) == "dispositivo" and self.da and ora - self.da > GIORNO
                             and ora - d.get("ultimo_visto_rete", 0) < GIORNO and ora - d.get("ultimo_dns", 0) > GIORNO)
                dispositivi.append({
                    "chiave": d["chiave"], "nome": d.get("nome", ""), "mostrato": self._nome(d),
                    "produttore": d.get("produttore", ""), "ip": d.get("ip", ""), "ip6": d.get("ip6", ""),
                    "mac": d.get("mac", ""), "tipo": self._tipo(d), "primo": d.get("primo", 0),
                    "ultimo": d.get("ultimo", 0), "attivita": attivita, "totale": sum(attivita),
                    "nuovo": ora - d.get("primo", 0) < GIORNO and bool(self.da) and d.get("primo", 0) >= self.da + APPRENDIMENTO,
                    "senza_dns": bool(senza_dns),
                })
            dispositivi.sort(key=lambda x: (-(x["ultimo"] > ora - 900), -x["totale"], x["mostrato"].lower()))
            nomi = {d["chiave"]: self._nome(d) for d in self.dispositivi.values()}
            avvisi = [dict(a, nome_dispositivo=nomi.get(a.get("dispositivo"), "")) for a in self.avvisi[:100]]
            for a in avvisi:
                a.pop("chiave", None)
            return {
                "attiva": self.attiva, "configurata": self.configurata(), "da": self.da,
                "apprendimento_fino": (self.da + APPRENDIMENTO) if self.da and ora < self.da + APPRENDIMENTO else 0,
                "registro": dict(self.registro), "dns": {"ok": self.dns["ok"]},
                "liste": dict(self.liste_info), "telegram": self._vista_telegram(),
                "ore": ore, "attivita": [self.rete_ore.get(h, 0) for h in ore],
                "dispositivi": dispositivi[:200], "avvisi": avvisi,
                "da_leggere": sum(1 for a in self.avvisi if not a.get("letto")),
            }

    def rinomina(self, chiave, nome):
        nome = " ".join(str(nome or "").split())[:60]
        with self.lock:
            d = self.dispositivi.get(str(chiave or ""))
            if not d:
                raise ReteErrore("Non trovo questo dispositivo.", 404)
            d["nome"] = nome
        self._salva()
        return {"ok": True}

    def segna_letti(self):
        with self.lock:
            for a in self.avvisi:
                a["letto"] = True
        self._salva()
        return {"ok": True}

    def ignora(self, ident):
        with self.lock:
            for a in self.avvisi:
                if a.get("id") == ident:
                    if a.get("chiave"):
                        self.ignorati.add(a["chiave"])
                    a["letto"] = True
                    break
            else:
                raise ReteErrore("Non trovo questo avviso.", 404)
        self._salva()
        return {"ok": True}

    def aggiorna_liste_adesso(self):
        if not self.attiva:
            raise ReteErrore("Il controllo della rete è spento.")
        threading.Thread(target=self._aggiorna_liste, kwargs={"forza": True}, daemon=True).start()
        return {"ok": True}
