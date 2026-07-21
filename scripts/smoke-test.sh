#!/usr/bin/env bash
set -euo pipefail

rpc_url=${RPC_URL:-http://localhost:${RPC_PORT:-8899}}

echo "Checking RPC at ${rpc_url}"
deadline=$((SECONDS + ${SMOKE_TIMEOUT_SECONDS:-180}))

while true; do
  version=$(curl --fail --silent --show-error \
    -H 'content-type: application/json' \
    --data '{"jsonrpc":"2.0","id":1,"method":"getVersion"}' \
    "${rpc_url}" 2>/dev/null || true)
  [[ -n "${version}" ]] && break
  if (( SECONDS >= deadline )); then
    echo "RPC did not become reachable before the timeout" >&2
    exit 1
  fi
  sleep 2
done

if [[ "${version}" != *'4.1.2'* ]]; then
  echo "Unexpected RPC version response: ${version}" >&2
  exit 1
fi

while true; do
  health=$(curl --fail --silent --show-error \
    -H 'content-type: application/json' \
    --data '{"jsonrpc":"2.0","id":1,"method":"getHealth"}' \
    "${rpc_url}" 2>/dev/null || true)
  [[ "${health}" == *'"ok"'* ]] && break
  if (( SECONDS >= deadline )); then
    echo "RPC did not become healthy before the timeout: ${health}" >&2
    exit 1
  fi
  sleep 2
done

vote_accounts=$(curl --fail --silent --show-error \
  -H 'content-type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"getVoteAccounts"}' \
  "${rpc_url}")
vote_count=$(grep -o '"votePubkey"' <<<"${vote_accounts}" | wc -l | tr -d ' ')
if [[ "${vote_count}" != 3 ]]; then
  echo "Expected 3 vote accounts, found ${vote_count}: ${vote_accounts}" >&2
  exit 1
fi

docker compose exec -T toolbox solana validators
docker compose exec -T toolbox solana airdrop 1
echo "Local Agave cluster is healthy."
