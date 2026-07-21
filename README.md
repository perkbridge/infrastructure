# Dockerized Agave local cluster

A persistent, multi-node Solana development cluster running Agave **v4.1.2**:

- one bootstrap voting validator;
- two additional voting validators staked in genesis;
- one dedicated, non-voting RPC node;
- one Solana faucet;
- one always-on CLI toolbox;
- one browser-based localnet explorer;
- one PerkBridge application portal;
- one Keycloak identity provider with self-registration;
- one authenticated custodial wallet application and encrypted wallet volume; and
- named Docker volumes for keys, genesis, every ledger, and toolbox state.

This is a private development cluster, not a production validator deployment.

## Requirements

- Docker Engine 24+ with Docker Compose v2
- at least 8 GB assigned to the Docker VM (16 GB recommended) and 20 GB of free disk
- `curl` and `make` on the host (only needed for the convenience targets)

Agave v4.1.2 publishes a Linux x86-64 archive but no Linux ARM64 archive. Compose therefore pins `linux/amd64`; Docker Desktop on Apple Silicon runs it through emulation and will be slower than a native x86-64 host. The downloaded release archive is verified against its published SHA-256 digest during the image build.

## Start the cluster

```bash
cp .env.example .env
make up
docker compose ps
```

The first build downloads the client binaries from the exact `v4.1.2` Agave GitHub release. Because that release archive omits the server executables, the image compiles `agave-validator`, `solana-genesis`, and `solana-faucet` from the exact signed v4.1.2 commit with Agave's pinned Rust toolchain. Narrow localnet patches enable Agave's synchronous directory-removal fallback when Docker Desktop does not expose Linux `io_uring` and reduce production-sized RocksDB write buffers for the tiny local ledger; the reported Agave version remains v4.1.2. This initial build is CPU-intensive and can take a substantial amount of time under Apple Silicon emulation; Docker caches it for later builds. Compose starts the voting nodes sequentially to avoid overlapping initialization peaks, then starts the RPC node and toolbox.

Published endpoints:

| Service | Host endpoint |
| --- | --- |
| JSON-RPC | `http://localhost:8899` |
| WebSocket RPC | `ws://localhost:8900` |
| Faucet | `localhost:9900` |
| PerkBridge portal | `http://localhost:3000` |
| Explorer UI | `http://localhost:3001` |
| Wallet application | `http://localhost:3100` |
| Keycloak administration | `http://localhost:8080/auth` |
| Custodial wallet API | `http://127.0.0.1:8787` |

Ports can be changed in `.env` without changing container-to-container addressing.

## PerkBridge portal

Open `http://localhost:3000` after `make up`. This is the PerkBridge main entry page and lets you choose between the authenticated **Wallet** and read-only **Explorer** applications. Use the **Light mode / Dark mode** control to choose an appearance; the preference is retained and carried into the wallet. Run `make portal` to print its configured URL.

## Explorer UI

Open `http://localhost:3001`. The explorer proxies requests to the dedicated RPC node inside Docker and provides separate views for:

- live cluster health, slots, blocks, epoch progress, and validators;
- recent confirmed transactions with program and fee summaries;
- programs ranked by recent invocation activity;
- SPL Token and Token-2022 portfolios by owner address; and
- detailed block, transaction, account, and program inspection.

Run `make explorer` to print the configured URL. Set `EXPLORER_PORT` in `.env` to publish it on another host port.

## PerkBridge development wallet

Choose **Wallet** on `http://localhost:3000`, or open `http://localhost:3100` directly. New users select **Create an account**, register through Keycloak, and return to their automatically provisioned wallet. Existing users select **Sign in**.

Each Keycloak user receives an initial wallet tied to the immutable identity subject and can create up to ten encrypted custodial wallets. The API validates Keycloak's RSA-signed access token before every user operation; clients cannot select or operate another user's wallet. Users can:

- register, sign in, refresh their session, and sign out;
- create, select, and permanently delete additional wallets;
- inspect the balance and transaction history of each wallet separately;
- receive local SOL using a public address or unique `@username`;
- fund their wallet from the development faucet;
- send SOL to another registered user by `@username`;
- send SOL to any valid Solana address; and
- inspect balances and recent confirmed transactions.

The application uses the OAuth 2.0 authorization-code flow with PKCE. Keycloak runs at `http://localhost:8080/auth`; its development administrator defaults come from `.env` and must be changed for any shared environment.

