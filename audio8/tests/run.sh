#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "${repo_root}"

python3 -m unittest -v audio8.tests.test_server
"${repo_root}/audio8/tests/test_static.sh"
"${repo_root}/scripts/install-audio8.sh" verify

echo "All offline Audio8 tests passed."
