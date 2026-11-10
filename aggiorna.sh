#!/bin/bash
# Aggiorna la Videoteca sul Pi con l'ultima versione presente su GitHub.
#
#   bash project-nas-home/aggiorna.sh               scarica l'ultima versione e la installa
#   bash project-nas-home/aggiorna.sh --automatico  da ora il Pi controlla GitHub da solo ogni 15 minuti
#   bash project-nas-home/aggiorna.sh --manuale     spegne il controllo automatico
#   bash project-nas-home/aggiorna.sh --stato       mostra la versione e se l'automatico è acceso
#
# La chiave TMDB, la password e i film restano dove sono: qui dentro non ci sono.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWNER="$(stat -c %U "$REPO")"
MARKER="$REPO/.git/videoteca-installata"
UNIT=videoteca-aggiorna
SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

# I comandi git girano sempre come il proprietario della cartella, anche quando
# lo script parte da systemd come root: così i file restano suoi.
come_proprietario() {
  if [ "$(id -u)" -eq 0 ] && [ "$OWNER" != "root" ]; then
    runuser -u "$OWNER" -- "$@"
  else
    "$@"
  fi
}

descrivi() {
  come_proprietario git -C "$REPO" log -1 --date=format:'%d/%m/%Y %H:%M' --format="$1" "${2:-HEAD}"
}

aggiorna() {
  local solo_se_cambiato="$1"
  if [ "$OWNER" = "root" ] && [ -z "${VIDEOTECA_USER:-}" ]; then
    echo "La cartella $REPO è dell'utente root: cancellala con  sudo rm -rf $REPO"
    echo "e scaricala di nuovo senza sudo:  git clone -b videoteca https://github.com/Stefold08/project-nas-home"
    return 1
  fi

  if ! come_proprietario git -C "$REPO" pull --ff-only --quiet; then
    echo "Non riesco a scaricare l'ultima versione da GitHub: controlla che il Pi sia collegato a internet."
    [ "$solo_se_cambiato" = "1" ] && return 1
    echo "Intanto installo la versione che c'è già sul Pi."
  fi

  local head installata
  head="$(come_proprietario git -C "$REPO" rev-parse HEAD)"
  installata="$(cat "$MARKER" 2>/dev/null || true)"
  if [ "$solo_se_cambiato" = "1" ] && [ "$head" = "$installata" ]; then
    return 0
  fi
  # In automatico, se è cambiato solo altro (README, script, prove) la Videoteca
  # installata è già giusta: non la reinstallo, così non si riavvia per niente.
  if [ "$solo_se_cambiato" = "1" ] && [ -n "$installata" ] \
     && come_proprietario git -C "$REPO" diff --quiet "$installata" HEAD -- installa-videoteca.sh 2>/dev/null; then
    printf '%s\n' "$head" | come_proprietario tee "$MARKER" >/dev/null
    return 0
  fi

  echo "Installo la versione $(descrivi '%h del %cd: %s')"
  if [ "$(id -u)" -eq 0 ]; then
    VIDEOTECA_USER="${VIDEOTECA_USER:-$OWNER}" bash "$REPO/installa-videoteca.sh"
  else
    bash "$REPO/installa-videoteca.sh"
  fi
  printf '%s\n' "$head" | come_proprietario tee "$MARKER" >/dev/null
}

attiva_automatico() {
  if [ "$OWNER" = "root" ]; then
    echo "Prima scarica il repository senza sudo (vedi il README), poi riprova."
    return 1
  fi
  $SUDO tee "/etc/systemd/system/$UNIT.service" >/dev/null <<EOF
[Unit]
Description=Videoteca: scarica e installa gli aggiornamenti da GitHub
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=VIDEOTECA_USER=$OWNER
ExecStart=/bin/bash "$REPO/aggiorna.sh" --se-cambiato
EOF
  $SUDO tee "/etc/systemd/system/$UNIT.timer" >/dev/null <<EOF
[Unit]
Description=Videoteca: controlla gli aggiornamenti su GitHub ogni 15 minuti

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
RandomizedDelaySec=1min

[Install]
WantedBy=timers.target
EOF
  $SUDO systemctl daemon-reload
  $SUDO systemctl --quiet enable --now "$UNIT.timer"
  echo "Fatto: da ora il Pi controlla GitHub ogni 15 minuti e installa da solo le versioni nuove."
  echo "Per spegnerlo:  bash $REPO/aggiorna.sh --manuale"
}

spegni_automatico() {
  $SUDO systemctl --quiet disable --now "$UNIT.timer" 2>/dev/null || true
  echo "Aggiornamento automatico spento. Per aggiornare a mano:  bash $REPO/aggiorna.sh"
}

stato() {
  local installata
  installata="$(cat "$MARKER" 2>/dev/null || true)"
  echo "Scaricata da GitHub:  $(descrivi '%h del %cd: %s')"
  if [ -n "$installata" ]; then
    echo "Installata:           $(descrivi '%h del %cd' "$installata" 2>/dev/null || echo "$installata")"
  else
    echo "Installata:           nessuna versione installata da qui"
  fi
  if systemctl is-enabled --quiet "$UNIT.timer" 2>/dev/null; then
    echo "Aggiornamento automatico: acceso"
    journalctl -u "$UNIT.service" -n 6 --no-pager -o cat 2>/dev/null | sed 's/^/  /' || true
  else
    echo "Aggiornamento automatico: spento"
  fi
}

main() {
  case "${1:-}" in
    "")             aggiorna 0 ;;
    --se-cambiato)  aggiorna 1 ;;
    --automatico)   attiva_automatico ;;
    --manuale)      spegni_automatico ;;
    --stato)        stato ;;
    *) echo "Uso: bash $0 [--automatico | --manuale | --stato]"; return 2 ;;
  esac
}

# Tutto sta dentro main: se git pull aggiorna questo file mentre gira, bash non se ne accorge.
main "$@"; exit $?
