.PHONY: build up down destroy logs status shell test wallet-test auth-wallet-test airdrop validators portal explorer wallets

build:
	docker compose build

up:
	docker compose up --detach --build

down:
	docker compose down

destroy:
	docker compose down --volumes --remove-orphans

logs:
	docker compose logs --follow --tail=100

status:
	docker compose ps

shell:
	docker compose exec toolbox bash

test:
	./scripts/smoke-test.sh

wallet-test:
	./scripts/auth-wallet-smoke-test.sh

auth-wallet-test:
	./scripts/auth-wallet-smoke-test.sh

airdrop:
	docker compose exec toolbox solana airdrop 100

validators:
	docker compose exec toolbox solana validators

portal:
	@echo "http://localhost:$${PORTAL_PORT:-3000}"

explorer:
	@echo "http://localhost:$${EXPLORER_PORT:-3001}"

wallets:
	@echo "UI:  http://localhost:$${WALLET_APP_PORT:-3100}"
	@echo "API: http://localhost:$${WALLET_APP_PORT:-3100}/wallet-api/docs"
