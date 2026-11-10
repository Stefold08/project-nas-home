#!/bin/bash
# Controlla la Videoteca e la rete del Pi e stampa un riepilogo da mandare a Claude.
#
#   bash project-nas-home/diagnosi.sh
#
# Non cambia niente: legge e basta.
set -uo pipefail

APP_DIR="${VIDEOTECA_DIR:-/opt/videoteca}"

leggi_config() {
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], sys.argv[3]))' \
    "$APP_DIR/config.json" "$1" "$2" 2>/dev/null || echo "$2"
}

rientra() { sed 's/^/  /'; }

main() {
  local porta libreria codice ascolto
  porta="$(leggi_config port 8080)"
  libreria="$(leggi_config library /mnt/film)"

  echo "== Videoteca"
  echo "servizio:   $(systemctl is-active videoteca 2>/dev/null || true), all'accensione: $(systemctl is-enabled videoteca 2>/dev/null || true)"
  codice="$(curl -s -m 8 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$porta/" 2>/dev/null || true)"
  if [ -n "$codice" ] && [ "$codice" != "000" ]; then
    echo "sul Pi:     risponde su http://127.0.0.1:$porta (codice $codice)"
  else
    echo "sul Pi:     NON risponde su http://127.0.0.1:$porta"
  fi
  ascolto="$(ss -ltnH "sport = :$porta" 2>/dev/null | awk '{print $4}' | paste -sd ' ' - || true)"
  echo "in ascolto: ${ascolto:-nessuno}"

  echo "== Rete"
  echo "indirizzi:  $(hostname -I 2>/dev/null || true)"
  ip -br addr 2>/dev/null | grep -v '^lo ' | rientra
  if curl -s -m 8 -o /dev/null https://github.com 2>/dev/null; then
    echo "internet:   ok"
  else
    echo "internet:   NON raggiungibile"
  fi

  echo "== Tailscale"
  if command -v tailscale >/dev/null 2>&1; then
    echo "servizio:   $(systemctl is-active tailscaled 2>/dev/null || true)"
    echo "indirizzo:  $(tailscale ip -4 2>/dev/null | head -n 1 || true)"
    tailscale status --peers=false 2>&1 | head -n 3 | rientra
  else
    echo "non installato"
  fi

  echo "== Ultimi messaggi della Videoteca"
  journalctl -u videoteca -n 12 --no-pager -o short 2>&1 | rientra

  echo "== Ultimi aggiornamenti automatici"
  journalctl -u videoteca-aggiorna -n 30 --no-pager -o short 2>&1 | grep -v pam_unix | tail -n 6 | rientra

  echo "== Sistema"
  echo "acceso da:  $(uptime -p 2>/dev/null || true)"
  free -m 2>/dev/null | awk 'NR==2 {print "memoria:    " $3 " MB usati su " $2}'
  df -h / "$libreria" 2>&1 | rientra
  echo "avvisi del kernel:"
  journalctl -k -b -p warning -n 6 --no-pager -o short 2>&1 | rientra
}

main "$@"; exit $?
