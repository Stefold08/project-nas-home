#!/bin/bash
# Installa Tailscale sul Pi e lo collega al tuo account, così puoi aprire la
# Videoteca (e collegarti col Terminale) anche fuori casa o da un'altra rete.
#
#   bash project-nas-home/tailscale.sh
#
# Usa i comandi della guida ufficiale: https://tailscale.com/docs/install/rpi/rpi-trixie
set -euo pipefail

main() {
  local sudo="" utente
  [ "$(id -u)" -ne 0 ] && sudo="sudo"
  utente="${SUDO_USER:-$(id -un)}"

  if ! command -v tailscale >/dev/null 2>&1; then
    local id_os="" codename=""
    if [ -r /etc/os-release ]; then
      id_os="$(. /etc/os-release && echo "${ID:-}")"
      codename="$(. /etc/os-release && echo "${VERSION_CODENAME:-}")"
    fi
    local dist="raspbian"
    [ "$id_os" = "debian" ] && dist="debian"
    codename="${codename:-trixie}"

    if ! command -v curl >/dev/null 2>&1; then
      $sudo apt-get update
      $sudo apt-get install -y curl
    fi
    echo "Installo Tailscale ($dist $codename): sul Pi Zero ci vogliono alcuni minuti..."
    curl -fsSL "https://pkgs.tailscale.com/stable/$dist/$codename.noarmor.gpg" \
      | $sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
    curl -fsSL "https://pkgs.tailscale.com/stable/$dist/$codename.tailscale-keyring.list" \
      | $sudo tee /etc/apt/sources.list.d/tailscale.list >/dev/null
    $sudo apt-get update
    $sudo apt-get install -y tailscale
  fi

  if ! tailscale version >/dev/null 2>&1; then
    echo
    echo "Tailscale è installato ma non parte su questo Pi."
    echo "Scrivi a Claude cosa risponde il comando:  tailscale version"
    return 1
  fi

  echo
  echo "Ora collego il Pi al tuo account Tailscale."
  echo "Inquadra il codice QR col telefono, oppure apri il link sul Mac, e accedi."
  echo
  $sudo tailscale up --qr

  local ip porta=8080
  ip="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
  if [ -r /opt/videoteca/config.json ]; then
    porta="$(python3 -c 'import json; print(json.load(open("/opt/videoteca/config.json")).get("port", 8080))' 2>/dev/null || echo 8080)"
  fi
  echo
  echo "Fatto! Con Tailscale acceso anche sul Mac o sul telefono, da qualsiasi rete:"
  echo "  Videoteca:  http://$ip:$porta"
  echo "  Terminale:  ssh $utente@$ip"
  echo "Consiglio: nella console di Tailscale, sul Pi scegli «Disable key expiry»,"
  echo "così non devi rifare l'accesso tra qualche mese."
}

main "$@"; exit $?
