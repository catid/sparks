#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
export MIA_ENV_FILE="${MIA_ENV_FILE:-mia-agent.env}"
# shellcheck disable=SC1091
source "${root}/bin/common.sh"

complete_catalog='{
  "data": [
    {"id": "deepseek-v4-flash-dspark-mia-throughput"},
    {"id": "deepseek-v4-flash"}
  ]
}'
[[ -z "$(missing_served_model_ids "${complete_catalog}")" ]]

primary_only='{
  "data": [
    {"id": "deepseek-v4-flash-dspark-mia-throughput"}
  ]
}'
[[ "$(missing_served_model_ids "${primary_only}")" == "deepseek-v4-flash" ]]

empty_catalog='{"data":[]}'
expected="$(
  printf '%s\n' \
    deepseek-v4-flash-dspark-mia-throughput \
    deepseek-v4-flash
)"
[[ "$(missing_served_model_ids "${empty_catalog}")" == "${expected}" ]]

echo "Model-catalog test passed: primary and canonical alias are both health requirements."
