#!/usr/bin/env bash
set -euo pipefail

tp="${1:?usage: $0 TP PP NNODES HEADS EXPERTS LAYERS [PARTITION]}"
pp="${2:?usage: $0 TP PP NNODES HEADS EXPERTS LAYERS [PARTITION]}"
nnodes="${3:?usage: $0 TP PP NNODES HEADS EXPERTS LAYERS [PARTITION]}"
heads="${4:?usage: $0 TP PP NNODES HEADS EXPERTS LAYERS [PARTITION]}"
experts="${5:?usage: $0 TP PP NNODES HEADS EXPERTS LAYERS [PARTITION]}"
layers="${6:?usage: $0 TP PP NNODES HEADS EXPERTS LAYERS [PARTITION]}"
partition="${7:-}"

for value in "${tp}" "${pp}" "${nnodes}" "${heads}" "${experts}" "${layers}"; do
  [[ "${value}" =~ ^[1-9][0-9]*$ ]] || {
    echo "Parallelism and model dimensions must be positive integers." >&2
    exit 2
  }
done

if ((tp == 3)); then
  echo "TP3 is invalid for this checkpoint: 64 attention heads and 256 routed experts are not divisible by 3. Use TP1/PP3 for three one-GPU nodes." >&2
  exit 1
fi
if ((heads % tp != 0)); then
  echo "TP=${tp} does not divide ${heads} attention heads." >&2
  exit 1
fi
if ((experts % tp != 0)); then
  echo "TP=${tp} does not divide ${experts} routed experts." >&2
  exit 1
fi
if ((tp * pp != nnodes)); then
  echo "One GPU per node requires TP*PP=NNODES; got ${tp}*${pp}!=${nnodes}." >&2
  exit 1
fi

if [[ -n "${partition}" ]]; then
  IFS=',' read -r -a stages <<<"${partition}"
  if ((${#stages[@]} != pp)); then
    echo "Layer partition must contain exactly ${pp} stages: ${partition}" >&2
    exit 1
  fi
  sum=0
  for stage in "${stages[@]}"; do
    [[ "${stage}" =~ ^[1-9][0-9]*$ ]] || {
      echo "Layer partition entries must be positive integers: ${partition}" >&2
      exit 1
    }
    ((sum += stage))
  done
  if ((sum != layers)); then
    echo "Layer partition sums to ${sum}, expected ${layers}: ${partition}" >&2
    exit 1
  fi
fi

printf 'valid TP=%s PP=%s NNODES=%s partition=%s\n' \
  "${tp}" "${pp}" "${nnodes}" "${partition:-automatic}"
