#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
  init-genesis)
    exec /usr/local/lib/agave-localnet/init-genesis.sh
    ;;
  validator)
    shift
    exec /usr/local/lib/agave-localnet/run-validator.sh "$@"
    ;;
  faucet)
    exec solana-faucet --keypair /config/faucet.json
    ;;
  toolbox)
    mkdir -p /root/.config/solana/cli
    solana config set \
      --url http://rpc:8899 \
      --keypair /config/toolbox.json >/dev/null
    echo "Agave toolbox ready. Attach with: docker compose exec toolbox bash"
    exec sleep infinity
    ;;
  *)
    exec "$@"
    ;;
esac
