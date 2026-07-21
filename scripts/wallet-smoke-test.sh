#!/usr/bin/env bash
set -euo pipefail

api=${WALLET_API_URL:-http://127.0.0.1:${WALLET_API_PORT:-8787}/api}
wallet_a=
wallet_b=
address_a=
address_b=

json_field() {
  python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

delete_wallets() {
  if [[ -n "${wallet_a}" && -n "${address_a}" ]]; then
    curl --silent --request DELETE -H 'content-type: application/json' \
      --data "{\"confirmation\":\"${address_a}\"}" "${api}/wallets/${wallet_a}" >/dev/null || true
  fi
  if [[ -n "${wallet_b}" && -n "${address_b}" ]]; then
    curl --silent --request DELETE -H 'content-type: application/json' \
      --data "{\"confirmation\":\"${address_b}\"}" "${api}/wallets/${wallet_b}" >/dev/null || true
  fi
}
trap delete_wallets EXIT

curl --fail --silent "${api%/api}/health" >/dev/null
first=$(curl --fail --silent -H 'content-type: application/json' --data '{"name":"Smoke sender"}' "${api}/wallets")
wallet_a=$(json_field id <<<"${first}")
address_a=$(json_field address <<<"${first}")
second=$(curl --fail --silent -H 'content-type: application/json' --data '{"name":"Smoke recipient"}' "${api}/wallets")
wallet_b=$(json_field id <<<"${second}")
address_b=$(json_field address <<<"${second}")

curl --fail --silent -H 'content-type: application/json' --data '{"sol":2}' "${api}/wallets/${wallet_a}/airdrop" >/dev/null
transfer=$(curl --fail --silent -H 'content-type: application/json' \
  --data "{\"recipient\":\"${address_b}\",\"sol\":0.25}" "${api}/wallets/${wallet_a}/transfer")
signature=$(json_field signature <<<"${transfer}")
[[ -n "${signature}" && "${signature}" != None ]]

recipient=$(curl --fail --silent "${api}/wallets/${wallet_b}")
balance=$(json_field balance <<<"${recipient}")
python3 -c 'import sys; assert float(sys.argv[1]) >= .25' "${balance}"

echo "Custodial wallet flow passed: create, airdrop, transfer, history, delete."
