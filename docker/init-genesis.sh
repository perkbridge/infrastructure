#!/usr/bin/env bash
set -euo pipefail

readonly CONFIG_DIR=/config
readonly GENESIS_DIR=/genesis
readonly LAMPORTS_PER_SOL=1000000000

mkdir -p "${CONFIG_DIR}" "${GENESIS_DIR}"

new_keypair() {
  local output=$1
  if [[ ! -s "${output}" ]]; then
    solana-keygen new --no-bip39-passphrase --silent --force --outfile "${output}"
  fi
}

for name in \
  faucet toolbox rpc-identity \
  bootstrap-identity bootstrap-vote bootstrap-stake \
  validator-1-identity validator-1-vote validator-1-stake \
  validator-2-identity validator-2-vote validator-2-stake; do
  new_keypair "${CONFIG_DIR}/${name}.json"
done

if [[ -s "${GENESIS_DIR}/genesis.bin" ]]; then
  echo "Existing genesis found; preserving cluster identity and stake allocation."
  exit 0
fi

pubkey() {
  solana-keygen pubkey "${CONFIG_DIR}/$1.json"
}

solana-genesis \
  --ledger "${GENESIS_DIR}" \
  --cluster-type development \
  --faucet-pubkey "$(pubkey faucet)" \
  --faucet-lamports "$((500000000 * LAMPORTS_PER_SOL))" \
  --bootstrap-validator "$(pubkey bootstrap-identity)" "$(pubkey bootstrap-vote)" "$(pubkey bootstrap-stake)" \
  --bootstrap-validator "$(pubkey validator-1-identity)" "$(pubkey validator-1-vote)" "$(pubkey validator-1-stake)" \
  --bootstrap-validator "$(pubkey validator-2-identity)" "$(pubkey validator-2-vote)" "$(pubkey validator-2-stake)" \
  --target-lamports-per-signature 0

echo "Genesis created with three genesis-staked voting validators."
