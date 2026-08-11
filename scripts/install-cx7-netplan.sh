#!/usr/bin/env bash

set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
requested_role="${1:-}"
apply=0
c3_port_map=""
c3_port_map_seen=0
target="/etc/netplan/40-cx7.yaml"
sys_class_net_root="${CX7_SYS_CLASS_NET_ROOT:-/sys/class/net}"

usage() {
  cat <<'EOF'
Usage: install-cx7-netplan.sh cerberus1|cerberus2|cerberus3 \
  [--c3-port-map c3-p0-to-c1|c3-p0-to-c2] [--apply]

The legacy cerebrus1-3 misspelling and transitional spark1-3 aliases are also
accepted during hostname migration and normalize to cerberus1-3. Without
--apply, the selected canonical repository file or explicit C3 variant is
validated in an isolated Netplan root. With
--apply, the exact target is backed up, installed, generated, applied, and
checked against its reachable ring neighbor(s).

The C3 port-map option is valid only for cerberus3:
  c3-p0-to-c1  C3 P0 faces C1 and C3 P1 faces C2 (current canonical file).
  c3-p0-to-c2  C3 P0 faces C2 and C3 P1 faces C1 (NVIDIA crossed layout).

A cerberus3 dry run without the option validates the unchanged canonical
c3-p0-to-c1 file. A cerberus3 apply requires an explicit port map so a cable
swap can never silently install the wrong address-to-port assignment.

The management interface is never changed. Retain console access when applying
Netplan remotely.
EOF
}

canonical_role() {
  case "$1" in
    cerberus1|cerebrus1|spark1) printf 'cerberus1\n' ;;
    cerberus2|cerebrus2|spark2) printf 'cerberus2\n' ;;
    cerberus3|cerebrus3|spark3) printf 'cerberus3\n' ;;
    *) return 1 ;;
  esac
}

case "${requested_role}" in
  -h|--help)
    usage
    exit 0
    ;;
  '')
    usage >&2
    exit 2
    ;;
esac

if ! role="$(canonical_role "${requested_role}")"; then
  usage >&2
  exit 2
fi
shift
while (($#)); do
  case "$1" in
    --apply) apply=1 ;;
    --c3-port-map)
      if ((c3_port_map_seen)); then
        echo "Specify --c3-port-map only once." >&2
        exit 2
      fi
      shift
      if (($# == 0)); then
        echo "--c3-port-map requires c3-p0-to-c1 or c3-p0-to-c2." >&2
        exit 2
      fi
      c3_port_map="$1"
      c3_port_map_seen=1
      ;;
    --c3-port-map=*)
      if ((c3_port_map_seen)); then
        echo "Specify --c3-port-map only once." >&2
        exit 2
      fi
      c3_port_map="${1#*=}"
      c3_port_map_seen=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [[ "${role}" == "cerberus3" ]]; then
  if [[ -z "${c3_port_map}" ]]; then
    c3_port_map=c3-p0-to-c1
  fi
  case "${c3_port_map}" in
    c3-p0-to-c1)
      source_file="${root_dir}/netplan/cerberus3-40-cx7.yaml"
      ;;
    c3-p0-to-c2)
      source_file="${root_dir}/netplan/cerberus3-40-cx7-p0-to-c2.yaml"
      ;;
    *)
      echo "Invalid C3 port map: ${c3_port_map}." >&2
      usage >&2
      exit 2
      ;;
  esac
  if ((apply && !c3_port_map_seen)); then
    echo "Refusing to apply cerberus3 networking without an explicit --c3-port-map." >&2
    exit 2
  fi
elif ((c3_port_map_seen)); then
  echo "--c3-port-map is valid only for cerberus3." >&2
  exit 2
else
  source_file="${root_dir}/netplan/${role}-40-cx7.yaml"
fi

if [[ ! -f "${source_file}" || -L "${source_file}" ]]; then
  echo "Missing Netplan source: ${source_file}" >&2
  exit 1
fi

actual_host="$(hostname -s)"
if ((apply)); then
  if ! actual_role="$(canonical_role "${actual_host}")" ||
      [[ "${actual_role}" != "${role}" ]]; then
    echo "Refusing to install ${role} networking on host ${actual_host}." >&2
    exit 2
  fi
fi

for interface in \
  enp1s0f0np0 enP2p1s0f0np0 enp1s0f1np1 enP2p1s0f1np1; do
  if [[ ! -d "${sys_class_net_root}/${interface}" ]]; then
    echo "Required ConnectX-7 netdev is absent: ${interface}" >&2
    exit 1
  fi
done

validation_root="$(mktemp -d)"
rollback_required=0
target_existed=0
backup=""
cleanup_and_rollback() {
  local status=$?
  trap - EXIT
  set +e
  if ((rollback_required)); then
    echo "CX-7 verification failed; restoring the prior Netplan state." >&2
    if ((target_existed)); then
      sudo cp -a -- "${backup}" "${target}"
      restore_status=$?
    else
      sudo /usr/bin/rm -f -- "${target}"
      restore_status=$?
    fi
    if ((restore_status == 0)); then
      sudo netplan generate && sudo netplan apply
      reapply_status=$?
      if ((reapply_status != 0)); then
        echo "WARNING: the prior file was restored, but reapplying it failed." >&2
      fi
    else
      echo "WARNING: restoring the prior Netplan file failed." >&2
    fi
  fi
  # netplan generate runs through sudo and can leave root-owned descendants.
  # The target is the exact directory returned by mktemp, never a glob.
  sudo /usr/bin/rm -rf -- "${validation_root}"
  exit "${status}"
}
trap cleanup_and_rollback EXIT
install -d -m 0755 "${validation_root}/etc/netplan"
install -m 0600 "${source_file}" "${validation_root}/etc/netplan/40-cx7.yaml"
sudo netplan generate --root-dir "${validation_root}"

if [[ "${role}" == "cerberus3" ]]; then
  echo "Validated ${source_file} for ${role} (${c3_port_map})."
else
  echo "Validated ${source_file} for ${role}."
fi
if ((!apply)); then
  echo "Dry run only; rerun with --apply to install ${target}."
  exit 0
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
if sudo test -e "${target}"; then
  target_existed=1
  backup="${target}.before-cx7-ring-${timestamp}"
  sudo cp -a -- "${target}" "${backup}"
fi
rollback_required=1
sudo install -o root -g root -m 0600 "${source_file}" "${target}"
sudo netplan generate
sudo netplan apply

case "${role}" in
  cerberus1|cerberus2)
    CX7_NODE_ROLE="${role}" \
      "${root_dir}/bin/wait-cx7-ready.sh" --check-once --scope tp2
    ;;
  cerberus3)
    CX7_NODE_ROLE="${role}" CX7_C3_PORT_MAP="${c3_port_map}" \
      "${root_dir}/bin/wait-cx7-ready.sh" --check-once --scope ring
    ;;
esac
rollback_required=0
echo "Installed and verified ${target} for ${role}."
