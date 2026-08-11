#!/usr/bin/env bash
set -euo pipefail

audio="${AUDIO8_TEST_WAV:-${HOME}/.local/state/audio8/silly-test.wav}"
device="${AUDIO8_ALSA_DEVICE:-plughw:CARD=CP900,DEV=0}"
pause_seconds="${AUDIO8_LOOP_PAUSE_SECONDS:-2}"

[[ -f "${audio}" && ! -L "${audio}" ]] || {
  echo "Missing regular test WAV: ${audio}" >&2
  exit 2
}
[[ "${pause_seconds}" =~ ^[0-9]+([.][0-9]+)?$ ]] || {
  echo "Invalid AUDIO8_LOOP_PAUSE_SECONDS: ${pause_seconds}" >&2
  exit 2
}
aplay -l | grep -Fq 'CP900' || {
  echo "Yealink CP900 is not available to ALSA." >&2
  exit 1
}

while :; do
  aplay -q -D "${device}" "${audio}"
  sleep "${pause_seconds}"
done
