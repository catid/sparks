#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
harness="${root}/deepseek_v4_bench/benchmark.py"
projector="${root}/deepseek_v4_bench/make_public_summary.py"

endpoint="http://127.0.0.1:8893"
label=""
artifact_root="${MIA3_BENCH_ARTIFACT_ROOT:-${XDG_STATE_HOME:-${HOME}/.local/state}/sparks/deepseek-v4-bench}"
public_dir=""
dry_run=0

usage() {
  cat <<'EOF'
Usage: run_mia3_fixed1024.sh --label LABEL [options]

Run the fixed three-Spark DS4F comparison workload:
  endpoint             http://127.0.0.1:8893
  concurrency          1, 2, 4, 8
  measured repeats     3
  prompt/output        approximately 1024 / exactly 1024 server tokens
  warm-up              one untimed 128-output-token wave per concurrency
  thinking             enabled, reasoning_effort=max

Options:
  --label LABEL         Public-safe configuration name (required for real runs)
  --endpoint URL        Override the OpenAI-compatible endpoint
  --artifact-root DIR   Private raw-artifact root
  --public-dir DIR      Also write allowlisted summary JSON/CSV here
  --dry-run             Validate the exact workload without network or writes
  -h, --help            Show this help

BENCH_PYTHON may select a Python with deepseek_v4_bench/requirements.txt.
VLLM_API_KEY is read by the benchmark, sent in memory, and never persisted.
EOF
}

while (($#)); do
  case "$1" in
    --label)
      (($# >= 2)) || { echo "--label requires a value" >&2; exit 2; }
      label="$2"
      shift 2
      ;;
    --endpoint)
      (($# >= 2)) || { echo "--endpoint requires a value" >&2; exit 2; }
      endpoint="$2"
      shift 2
      ;;
    --artifact-root)
      (($# >= 2)) || { echo "--artifact-root requires a value" >&2; exit 2; }
      artifact_root="$2"
      shift 2
      ;;
    --public-dir)
      (($# >= 2)) || { echo "--public-dir requires a value" >&2; exit 2; }
      public_dir="$2"
      shift 2
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "${label}" ]]; then
  if ((dry_run)); then
    label="mia3-dry-run"
  else
    echo "--label is required; name the exact runtime/profile being measured." >&2
    exit 2
  fi
fi
if [[ ! "${label}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$ ]]; then
  echo "Unsafe label: use 1-96 letters, digits, dots, underscores, or hyphens." >&2
  exit 2
fi
if [[ ! "${endpoint}" =~ ^https?://[^[:space:]]+$ ]]; then
  echo "--endpoint must be an http:// or https:// URL without whitespace." >&2
  exit 2
fi

python_bin="${BENCH_PYTHON:-${root}/.venv-bench/bin/python}"
if [[ ! -x "${python_bin}" ]]; then
  python_bin="$(command -v python3 || true)"
fi
if [[ -z "${python_bin}" || ! -x "${python_bin}" ]]; then
  echo "No executable Python found; set BENCH_PYTHON." >&2
  exit 2
fi
if ((!dry_run)); then
  "${python_bin}" -c 'import aiohttp' >/dev/null 2>&1 || {
    echo "${python_bin} cannot import aiohttp; install deepseek_v4_bench/requirements.txt." >&2
    exit 2
  }
fi

benchmark_args=(
  --endpoint "${endpoint}"
  --label "${label}"
  --concurrency 1 2 4 8
  --repeats 3
  --prompt-tokens 1024
  --prompt-tolerance 12
  --output-tokens 1024
  --warmup-output-tokens 128
  --timeout 7200
  --seed 260810
)

if ((dry_run)); then
  exec "${python_bin}" "${harness}" "${benchmark_args[@]}" --dry-run
fi

umask 077
if [[ -L "${artifact_root}" || (-e "${artifact_root}" && ! -d "${artifact_root}") ]]; then
  echo "Artifact root must be a real directory, not a symlink or file: ${artifact_root}" >&2
  exit 2
fi
# Only the leaf needs an explicit mode; umask 077 protects any parents created
# by -p, and the leaf's owner/mode are validated immediately below.
# shellcheck disable=SC2174
mkdir -p -m 0700 -- "${artifact_root}"
artifact_root="$(realpath -e -- "${artifact_root}")"
artifact_owner="$(stat -c '%u' -- "${artifact_root}")"
artifact_mode="$(stat -c '%a' -- "${artifact_root}")"
artifact_mode_value=$((8#${artifact_mode}))
if [[ "${artifact_owner}" != "$(id -u)" ]] ||
   (( (artifact_mode_value & 8#700) != 8#700 ||
      (artifact_mode_value & 8#077) != 0 )); then
  echo "Artifact root must be owned by the current user with mode 0700: ${artifact_root}" >&2
  exit 2
fi

# Raw prompts/reasoning must stay outside Git, or in a path explicitly ignored
# by Git. This makes an accidental --artifact-root inside the checkout fail
# closed rather than silently creating publishable model transcripts.
case "${artifact_root}/" in
  "${root}/"*)
    if ! git -C "${root}" check-ignore -q -- "${artifact_root}"; then
      echo "Raw artifact root is inside the checkout but is not Git-ignored: ${artifact_root}" >&2
      exit 2
    fi
    ;;
esac

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
run_dir="$(mktemp -d "${artifact_root}/${label}.${timestamp}.XXXXXX")"
chmod 0700 -- "${run_dir}"
runner_log="${run_dir}.runner.log"

echo "Private run directory: ${run_dir}"
set +e
"${python_bin}" "${harness}" \
  "${benchmark_args[@]}" \
  --output-dir "${run_dir}" \
  2>&1 | tee "${runner_log}"
pipeline_status=("${PIPESTATUS[@]}")
benchmark_status=${pipeline_status[0]}
log_status=${pipeline_status[1]}
set -e
if [[ -f "${runner_log}" ]]; then
  mv -- "${runner_log}" "${run_dir}/runner.log"
fi

summary_status=0
if [[ -f "${run_dir}/summary.json" ]]; then
  if [[ -n "${public_dir}" ]]; then
    mkdir -p -- "${public_dir}"
    public_dir="$(realpath -- "${public_dir}")"
    public_json="${public_dir}/${label}.${timestamp}.summary.json"
    public_csv="${public_dir}/${label}.${timestamp}.summary.csv"
  else
    public_json="${run_dir}/summary.public.json"
    public_csv="${run_dir}/summary.public.csv"
  fi
  if ! "${python_bin}" "${projector}" \
    --source "${run_dir}/summary.json" \
    --json-out "${public_json}" \
    --csv-out "${public_csv}" \
    --label "${label}"; then
    summary_status=1
    echo "Failed to create the allowlisted public summary." >&2
  fi
else
  summary_status=1
  echo "Benchmark produced no summary.json; partial private artifacts remain in ${run_dir}." >&2
fi

echo "Private artifacts: ${run_dir}"
if [[ -n "${public_json:-}" ]]; then
  echo "Allowlisted JSON: ${public_json}"
  echo "Allowlisted CSV:  ${public_csv}"
fi

if ((benchmark_status != 0)); then
  exit "${benchmark_status}"
fi
if ((log_status != 0)); then
  echo "tee failed while preserving runner.log." >&2
  exit "${log_status}"
fi
exit "${summary_status}"
