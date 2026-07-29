#!/usr/bin/env bash

set -euo pipefail

action="${1:-status}"

usage() {
  cat <<'EOF'
Usage: configure-headless.sh enable|restore-gui|status

enable       Set multi-user.target as the boot default and stop the current
             display manager. X/GNOME packages and NVIDIA's stock xorg.conf
             remain installed for easy rollback.
restore-gui  Restore graphical.target and start the display manager.
status       Show the current default target and display-manager state.
EOF
}

show_status() {
  printf 'default_target=%s\n' "$(systemctl get-default)"
  printf 'graphical_target=%s\n' "$(systemctl is-active graphical.target || true)"
  printf 'display_manager=%s\n' \
    "$(systemctl is-active display-manager.service 2>/dev/null || true)"
  if pgrep -x Xorg >/dev/null 2>&1 ||
     pgrep -x Xwayland >/dev/null 2>&1 ||
     pgrep -x gnome-shell >/dev/null 2>&1; then
    echo "desktop_processes=running"
  else
    echo "desktop_processes=absent"
  fi
}

case "${action}" in
  enable)
    sudo systemctl set-default multi-user.target
    sudo systemctl stop display-manager.service 2>/dev/null || true
    show_status
    ;;
  restore-gui)
    sudo systemctl set-default graphical.target
    sudo systemctl start display-manager.service
    show_status
    ;;
  status)
    show_status
    ;;
  -h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
