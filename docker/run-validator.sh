#!/usr/bin/env bash
set -euo pipefail

readonly ROLE=${1:?validator role is required}
readonly CONFIG_DIR=/config
readonly LEDGER_DIR=/ledger
readonly GENESIS_DIR=/genesis

case "${ROLE}" in
  bootstrap)
    identity=bootstrap-identity
    vote=bootstrap-vote
    entrypoint=()
    voting=(
      --vote-account "$(solana-keygen pubkey "${CONFIG_DIR}/${vote}.json")"
      --no-wait-for-vote-to-start-leader
    )
    rpc_options=()
    ;;
  validator-1|validator-2)
    identity=${ROLE}-identity
    vote=${ROLE}-vote
    entrypoint=(--entrypoint bootstrap-validator:8001)
    voting=(--vote-account "$(solana-keygen pubkey "${CONFIG_DIR}/${vote}.json")")
    rpc_options=()
    ;;
  rpc)
    identity=rpc-identity
    entrypoint=(--entrypoint bootstrap-validator:8001)
    voting=(--no-voting)
    rpc_options=(
      --full-rpc-api
      --enable-rpc-transaction-history
      --rpc-faucet-address faucet:9900
    )
    ;;
  *)
    echo "Unknown validator role: ${ROLE}" >&2
    exit 64
    ;;
esac

mkdir -p "${LEDGER_DIR}"
if [[ ! -f "${LEDGER_DIR}/.genesis-copied" ]]; then
  # solana-genesis writes slot-0 shreds into RocksDB as well as genesis.bin.
  # Copying only genesis.bin leaves Agave unable to replay bank 0. Remove only
  # known partial-startup artifacts before seeding this ledger for the first
  # time; subsequent restarts preserve the complete validator ledger.
  rm -rf \
    "${LEDGER_DIR}/accounts" \
    "${LEDGER_DIR}/admin.rpc" \
    "${LEDGER_DIR}/bank_snapshots" \
    "${LEDGER_DIR}/genesis.bin" \
    "${LEDGER_DIR}/genesis.tar.bz2" \
    "${LEDGER_DIR}/rocksdb" \
    "${LEDGER_DIR}/rocksdb_fifo_shred_storage" \
    "${LEDGER_DIR}/snapshot"
  cp -a "${GENESIS_DIR}/." "${LEDGER_DIR}/"
  touch "${LEDGER_DIR}/.genesis-copied"
fi

# Binding gossip to the container's routable address also makes Agave advertise
# that address. Binding gossip to 0.0.0.0 would make the bootstrap node
# unreachable because it has no entrypoint from which to discover its address.
read -r node_ip _ < <(hostname -i)

exec agave-validator \
  --log - \
  --identity "${CONFIG_DIR}/${identity}.json" \
  "${voting[@]}" \
  --ledger "${LEDGER_DIR}" \
  "${entrypoint[@]}" \
  --allow-private-addr \
  --bind-address "${node_ip}" \
  --gossip-port 8001 \
  --dynamic-port-range 8002-8031 \
  --rpc-bind-address "${node_ip}" \
  --rpc-port 8899 \
  "${rpc_options[@]}" \
  --accounts-db-cache-limit-mb 64 \
  --accounts-db-background-threads 1 \
  --accounts-db-foreground-threads 1 \
  --accounts-index-flush-threads 1 \
  --block-production-num-workers 1 \
  --ip-echo-server-threads 1 \
  --rayon-global-threads 1 \
  --replay-forks-threads 1 \
  --replay-transactions-threads 1 \
  --tpu-sigverify-threads 1 \
  --tpu-transaction-forward-receive-threads 1 \
  --tpu-transaction-receive-threads 1 \
  --tpu-vote-transaction-receive-threads 1 \
  --tvu-receive-threads 1 \
  --tvu-retransmit-threads 1 \
  --tvu-shred-sigverify-threads 1 \
  --tvu-bls-shred-sigverify-threads 1 \
  --no-poh-speed-test \
  --no-os-network-limits-test \
  --no-snapshot-fetch \
  --limit-ledger-size 50000000
