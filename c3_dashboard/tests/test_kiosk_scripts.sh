#!/usr/bin/env bash

set -euo pipefail

dashboard_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
fixture="${dashboard_dir}/tests/fixtures/fake-command"
test_root="$(mktemp -d)"
trap 'rm -rf -- "${test_root}"' EXIT

fake_bin="${test_root}/bin"
runtime_home="${test_root}/runtime"
command_log="${test_root}/commands.log"
mkdir -p "${fake_bin}" "${runtime_home}"
for command_name in \
  Xorg mcookie startx xauth xinit xrandr xset dbus-run-session python3 sleep; do
  ln -s "${fixture}" "${fake_bin}/${command_name}"
done

export C3_TEST_COMMAND_LOG="${command_log}"
export C3_KIOSK_RUNTIME_HOME="${runtime_home}"
export PATH="${fake_bin}:/usr/bin:/bin"
sed "s|@HOME@|${HOME}|g" \
  "${dashboard_dir}/dashboard.env.example" >"${test_root}/good.env"

: >"${command_log}"
"${dashboard_dir}/scripts/launch-kiosk.sh"
grep -Eq '^python3 .*kiosk.py .*--check .*--url .*127.0.0.1:9763/' \
  "${command_log}"
grep -Eq '^startx .*kiosk-session.sh -- .*/Xorg :0 vt7 -keeptty -nolisten tcp -noreset$' \
  "${command_log}"
grep -Fq "STARTX_ENV HOME=${runtime_home} XDG_RUNTIME_DIR=${runtime_home}" \
  "${command_log}"

ln -sfn "${dashboard_dir}/tests/fixtures/fake-startx-noauth" \
  "${fake_bin}/startx"
set +e
"${dashboard_dir}/scripts/launch-kiosk.sh" \
  >"${test_root}/no-xauth.out" 2>&1
no_xauth_status=$?
set -e
[[ "${no_xauth_status}" == "64" ]]
grep -Fq 'startx does not advertise Xorg cookie authentication' \
  "${test_root}/no-xauth.out"
ln -sfn "${fixture}" "${fake_bin}/startx"

: >"${command_log}"
C3_KIOSK_DISPLAY=:9 C3_KIOSK_VT=vt9 \
  "${dashboard_dir}/scripts/launch-kiosk.sh"
grep -Eq '^startx .*kiosk-session.sh -- .*/Xorg :0 vt7 -keeptty -nolisten tcp -noreset$' \
  "${command_log}"

: >"${command_log}"
C3_TEST_XRANDR_STATE=connected \
  "${dashboard_dir}/scripts/kiosk-session.sh"
grep -Fq 'xrandr --output TV-0 --mode 1424x280 --pos 0x0 --primary --fb 1424x280' \
  "${command_log}"
grep -Fq 'xset s off' "${command_log}"
grep -Eq '^dbus-run-session -- python3 .*kiosk.py --url .*127.0.0.1:9763/ --size 1424x280 --retry-seconds 5$' \
  "${command_log}"

: >"${command_log}"
set +e
C3_TEST_XRANDR_STATE=disconnected C3_KIOSK_OUTPUT_WAIT_SECONDS=1 \
  "${dashboard_dir}/scripts/kiosk-session.sh" \
  >"${test_root}/headless.out" 2>&1
headless_status=$?
set -e
[[ "${headless_status}" == "75" ]]
grep -Fq 'no connected output accepted native mode 1424x280 after 1s' \
  "${test_root}/headless.out"
if grep -q '^dbus-run-session ' "${command_log}"; then
  echo "Headless session unexpectedly launched WebKit." >&2
  exit 1
fi

: >"${command_log}"
C3_TEST_XRANDR_STATE=multiple C3_KIOSK_OUTPUT=TV-0 \
  "${dashboard_dir}/scripts/kiosk-session.sh"
grep -Fq 'xrandr --output DP-1 --off --output TV-0 --mode 1424x280 --pos 0x0 --primary --fb 1424x280' \
  "${command_log}"

sed \
  -e 's/^C3_DASHBOARD_PORT=.*/C3_DASHBOARD_PORT=80/' \
  -e 's|^C3_KIOSK_URL=.*|C3_KIOSK_URL=http://127.0.0.1:0/|' \
  "${dashboard_dir}/dashboard.env.example" \
  >"${test_root}/bad-port.env"
set +e
PATH=/usr/bin:/bin C3_DASHBOARD_ENV_FILE="${test_root}/bad-port.env" \
  "${dashboard_dir}/scripts/install.sh" verify \
  >"${test_root}/bad-port.out" 2>&1
bad_port_status=$?
set -e
[[ "${bad_port_status}" != "0" ]]
grep -Fq 'C3_KIOSK_URL port must be between 1 and 65535' \
  "${test_root}/bad-port.out"

sed 's/^C3_KIOSK_RETRY_SECONDS=.*/C3_KIOSK_RETRY_SECONDS=301/' \
  "${test_root}/good.env" >"${test_root}/bad-retry.env"
set +e
/usr/bin/python3 "${dashboard_dir}/scripts/validate-environment.py" \
  "${test_root}/bad-retry.env" >"${test_root}/bad-retry.out" 2>&1
bad_retry_status=$?
set -e
[[ "${bad_retry_status}" == "2" ]]
grep -Fq 'C3_KIOSK_RETRY_SECONDS must be between 1 and 300' \
  "${test_root}/bad-retry.out"

sed 's|^C3_KIOSK_URL=.*|C3_KIOSK_URL=http://user:pass@127.0.0.1:9763/|' \
  "${test_root}/good.env" >"${test_root}/credentials.env"
set +e
/usr/bin/python3 "${dashboard_dir}/scripts/validate-environment.py" \
  "${test_root}/credentials.env" >"${test_root}/credentials.out" 2>&1
credentials_status=$?
set -e
[[ "${credentials_status}" == "2" ]]
grep -Fq 'C3_KIOSK_URL cannot contain credentials' \
  "${test_root}/credentials.out"

set +e
PATH=/usr/bin:/bin SPARK_SERVICE_USER=root \
  "${dashboard_dir}/scripts/install.sh" verify \
  >"${test_root}/root-user.out" 2>&1
root_user_status=$?
set -e
[[ "${root_user_status}" == "2" ]]
grep -Fq 'service user must be unprivileged, not root' \
  "${test_root}/root-user.out"

# The search strings intentionally match literal shell variables in install.sh.
# shellcheck disable=SC2016
preserved_validation_line="$(
  grep -nF 'python3 "${validator}" "${environment_target}"' \
    "${dashboard_dir}/scripts/install.sh" | cut -d: -f1
)"
# shellcheck disable=SC2016
enable_line="$(
  grep -nF 'systemctl enable "${collector_unit}" "${kiosk_unit}"' \
    "${dashboard_dir}/scripts/install.sh" | cut -d: -f1
)"
preflight_line="$(
  grep -nF 'systemctl is-active --quiet display-manager.service' \
    "${dashboard_dir}/scripts/install.sh" | cut -d: -f1
)"
[[ -n "${preserved_validation_line}" ]]
[[ "${preflight_line}" -lt "${enable_line}" ]]

kiosk_unit="${dashboard_dir}/systemd/dgx-spark-c3-kiosk.service.in"
grep -Fq 'ExecStartPre=+/usr/bin/chvt 7' "${kiosk_unit}"
grep -Fq 'Environment=WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1' \
  "${kiosk_unit}"

echo "C3 kiosk launcher mock tests passed."