Before creating wallets, set a unique development key in `.env`:

```dotenv
WALLET_MASTER_KEY=replace-with-a-long-random-local-development-secret
```

Do not change that value while wallets exist. The service derives an encryption key from it and a random salt stored in the persistent `custodial-wallets` volume. Changing or losing the value makes existing wallet keypairs unreadable.

Signing normally happens inside the `wallet-service` container. Swagger uses the public Keycloak client `wallet-swagger` and the OAuth password grant for local API development. It does not generate, store, or require a client secret.

Open `http://localhost:3100/wallet-api/docs`, select **Authorize**, and enter your registered Keycloak username and password. The client ID is preconfigured as `wallet-swagger`; no client secret field or value is needed. Swagger exchanges the credentials for a JWT and automatically sends that JWT as a bearer token with subsequent requests. This password flow is intentionally development-only; the wallet web application continues to use authorization code with PKCE.

```bash
# For a non-Swagger API client, use a token obtained from the same password flow.
export PERKBRIDGE_ACCESS_TOKEN='<access-token>'

curl --fail --silent \
  -H "authorization: Bearer ${PERKBRIDGE_ACCESS_TOKEN}" \
  http://localhost:3100/wallet-api/api/app/wallets

curl --fail --silent \
  -H "authorization: Bearer ${PERKBRIDGE_ACCESS_TOKEN}" \
  http://localhost:3100/wallet-api/api/app/wallets/<WALLET_ID>/keys

# Fund any valid localnet address; a custodial wallet UUID is not required.
curl --fail --silent \
  -H "authorization: Bearer ${PERKBRIDGE_ACCESS_TOKEN}" \
  -H 'content-type: application/json' \
  --data '{"address":"<SOLANA_ADDRESS>","sol":10}' \
  http://localhost:3100/wallet-api/api/app/airdrop
```

The `/keys` endpoint returns the address/public key and owner information. For custom program transactions, serialize the Solana transaction **message** with your SDK and submit its standard-Base64 representation:

```bash
# Sign only; attach signatureBase64 to the transaction with your Solana SDK.
curl --fail --silent \
  -H "authorization: Bearer ${PERKBRIDGE_ACCESS_TOKEN}" \
  -H 'content-type: application/json' \
  --data '{"messageBase64":"<SERIALIZED_TRANSACTION_MESSAGE>"}' \
  http://localhost:3100/wallet-api/api/app/wallets/<WALLET_ID>/sign

# Or sign and submit a transaction requiring exactly one signature.
curl --fail --silent \
  -H "authorization: Bearer ${PERKBRIDGE_ACCESS_TOKEN}" \
  -H 'content-type: application/json' \
  --data '{"messageBase64":"<SERIALIZED_TRANSACTION_MESSAGE>"}' \
  http://localhost:3100/wallet-api/api/app/wallets/<WALLET_ID>/sign-and-send
```

The selected wallet must be the transaction fee payer and first signer. Both legacy and version-0 Solana messages are supported. Multi-signer messages can use `/sign` and combine the returned signature with other signatures client-side; `/sign-and-send` intentionally accepts only single-signer transactions.

Private-key export is a separate development-only operation. It requires OAuth, wallet ownership, `WALLET_PRIVATE_KEY_EXPORT_ENABLED=true`, and the exact public address in the POST body. Avoid using it for ordinary transactions:

```bash
curl --fail --silent \
  -H "authorization: Bearer ${PERKBRIDGE_ACCESS_TOKEN}" \
  -H 'content-type: application/json' \
  --data '{"confirmation":"<FULL_WALLET_ADDRESS>"}' \
  http://localhost:3100/wallet-api/api/app/wallets/<WALLET_ID>/export
```

The former unauthenticated `/api/wallets` interface is disabled by default. It can only be restored explicitly with `WALLET_LEGACY_API_ENABLED=true`; do not enable it on a shared machine.

Run the OAuth-secured end-to-end check with `make wallet-test` or `make auth-wallet-test`.

The check creates two temporary Keycloak users, verifies that the API rejects unauthenticated access, provisions both wallets, transfers by `@username`, and removes the temporary users and wallets.

