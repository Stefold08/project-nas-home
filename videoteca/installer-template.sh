#!/bin/bash
# Installa la Videoteca, la dashboard web dei film, sul Raspberry Pi.
# Uso:  bash installa-videoteca.sh     (senza sudo: la password la chiede lo script)
# Puoi rilanciarlo per aggiornare: la chiave TMDB e le impostazioni restano.
set -euo pipefail

APP_DIR="${VIDEOTECA_DIR:-/opt/videoteca}"
LIBRARY="${VIDEOTECA_LIBRARY:-/mnt/film}"
PORT="${VIDEOTECA_PORT:-8080}"

if [ "$(id -u)" -eq 0 ]; then
  RUN_USER="${VIDEOTECA_USER:-${SUDO_USER:-}}"
  SUDO=""
  if [ -z "$RUN_USER" ] || [ "$RUN_USER" = "root" ]; then
    echo "Lancia lo script come utente normale, senza sudo:  bash $0"
    exit 1
  fi
else
  RUN_USER="$(id -un)"
  SUDO="sudo"
fi

echo "Installo la Videoteca in $APP_DIR (utente $RUN_USER, film in $LIBRARY)..."

if ! command -v python3 >/dev/null 2>&1; then
  echo "Installo Python 3..."
  $SUDO apt-get install -y python3
fi

$SUDO mkdir -p "$APP_DIR" "$LIBRARY"
$SUDO chown "$RUN_USER:$RUN_USER" "$APP_DIR" "$LIBRARY"

$SUDO tee "$APP_DIR/videoteca.py" >/dev/null <<'VIDEOTECA_PY_FINE'
@@VIDEOTECA_PY@@
VIDEOTECA_PY_FINE

$SUDO tee "$APP_DIR/index.html" >/dev/null <<'VIDEOTECA_HTML_FINE'
@@INDEX_HTML@@
VIDEOTECA_HTML_FINE

$SUDO tee "$APP_DIR/rete.py" >/dev/null <<'VIDEOTECA_RETE_FINE'
@@RETE_PY@@
VIDEOTECA_RETE_FINE

if [ ! -f "$APP_DIR/config.json" ]; then
  $SUDO tee "$APP_DIR/config.json" >/dev/null <<EOF
{
  "library": "$LIBRARY",
  "port": $PORT,
  "tmdb_key": "",
  "language": "it-IT",
  "reserve_gb": 2
}
EOF
fi

$SUDO chown -R "$RUN_USER:$RUN_USER" "$APP_DIR"
$SUDO chmod 600 "$APP_DIR/config.json"

if [ "${VIDEOTECA_NO_SYSTEMD:-0}" = "1" ]; then
  echo "File installati. Servizio non configurato (VIDEOTECA_NO_SYSTEMD=1)."
  exit 0
fi

# Il controllo della rete legge il registro di dnsmasq: serve il permesso di leggere il journal.
GRUPPI=""
if getent group systemd-journal >/dev/null 2>&1; then
  GRUPPI="SupplementaryGroups=systemd-journal"
fi

$SUDO tee /etc/systemd/system/videoteca.service >/dev/null <<EOF
[Unit]
Description=Videoteca, la dashboard dei film
After=network-online.target local-fs.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
$GRUPPI
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 $APP_DIR/videoteca.py
Restart=on-failure
RestartSec=5
UMask=0022
Nice=5

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable videoteca >/dev/null 2>&1
$SUDO systemctl restart videoteca

printf "Avvio la Videoteca"
ok=0
for _ in $(seq 1 30); do
  if curl -s -o /dev/null "http://127.0.0.1:$PORT/api/settings"; then ok=1; break; fi
  printf "."
  sleep 1
done
echo

if [ "$ok" = "1" ]; then
  IP="$(hostname -I | tr ' ' '\n' | grep -E '^[0-9]+\.' | grep -v '^100\.' | head -n 1 || true)"
  TS_IP=""
  if command -v tailscale >/dev/null 2>&1; then
    TS_IP="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
  fi
  echo "Fatto! Apri la Videoteca dal browser del Mac o del telefono:"
  echo "  in casa:     http://${IP:-$(hostname).local}:$PORT"
  if [ -n "$TS_IP" ]; then
    echo "  fuori casa:  http://$TS_IP:$PORT   (con Tailscale acceso)"
  fi
else
  echo "La Videoteca non risponde. Guarda cosa è successo con:  journalctl -u videoteca -n 30"
  exit 1
fi
