#!/bin/bash
# Accende il controllo della rete: il Pi diventa il DNS di casa (con dnsmasq) e la
# Videoteca controlla quali siti contatta ogni dispositivo, avvisandoti su Telegram.
#
#   bash project-nas-home/rete.sh            accende il controllo (si può rilanciare)
#   bash project-nas-home/rete.sh --spegni   spegne tutto
#
# Il Pi vede solo i nomi dei siti che i dispositivi chiedono, non il contenuto.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF=/etc/dnsmasq.d/videoteca.conf
OVERRIDE=/etc/systemd/system/dnsmasq.service.d/videoteca.conf
APP_DIR="${VIDEOTECA_DIR:-/opt/videoteca}"
SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

json() {
  python3 -c "import json, sys
dati = json.load(sys.stdin)
$1" 2>/dev/null || true
}

prova_dns() {
  python3 - <<'PY'
import random, socket, struct, sys
ident = random.randint(0, 65535)
domanda = struct.pack(">HHHHHH", ident, 0x0100, 1, 0, 0, 0) + b"\x07example\x03com\x00" + struct.pack(">HH", 1, 1)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(5)
try:
    s.sendto(domanda, ("127.0.0.1", 53))
    dati, _ = s.recvfrom(4096)
except OSError:
    sys.exit(1)
sys.exit(0 if dati[:2] == struct.pack(">H", ident) and dati[3] & 15 == 0 else 1)
PY
}