> **Not production-ready:** this service is intentionally scoped to an isolated localnet. Keycloak and its embedded H2 database run in development mode, while custody has no HSM/KMS integration, transaction approval policy, immutable audit log, rate limiting, fraud controls, backup/rotation system, or production key-recovery design. Never point it at devnet, testnet, or mainnet and never store real funds in it.

## Use the CLI toolbox

The toolbox is preconfigured to use the dedicated RPC node and its persistent keypair:

```bash
make shell
solana cluster-version
solana validators
solana address
solana airdrop 100
solana balance
```

For single commands from the host:

```bash
make validators
make airdrop
```

## Connect with a host Solana CLI

You can use a Solana CLI installed directly on the host; the toolbox container is optional. Agave/Solana CLI **v4.1.2** is recommended so client and cluster behavior match:

```bash
solana --version
solana-keygen --version
```

Create a separate configuration profile and deployer keypair. This avoids changing a CLI profile that you may use for devnet or mainnet:

```bash
mkdir -p ~/.config/solana
solana-keygen new \
  --no-bip39-passphrase \
  --outfile ~/.config/solana/localnet-deployer.json

solana config set \
  --config ~/.config/solana/localnet.yml \
  --url http://127.0.0.1:8899 \
  --ws ws://127.0.0.1:8900 \
  --keypair ~/.config/solana/localnet-deployer.json \
  --commitment confirmed
```

If the deployer keypair already exists, do not regenerate it. Confirm the profile and fund the account:

```bash
solana --config ~/.config/solana/localnet.yml config get
solana --config ~/.config/solana/localnet.yml cluster-version
solana --config ~/.config/solana/localnet.yml slot
solana --config ~/.config/solana/localnet.yml address
solana --config ~/.config/solana/localnet.yml airdrop 100
solana --config ~/.config/solana/localnet.yml balance
```

If you intentionally want this localnet to become the default CLI target, omit `--config ~/.config/solana/localnet.yml` and run `solana config set` against the default profile instead.

## Build and deploy a Solana program

These steps deploy an existing Rust Solana program from the host. The host needs Rust and the v4.1.2 Solana toolchain, including `cargo-build-sbf`:

```bash
rustc --version
cargo --version
cargo-build-sbf --version
```

### 1. Start and verify the cluster

```bash
make up
make test
```

Do not deploy until `make test` reports `Local Agave cluster is healthy.`

### 2. Select and fund the deployer

```bash
solana --config ~/.config/solana/localnet.yml address
solana --config ~/.config/solana/localnet.yml airdrop 1000
solana --config ~/.config/solana/localnet.yml balance
```

Program deployment creates and funds program-data and temporary buffer accounts, so it requires substantially more SOL than an ordinary transaction.

### 3. Build the SBF program

From the Rust program workspace containing its `Cargo.toml`:

```bash
cd /path/to/your/program
cargo build-sbf
```

The compiler prints the artifact paths. A typical workspace produces:

```text
target/deploy/my_program.so
target/deploy/my_program-keypair.json
```

Keep `target/deploy/my_program-keypair.json` safe and persistent. It determines the program ID and is required for the initial deployment. Do not generate a different keypair when upgrading the same program.

If the build did not create a program keypair, create one explicitly:

```bash
solana-keygen new \
  --no-bip39-passphrase \
  --outfile target/deploy/my_program-keypair.json
```

Print the program ID:

```bash
solana-keygen pubkey target/deploy/my_program-keypair.json
```

The program ID declared in the Rust source must match this address. For an Anchor program, `anchor keys sync` updates `declare_id!` and `Anchor.toml` from the generated keypair.

### 4. Deploy through the dedicated RPC node

Replace the artifact names with those produced by your build:

```bash
solana --config ~/.config/solana/localnet.yml program deploy \
  --use-rpc \
  --program-id target/deploy/my_program-keypair.json \
  target/deploy/my_program.so
```

`--use-rpc` is important for this Docker topology. The validator TPU ports are private to the Compose network, while JSON-RPC is published on the host.

A successful deployment prints output similar to:

```text
Program Id: <PROGRAM_ID>
```

### 5. Verify the deployment

```bash
solana --config ~/.config/solana/localnet.yml program show <PROGRAM_ID>
solana --config ~/.config/solana/localnet.yml account <PROGRAM_ID>
```

Open `http://localhost:3001`, search for the program ID, and use the **Programs** and **Transactions** views. A program appears in the activity ranking after a transaction invokes it.

