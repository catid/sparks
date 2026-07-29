#!/usr/bin/env bash

set -euo pipefail

usage() {
  echo "usage: $0 --staged|--tracked" >&2
}

if [[ "$#" -ne 1 ]]; then
  usage
  exit 2
fi

case "$1" in
  --staged)
    mapfile -d '' files < <(
      git diff --cached --name-only --diff-filter=ACMR -z
    )
    ;;
  --tracked)
    mapfile -d '' files < <(git ls-files -z)
    ;;
  *)
    usage
    exit 2
    ;;
esac

if [[ "${#files[@]}" -eq 0 ]]; then
  echo "public-safety: no files to scan"
  exit 0
fi

failed=0
tmp_dir="$(mktemp -d)"
trap 'rm -rf -- "${tmp_dir}"' EXIT

high_confidence_secret_re='(sk-(proj|ant|or-v1)-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{24,}|xapp-[A-Za-z0-9-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|hf_[A-Za-z0-9]{20,}|csk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN ([A-Z0-9 ]+ )?PRIVATE KEY-----)'
provider_assignment_re='(OPENAI|ANTHROPIC|GOOGLE|GEMINI|SLACK|HF|HUGGING_FACE|OPENROUTER|EXA|KAGGLE|DROID|CEREBRAS|AWS)_[A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD)[[:space:]]*=[[:space:]]*["'\'']?[A-Za-z0-9/+_.:-]{16,}'
management_ipv4_re='(^|[^0-9])10\.([0-9]{1,3}\.){2}[0-9]{1,3}([^0-9]|$)'

for path in "${files[@]}"; do
  if [[ "${path}" == results/* ]]; then
    case "${path}" in
      results/AGENT_EVAL.md|\
      results/BASELINE.md|\
      results/DEEPSEEK_V4_2SPARK_REPORT.md|\
      results/DEEPSEEK_V4_AGENT_EVAL_MAX.md|\
      results/DEEPSEEK_V4_DSPARK_AGENT_EVAL_MAX.md|\
      results/DFLASH_REALISTIC.md|\
      results/DGX_SPARK_COOLING_CHECK_20260729.md|\
      results/PRODUCTION_VALIDATION.md|\
      results/deepseek-v4-fixed1024-comparison.csv|\
      results/deepseek-v4-loader-asymmetry.md|\
      results/deepseek-v4-persistence-audit.md|\
      results/deepseek-v4-tp2-dflash-all-cudagraph-failure.md|\
      results/dsv4-tp2-dspark-official-nvfp4-k5-c32-fixed1024/run_manifest.json|\
      results/dsv4-tp2-dspark-official-nvfp4-k5-c32-fixed1024/summary.csv|\
      results/dsv4-tp2-dspark-official-nvfp4-k5-c32-fixed1024/summary.json|\
      results/dsv4-tp2-dspark-official-nvfp4-k5-c32-fixed1024/waves.csv)
        ;;
      *)
        echo "public-safety: result path is not explicitly approved: ${path}" >&2
        failed=1
        continue
        ;;
    esac
  fi

  case "${path}" in
    logs/*|staging/*|dashboard/dashboard.env.lan|openclaw/openclaw.json)
      echo "public-safety: forbidden active/runtime path: ${path}" >&2
      failed=1
      continue
      ;;
    results/*/responses/*|results/*/telemetry/*|results/*.requests.csv|\
    results/*.requests.jsonl|results/*.jsonl|results/*.log|results/*.prom)
      echo "public-safety: forbidden raw result path: ${path}" >&2
      failed=1
      continue
      ;;
    *.pem|*.p12|*.pfx|*.key|*/id_rsa*|*/id_ed25519*|*known_hosts*)
      echo "public-safety: forbidden key/certificate path: ${path}" >&2
      failed=1
      continue
      ;;
    *.env)
      if [[ "${path}" != "dspark_mia/mia.env" &&
            "${path}" != "dspark_mia/mia-throughput.env" ]]; then
        echo "public-safety: unapproved .env file: ${path}" >&2
        failed=1
        continue
      fi
      ;;
  esac

  # Gitlinks (such as the pinned upstream submodule) have no regular file
  # content in the parent repository.
  if [[ ! -f "${path}" ]]; then
    continue
  fi

  size="$(stat -c '%s' -- "${path}")"
  if (( size > 10 * 1024 * 1024 )); then
    echo "public-safety: file exceeds 10 MiB review limit: ${path}" >&2
    failed=1
    continue
  fi

  materialized="${tmp_dir}/content"
  if [[ "$1" == "--staged" ]]; then
    git show ":${path}" >"${materialized}"
  else
    cp -- "${path}" "${materialized}"
  fi

  if LC_ALL=C grep -I -E -q \
      "${high_confidence_secret_re}|${provider_assignment_re}" \
      "${materialized}"; then
    echo "public-safety: possible credential in ${path}" >&2
    failed=1
  fi
  if LC_ALL=C grep -I -E -q "${management_ipv4_re}" "${materialized}"; then
    echo "public-safety: literal 10/8 management address in ${path}" >&2
    failed=1
  fi
done

if (( failed != 0 )); then
  echo "public-safety: FAILED; nothing should be pushed" >&2
  exit 1
fi

echo "public-safety: ${#files[@]} files passed"
