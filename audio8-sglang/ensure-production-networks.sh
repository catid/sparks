#!/usr/bin/env bash
set -euo pipefail

[[ "${EUID}" == 0 ]] || {
  echo "Production network setup must run as root." >&2
  exit 2
}

backend_network=cerberus3-audio8-sglang-backend
backend_subnet=172.30.82.0/29
frontend_network=cerberus3-audio8-sglang-frontend
frontend_subnet=172.30.81.0/29
gateway_frontend_ip=172.30.81.2
backend_ip=172.30.82.2
gateway_backend_ip=172.30.82.3
firewall_chain=CERBERUS-AUDIO8
host_firewall_chain=CERBERUS-AUDIO8-HOST

ensure_network() {
  local name="$1" subnet="$2" internal="$3" role="$4"
  if ! docker network inspect "${name}" >/dev/null 2>&1; then
    args=(
      network create --driver bridge --subnet "${subnet}"
      --label "io.cerberus.audio8-sglang.role=${role}"
    )
    if [[ "${internal}" == true ]]; then
      args+=(--internal)
    fi
    docker "${args[@]}" "${name}" >/dev/null
  fi

  mapfile -t actual < <(
    docker network inspect --format \
      '{{.Driver}}{{println}}{{.Scope}}{{println}}{{.Internal}}{{println}}{{(index .IPAM.Config 0).Subnet}}{{println}}{{index .Labels "io.cerberus.audio8-sglang.role"}}' \
      "${name}"
  )
  expected=(bridge local "${internal}" "${subnet}" "${role}")
  [[ "${#actual[@]}" == "${#expected[@]}" ]] || {
    echo "Unexpected production network metadata: ${name}" >&2
    exit 1
  }
  for index in "${!expected[@]}"; do
    [[ "${actual[index]}" == "${expected[index]}" ]] || {
      echo "Refusing mismatched production network: ${name}" >&2
      exit 1
    }
  done
  [[ "$(docker network inspect --format '{{len .Containers}}' "${name}")" =~ ^[0-2]$ ]] || {
    echo "Unexpected attachment count on production network: ${name}" >&2
    exit 1
  }
}

ensure_firewall() {
  if ! iptables -S "${firewall_chain}" >/dev/null 2>&1; then
    iptables -N "${firewall_chain}"
    iptables -A "${firewall_chain}" \
      -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
    iptables -A "${firewall_chain}" -j REJECT --reject-with icmp-port-unreachable
  fi
  mapfile -t rules < <(iptables -S "${firewall_chain}")
  expected=(
    "-N ${firewall_chain}"
    "-A ${firewall_chain} -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN"
    "-A ${firewall_chain} -j REJECT --reject-with icmp-port-unreachable"
  )
  [[ "${rules[*]}" == "${expected[*]}" ]] || {
    echo "Refusing unexpected ${firewall_chain} firewall rules." >&2
    exit 1
  }
  iptables -C DOCKER-USER -s "${gateway_frontend_ip}/32" \
    -j "${firewall_chain}" 2>/dev/null || \
    iptables -I DOCKER-USER 1 -s "${gateway_frontend_ip}/32" \
      -j "${firewall_chain}"
  mapfile -t docker_user_rules < <(iptables -S DOCKER-USER)
  [[ "${docker_user_rules[1]:-}" == \
    "-A DOCKER-USER -s ${gateway_frontend_ip}/32 -j ${firewall_chain}" ]] || {
    echo "Gateway egress firewall jump is not first in DOCKER-USER." >&2
    exit 1
  }

  if ! iptables -S "${host_firewall_chain}" >/dev/null 2>&1; then
    iptables -N "${host_firewall_chain}"
    iptables -A "${host_firewall_chain}" \
      -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
    iptables -A "${host_firewall_chain}" \
      -j REJECT --reject-with icmp-port-unreachable
  fi
  mapfile -t host_rules < <(iptables -S "${host_firewall_chain}")
  host_expected=(
    "-N ${host_firewall_chain}"
    "-A ${host_firewall_chain} -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN"
    "-A ${host_firewall_chain} -j REJECT --reject-with icmp-port-unreachable"
  )
  [[ "${host_rules[*]}" == "${host_expected[*]}" ]] || {
    echo "Refusing unexpected ${host_firewall_chain} firewall rules." >&2
    exit 1
  }
  iptables -C INPUT -s "${gateway_backend_ip}/32" \
    -j "${host_firewall_chain}" 2>/dev/null || \
    iptables -I INPUT 1 -s "${gateway_backend_ip}/32" \
      -j "${host_firewall_chain}"
  iptables -C INPUT -s "${gateway_frontend_ip}/32" \
    -j "${host_firewall_chain}" 2>/dev/null || \
    iptables -I INPUT 1 -s "${gateway_frontend_ip}/32" \
      -j "${host_firewall_chain}"
  iptables -C INPUT -s "${backend_ip}/32" \
    -j "${host_firewall_chain}" 2>/dev/null || \
    iptables -I INPUT 1 -s "${backend_ip}/32" \
      -j "${host_firewall_chain}"
  mapfile -t input_rules < <(iptables -S INPUT)
  [[ "${input_rules[1]:-}" == \
    "-A INPUT -s ${backend_ip}/32 -j ${host_firewall_chain}" &&
     "${input_rules[2]:-}" == \
    "-A INPUT -s ${gateway_frontend_ip}/32 -j ${host_firewall_chain}" &&
     "${input_rules[3]:-}" == \
    "-A INPUT -s ${gateway_backend_ip}/32 -j ${host_firewall_chain}" ]] || {
    echo "Audio8 host-deny firewall jumps are not first in INPUT." >&2
    exit 1
  }

  # An unpublished Docker bridge address is still routable from its host.
  # Block ordinary local processes from bypassing the fixed-reference gateway.
  # Gateway-to-backend traffic traverses FORWARD, so this does not block it.
  iptables -C OUTPUT -d "${gateway_backend_ip}/32" \
    -j "${host_firewall_chain}" 2>/dev/null || \
    iptables -I OUTPUT 1 -d "${gateway_backend_ip}/32" \
      -j "${host_firewall_chain}"
  iptables -C OUTPUT -d "${backend_ip}/32" \
    -j "${host_firewall_chain}" 2>/dev/null || \
    iptables -I OUTPUT 1 -d "${backend_ip}/32" \
      -j "${host_firewall_chain}"
  mapfile -t output_rules < <(iptables -S OUTPUT)
  [[ "${output_rules[1]:-}" == \
    "-A OUTPUT -d ${backend_ip}/32 -j ${host_firewall_chain}" &&
     "${output_rules[2]:-}" == \
    "-A OUTPUT -d ${gateway_backend_ip}/32 -j ${host_firewall_chain}" ]] || {
    echo "Audio8 host-deny firewall jumps are not first in OUTPUT." >&2
    exit 1
  }
}

ensure_network "${backend_network}" "${backend_subnet}" true backend
ensure_network "${frontend_network}" "${frontend_subnet}" false frontend
ensure_firewall