### 6. Invoke and debug the program

Deployment only creates the executable account; it does not invoke an instruction. Run your program's client or integration test against `http://127.0.0.1:8899`, then inspect live program logs:

```bash
solana --config ~/.config/solana/localnet.yml logs <PROGRAM_ID>
```

In another terminal, run the client transaction. The resulting signature can be searched directly in the explorer.

### 7. Upgrade the program

Rebuild after changing the source, then deploy the new `.so` with the same program keypair:

```bash
cargo build-sbf

solana --config ~/.config/solana/localnet.yml program deploy \
  --use-rpc \
  --program-id target/deploy/my_program-keypair.json \
  target/deploy/my_program.so
```

The configured deployer remains the upgrade authority unless another authority was specified. Check it with:

```bash
solana --config ~/.config/solana/localnet.yml program show <PROGRAM_ID>
```

### Anchor workflow

Configure the Anchor project to use the already-running cluster rather than starting `solana-test-validator`:

```toml
[provider]
cluster = "Localnet"
wallet = "~/.config/solana/localnet-deployer.json"

[programs.localnet]
my_program = "<PROGRAM_ID>"
```

Build and synchronize the declared program ID:

```bash
anchor build
anchor keys sync
```

Deploy the generated artifact through the published RPC endpoint:

```bash
solana --config ~/.config/solana/localnet.yml program deploy \
  --use-rpc \
  --program-id target/deploy/my_program-keypair.json \
  target/deploy/my_program.so
```

Run Anchor tests without creating another validator or redeploying:

```bash
anchor test --skip-local-validator --skip-deploy
```

### Deployment troubleshooting

- **Connection refused:** confirm `docker compose ps rpc` is healthy and port `8899` is published.
- **Insufficient funds:** request another airdrop and check the deployer's balance.
- **Program ID mismatch:** run `anchor keys sync` or update `declare_id!` to match the program keypair.
- **TPU or private-address errors:** retain `--use-rpc` on `solana program deploy`.
- **Blockhash expired:** confirm slots are advancing, then retry the deployment.
- **Wrong cluster:** always inspect `solana --config ~/.config/solana/localnet.yml config get` before deploying.

## Verify and operate

```bash
make test                 # RPC health, version pin, and validator set
make logs                 # follow all service logs
docker compose logs rpc   # follow one service
make down                 # stop while retaining all volumes
make up                   # restart the same cluster and ledgers
```

`make destroy` permanently removes the cluster's named volumes, including every identity key, genesis, stake allocation, and ledger. The next `make up` creates a completely new cluster.

It also deletes every custodial wallet key in the `custodial-wallets` volume. This cannot be recovered unless that Docker volume was backed up separately.

The `keycloak-data` volume contains registered users, password credentials, roles, and sessions. `make destroy` permanently deletes that identity data as well.

## Persistence and topology

The `genesis` one-shot container generates keypairs only when they do not already exist. It writes the genesis ledger only once. All voting accounts and stakes therefore survive ordinary `down`/`up` cycles.

Each validator has an isolated ledger volume. `cluster-config` contains validator and faucet keypairs; treat its contents as secrets even though this cluster is intended for local development. The voting nodes expose RPC only inside the Docker network for health checks. Host applications use the dedicated non-voting `rpc` service.

## Configuration

`.env` supports:

- `RPC_PORT` and `RPC_WEBSOCKET_PORT`
- `FAUCET_PORT`
- `PORTAL_PORT`
- `EXPLORER_PORT`
- `WALLET_APP_PORT`
- `KEYCLOAK_PORT`
- `KEYCLOAK_ADMIN` and `KEYCLOAK_ADMIN_PASSWORD`
- `WALLET_API_PORT`
- `WALLET_MASTER_KEY`
- `RUST_LOG`

The Agave version and release digest are deliberately pinned in the Dockerfile and Compose file. Upgrading requires changing both values. Destroy the volumes before changing protocol versions unless you have planned and tested an in-place upgrade.

## Resource notes

Real Agave validators are resource-intensive. This configuration disables host tuning and PoH speed checks, limits worker pools and cache sizes, and starts validators sequentially. Four real Agave processes still require at least 8 GB assigned to Docker Desktop; a 4 GB VM will OOM-kill and restart nodes. The ledger cap is deliberately finite, but the volumes still grow over time.