accendi() {
  local occupata
  occupata="$($SUDO ss -H -lunp 'sport = :53' 2>/dev/null | grep -v '"dnsmasq"' || true)"
  if [ -n "$occupata" ]; then
    echo "La porta del DNS (53) è già usata da un altro programma:"
    echo "$occupata"
    echo "Scrivi a Claude cosa vedi qui sopra."
    return 1
  fi

  if [ ! -f "$APP_DIR/rete.py" ]; then
    echo "Prima aggiorno la Videoteca, che serve per il controllo della rete..."
    bash "$REPO/aggiorna.sh"
  fi

  echo "Installo dnsmasq, il programma che fa da DNS per la casa (sul Pi Zero ci vuole qualche minuto)..."
  $SUDO apt-get update
  $SUDO apt-get install -y dnsmasq
  $SUDO apt-get install -y ieee-data || echo "(Senza l'elenco dei produttori: i dispositivi avranno nomi meno chiari.)"

  $SUDO mkdir -p /etc/dnsmasq.d "$(dirname "$OVERRIDE")"
  $SUDO tee "$CONF" >/dev/null <<'EOF'
# Videoteca: il Pi fa da DNS per la rete di casa e registra quali nomi chiede ogni
# dispositivo, così la Videoteca può avvisarti se qualcuno contatta siti pericolosi.
# Le domande vanno a Quad9, che blocca già da solo i siti di malware più noti.
# Per spegnere tutto:  bash project-nas-home/rete.sh --spegni
no-resolv
server=9.9.9.9
server=149.112.112.112
domain-needed
bogus-priv
cache-size=2000
log-queries=extra
log-async=50
EOF
  $SUDO tee "$OVERRIDE" >/dev/null <<'EOF'
# Videoteca: se dnsmasq si ferma, ripartire subito (senza DNS in casa non si naviga).
[Unit]
StartLimitIntervalSec=0

[Service]
Restart=always
RestartSec=2
EOF
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable dnsmasq >/dev/null 2>&1
  $SUDO systemctl restart dnsmasq
  sleep 2

  if ! prova_dns; then
    echo
    echo "Il DNS del Pi non risponde (o non riesce a raggiungere internet). Ultimi messaggi:"
    $SUDO journalctl -u dnsmasq -n 20 --no-pager || true
    echo "Scrivi a Claude cosa vedi qui sopra. Per ora non cambiare niente sull'Iliadbox."
    return 1
  fi
  sleep 3
  if ! $SUDO journalctl -u dnsmasq --since "-2 min" --no-pager -o cat 2>/dev/null | grep -q 'query\[A\] example\.com'; then
    echo
    echo "Attenzione: il DNS risponde ma non registra le richieste, quindi la Videoteca non le vede."
    echo "Scrivi a Claude il risultato di:  sudo journalctl -u dnsmasq -n 20"
  fi

  local rotta iface gw ip4 ip6 mac porta
  rotta="$(ip -j route show default 2>/dev/null || echo '[]')"
  iface="$(echo "$rotta" | json 'print(dati[0].get("dev", "") if dati else "")')"
  gw="$(echo "$rotta" | json 'print(dati[0].get("gateway", "") if dati else "")')"
  ip4="$(ip -j -4 addr show dev "$iface" 2>/dev/null | json 'print(next((a["local"] for i in dati for a in i.get("addr_info", []) if a.get("family") == "inet"), ""))')"
  ip6="$(ip -j -6 addr show dev "$iface" scope global 2>/dev/null | json '
buoni = [a["local"] for i in dati for a in i.get("addr_info", []) if not a.get("temporary") and not a.get("deprecated")]
ula = [x for x in buoni if x.lower().startswith(("fc", "fd"))]
print((ula or buoni or [""])[0])')"
  mac="$(cat "/sys/class/net/$iface/address" 2>/dev/null || true)"
  porta="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("port", 8080))' "$APP_DIR/config.json" 2>/dev/null || echo 8080)"

  local testo_ip="$ip4" testo_mac="$mac" testo_router="$gw"
  [ -n "$testo_ip" ] || testo_ip="l'indirizzo del Pi"
  [ -n "$testo_mac" ] || testo_mac="quello del Pi"
  [ -n "$testo_router" ] || testo_router="192.168.1.254"

  echo
  echo "Fatto: il DNS del Pi è acceso e la Videoteca controlla le richieste."
  echo
  echo "Ultimo passo, sull'Iliadbox (dal browser, in casa: http://$testo_router):"
  echo "  1. Nelle impostazioni del DHCP dai al Pi un indirizzo fisso:"
  echo "     MAC $testo_mac  ->  IP $testo_ip"
  echo "  2. Sempre nel DHCP, come server DNS metti solo $testo_ip (togli gli altri)."
  if [ -n "$ip6" ]; then
    echo "  3. Nelle impostazioni IPv6, come server DNS metti ${ip6}"
    echo "  4. Spegni e riaccendi il Wi-Fi dei dispositivi (o riavviali): da lì passano dal Pi."
  else
    echo "  3. Spegni e riaccendi il Wi-Fi dei dispositivi (o riavviali): da lì passano dal Pi."
  fi
  echo
  echo "Poi apri la Videoteca nella scheda Rete e collega Telegram: http://${ip4:-IP-del-Pi}:$porta/#rete"
  echo
  echo "Importante: se il Pi si spegne, in casa non si naviga finché non torna acceso."
  echo "In caso di emergenza togli il Pi dai server DNS dell'Iliadbox."
}

spegni() {
  echo "Prima di spegnere, togli il Pi dai server DNS dell'Iliadbox (rimetti quelli di prima),"
  echo "altrimenti in casa non si naviga più."
  local risposta=""
  read -r -p "L'hai già fatto? Scrivi si e premi Invio: " risposta || true
  case "$risposta" in
    s|si|sì|S|SI|Si|SÌ|Sì) ;;
    *) echo "Va bene, non cambio niente."; return 1 ;;
  esac
  $SUDO rm -f "$CONF" "$OVERRIDE"
  $SUDO systemctl daemon-reload
  $SUDO systemctl disable --now dnsmasq >/dev/null 2>&1 || true
  echo "Controllo della rete spento: dnsmasq è fermo. La Videoteca dei film funziona come prima."
}

main() {
  case "${1:-}" in
    "")        accendi ;;
    --spegni)  spegni ;;
    *) echo "Uso: bash $0 [--spegni]"; return 2 ;;
  esac
}

main "$@"; exit $?
