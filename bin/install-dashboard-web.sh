#!/usr/bin/env bash
set -euo pipefail

# Install the already-present Nginx dashboard configuration. This script does
# not install OS packages; on a fresh host, install nginx first.

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
nginx_source="${root_dir}/dashboard/nginx-spark1-dashboard.conf"
site_target="/etc/nginx/sites-available/dgx-spark-dashboard"
cert_dir="/etc/nginx/ssl"
dashboard_web_host="${DASHBOARD_WEB_HOST:-cerebrus1.lan}"
dashboard_lan_ip="${DASHBOARD_LAN_IP:-}"

if ! [[ "${dashboard_web_host}" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]] ||
   [[ "${dashboard_web_host}" == *..* ]]; then
  echo "DASHBOARD_WEB_HOST must be a plain DNS hostname." >&2
  exit 2
fi
while IFS= read -r label; do
  if ((${#label} == 0 || ${#label} > 63)) ||
     [[ "${label}" == -* || "${label}" == *- ]]; then
    echo "DASHBOARD_WEB_HOST contains an invalid DNS label." >&2
    exit 2
  fi
done < <(tr . '\n' <<<"${dashboard_web_host}")

cert_file="${cert_dir}/${dashboard_web_host}.crt"
key_file="${cert_dir}/${dashboard_web_host}.key"

if ! command -v nginx >/dev/null 2>&1; then
  echo "nginx is not installed" >&2
  exit 1
fi

sudo install -d -o root -g root -m 0755 "${cert_dir}"
if [[ ! -s "${cert_file}" || ! -s "${key_file}" ]]; then
  subject_alt_name="DNS:${dashboard_web_host}"
  if [[ -n "${dashboard_lan_ip}" ]]; then
    python3 - "${dashboard_lan_ip}" <<'PY' || {
import ipaddress
import sys

ipaddress.ip_address(sys.argv[1])
PY
      echo "DASHBOARD_LAN_IP is not a valid IP literal" >&2
      exit 2
    }
    subject_alt_name+=",IP:${dashboard_lan_ip}"
  fi
  sudo openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 365 \
    -subj "/CN=${dashboard_web_host}" \
    -addext "subjectAltName=${subject_alt_name}" \
    -keyout "${key_file}" \
    -out "${cert_file}"
  sudo chmod 0600 "${key_file}"
  sudo chmod 0644 "${cert_file}"
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf -- "${tmp_dir}"' EXIT
nginx_rendered="${tmp_dir}/dgx-spark-dashboard"
sed "s|spark1\\.lan|${dashboard_web_host}|g" \
  "${nginx_source}" >"${nginx_rendered}"

sudo install -o root -g root -m 0644 "${nginx_rendered}" "${site_target}"
if [[ -L /etc/nginx/sites-enabled/default ]]; then
  sudo unlink /etc/nginx/sites-enabled/default
fi
sudo ln -sfn "${site_target}" /etc/nginx/sites-enabled/dgx-spark-dashboard
sudo nginx -t
sudo systemctl enable nginx.service
sudo systemctl restart nginx.service

echo "Dashboard web endpoints are active:"
echo "  http://${dashboard_web_host}"
echo "  https://${dashboard_web_host}"
