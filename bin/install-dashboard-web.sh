#!/usr/bin/env bash
set -euo pipefail

# Install the already-present Nginx dashboard configuration. This script does
# not install OS packages; on a fresh host, install nginx first.

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
nginx_source="${root_dir}/dashboard/nginx-spark1-dashboard.conf"
site_target="/etc/nginx/sites-available/dgx-spark-dashboard"
cert_dir="/etc/nginx/ssl"
dashboard_lan_ip="${DASHBOARD_LAN_IP:-}"
cert_file="${cert_dir}/spark1.lan.crt"
key_file="${cert_dir}/spark1.lan.key"

if ! command -v nginx >/dev/null 2>&1; then
  echo "nginx is not installed" >&2
  exit 1
fi

sudo install -d -o root -g root -m 0755 "${cert_dir}"
if [[ ! -s "${cert_file}" || ! -s "${key_file}" ]]; then
  subject_alt_name="DNS:spark1.lan"
  if [[ -n "${dashboard_lan_ip}" ]]; then
    if [[ ! "${dashboard_lan_ip}" =~ ^[0-9a-fA-F:.]+$ ]]; then
      echo "DASHBOARD_LAN_IP is not a valid IP literal" >&2
      exit 2
    fi
    subject_alt_name+=",IP:${dashboard_lan_ip}"
  fi
  sudo openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 365 \
    -subj "/CN=spark1.lan" \
    -addext "subjectAltName=${subject_alt_name}" \
    -keyout "${key_file}" \
    -out "${cert_file}"
  sudo chmod 0600 "${key_file}"
  sudo chmod 0644 "${cert_file}"
fi

sudo install -o root -g root -m 0644 "${nginx_source}" "${site_target}"
if [[ -L /etc/nginx/sites-enabled/default ]]; then
  sudo unlink /etc/nginx/sites-enabled/default
fi
sudo ln -sfn "${site_target}" /etc/nginx/sites-enabled/dgx-spark-dashboard
sudo nginx -t
sudo systemctl enable nginx.service
sudo systemctl restart nginx.service

echo "Dashboard web endpoints are active:"
echo "  http://spark1.lan"
echo "  https://spark1.lan"
