#!/usr/bin/env bash
set -euo pipefail

# Compatibility entry point retained for automation written before the hosts
# were renamed. New callers should use monitor-cerberus1-reboot.sh.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${script_dir}/monitor-cerberus1-reboot.sh" "$@"
