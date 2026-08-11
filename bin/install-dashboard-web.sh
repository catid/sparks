#!/usr/bin/env bash
set -euo pipefail

# Install the already-present Nginx dashboard configuration. This script does
# not install OS packages; on a fresh host, install nginx first.

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
nginx_source="${root_dir}/dashboard/nginx-cerberus1-dashboard.conf"
site_target="/etc/nginx/sites-available/dgx-spark-dashboard"
cert_dir="/etc/nginx/ssl"
dashboard_web_host="${DASHBOARD_WEB_HOST:-cerberus1.lan}"
dashboard_web_alt_host="${DASHBOARD_WEB_ALT_HOST:-cerberus1.local}"
dashboard_lan_ip="${DASHBOARD_LAN_IP:-}"

validate_dns_name() {
  local setting="$1" value="$2" label
  if ! [[ "${value}" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]] ||
     [[ "${value}" == *..* ]]; then
    echo "${setting} must be a plain DNS hostname." >&2
    exit 2
  fi
  while IFS= read -r label; do
    if ((${#label} == 0 || ${#label} > 63)) ||
       [[ "${label}" == -* || "${label}" == *- ]]; then
      echo "${setting} contains an invalid DNS label." >&2
      exit 2
    fi
  done < <(tr . '\n' <<<"${value}")
}
validate_dns_name DASHBOARD_WEB_HOST "${dashboard_web_host}"
validate_dns_name DASHBOARD_WEB_ALT_HOST "${dashboard_web_alt_host}"

cert_file="${cert_dir}/${dashboard_web_host}.crt"
key_file="${cert_dir}/${dashboard_web_host}.key"

if ! command -v nginx >/dev/null 2>&1; then
  echo "nginx is not installed" >&2
  exit 1
fi

sudo install -d -o root -g root -m 0755 "${cert_dir}"
certificate_matches_host() {
  sudo openssl x509 -in "${cert_file}" -noout -checkhost "$1" 2>/dev/null |
    grep -Fqx "Hostname $1 does match certificate"
}
if [[ ! -s "${cert_file}" || ! -s "${key_file}" ]] ||
   ! certificate_matches_host "${dashboard_web_host}" ||
   ! certificate_matches_host "${dashboard_web_alt_host}"; then
  subject_alt_name="DNS:${dashboard_web_host}"
  if [[ "${dashboard_web_alt_host}" != "${dashboard_web_host}" ]]; then
    subject_alt_name+=",DNS:${dashboard_web_alt_host}"
  fi
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
sed "s|cerberus1\\.lan|${dashboard_web_host}|g" \
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
if [[ "${dashboard_web_alt_host}" != "${dashboard_web_host}" ]]; then
  echo "  https://${dashboard_web_alt_host}"
fi
