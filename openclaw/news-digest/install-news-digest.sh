#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SOURCE_DIR="${SCRIPT_DIR}"
DEST_DIR="${HOME}/.openclaw/workspace/news-digest"
PYTHON_BIN="${PYTHON_BIN:-python3}"

usage() {
  echo "Usage: $0 [--source DIR] [--dest DIR] [--python PATH]" >&2
}

while (($#)); do
  case "$1" in
    --source)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      SOURCE_DIR="$2"
      shift 2
      ;;
    --dest)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      DEST_DIR="$2"
      shift 2
      ;;
    --python)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      PYTHON_BIN="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

SOURCE_DIR="$(cd -- "${SOURCE_DIR}" && pwd -P)"
if [[ -e "${DEST_DIR}" && ! -d "${DEST_DIR}" ]]; then
  echo "Destination exists and is not a directory." >&2
  exit 2
fi
mkdir -p -- "${DEST_DIR}"
DEST_DIR="$(cd -- "${DEST_DIR}" && pwd -P)"

if [[ "${SOURCE_DIR}" == "${DEST_DIR}" ]]; then
  echo "Source and destination must be different directories." >&2
  exit 2
fi

for secret_name in .x_creds.json .x_request_secret.txt; do
  secret_path="${DEST_DIR}/${secret_name}"
  if [[ -L "${secret_path}" ]] \
    || [[ -e "${secret_path}" && ! -f "${secret_path}" ]]; then
    echo "Refusing unsafe credential path: ${secret_name}" >&2
    exit 2
  fi
done
if [[ -L "${DEST_DIR}/.backups" ]]; then
  echo "Refusing a symlinked backup directory." >&2
  exit 2
fi

required_files=(
  news_digest.py
  news_briefing.py
  news-digest.py
  digest-poster.py
)
optional_files=(
  README.md
)

for relative in "${required_files[@]}"; do
  if [[ ! -f "${SOURCE_DIR}/${relative}" || -L "${SOURCE_DIR}/${relative}" ]]; then
    echo "Missing or unsafe source file: ${relative}" >&2
    exit 2
  fi
done

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "Python interpreter not found." >&2
  exit 2
fi
if ! "${PYTHON_BIN}" -c \
  'import sys; raise SystemExit(0 if (3, 9) <= sys.version_info[:2] else 1)'
then
  echo "Python 3.9 or newer is required." >&2
  exit 2
fi

stage_dir="$(mktemp -d "${TMPDIR:-/tmp}/news-digest-install.XXXXXXXX")"
cleanup() {
  rm -rf -- "${stage_dir}"
}
trap cleanup EXIT
chmod 0700 "${stage_dir}"

for relative in "${required_files[@]}"; do
  install -m 0600 -- "${SOURCE_DIR}/${relative}" "${stage_dir}/${relative}"
done
for relative in "${optional_files[@]}"; do
  if [[ -f "${SOURCE_DIR}/${relative}" && ! -L "${SOURCE_DIR}/${relative}" ]]; then
    install -m 0600 -- "${SOURCE_DIR}/${relative}" "${stage_dir}/${relative}"
  fi
done

PYTHONPYCACHEPREFIX="${stage_dir}/pycache" \
  "${PYTHON_BIN}" -m py_compile \
  "${stage_dir}/news_digest.py" \
  "${stage_dir}/news_briefing.py" \
  "${stage_dir}/news-digest.py" \
  "${stage_dir}/digest-poster.py"
PYTHONPATH="${stage_dir}" PYTHONPYCACHEPREFIX="${stage_dir}/pycache" \
  "${PYTHON_BIN}" "${stage_dir}/digest-poster.py" --help >/dev/null

timestamp="$(date -u '+%Y%m%dT%H%M%SZ')"
backup_dir="${DEST_DIR}/.backups/${timestamp}-$$"
backup_created=0
for relative in "${required_files[@]}" "${optional_files[@]}"; do
  if [[ -f "${DEST_DIR}/${relative}" && ! -L "${DEST_DIR}/${relative}" ]]; then
    if ((backup_created == 0)); then
      install -d -m 0700 -- "${backup_dir}"
      backup_created=1
    fi
    install -m 0600 -- "${DEST_DIR}/${relative}" "${backup_dir}/${relative}"
  elif [[ -L "${DEST_DIR}/${relative}" ]]; then
    echo "Refusing to replace destination symlink: ${relative}" >&2
    exit 2
  fi
done

install -d -m 0700 -- "${DEST_DIR}"
install -m 0644 -- "${stage_dir}/news_digest.py" "${DEST_DIR}/news_digest.py"
install -m 0644 -- "${stage_dir}/news_briefing.py" "${DEST_DIR}/news_briefing.py"
install -m 0755 -- "${stage_dir}/news-digest.py" "${DEST_DIR}/news-digest.py"
install -m 0755 -- "${stage_dir}/digest-poster.py" "${DEST_DIR}/digest-poster.py"
if [[ -f "${stage_dir}/README.md" ]]; then
  install -m 0644 -- "${stage_dir}/README.md" "${DEST_DIR}/README.md"
fi

for secret_name in .x_creds.json .x_request_secret.txt; do
  secret_path="${DEST_DIR}/${secret_name}"
  if [[ -e "${secret_path}" ]]; then
    chmod 0600 "${secret_path}"
  fi
done

echo "Installed news digest scripts in ${DEST_DIR}"
if ((backup_created == 1)); then
  echo "Previous scripts backed up in ${backup_dir}"
fi
