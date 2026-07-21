#!/usr/bin/env bash
set -euo pipefail

kcadm=/opt/keycloak/bin/kcadm.sh
config=/tmp/perkbridge-kcadm.config
server=http://keycloak:8080/auth
admin_user=${KC_BOOTSTRAP_ADMIN_USERNAME:-admin}
admin_password=${KC_BOOTSTRAP_ADMIN_PASSWORD:-local-development-admin}
wallet_port=${WALLET_APP_PORT:-3100}

for _ in {1..90}; do
  if "${kcadm}" config credentials --config "${config}" --server "${server}" --realm master --user "${admin_user}" --password "${admin_password}" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

if ! "${kcadm}" get realms/local-wallet --config "${config}" >/dev/null 2>&1; then
  echo "Keycloak local-wallet realm did not become ready" >&2
  exit 1
fi

client_uuid=$("${kcadm}" get clients --config "${config}" -r local-wallet -q clientId=wallet-swagger --fields id \
  | sed -n 's/^[[:space:]]*"id" : "\([^"]*\)".*/\1/p' | head -n 1)

if [[ -z "${client_uuid}" ]]; then
  client_uuid=$("${kcadm}" create clients --config "${config}" -r local-wallet -i \
    -s clientId=wallet-swagger \
    -s 'name=PerkBridge Swagger API' \
    -s enabled=true \
    -s publicClient=true \
    -s bearerOnly=false \
    -s standardFlowEnabled=true \
    -s implicitFlowEnabled=false \
    -s directAccessGrantsEnabled=true \
    -s serviceAccountsEnabled=false \
    -s protocol=openid-connect)
fi

"${kcadm}" update "clients/${client_uuid}" --config "${config}" -r local-wallet \
  -s publicClient=true \
  -s bearerOnly=false \
  -s standardFlowEnabled=true \
  -s directAccessGrantsEnabled=true \
  -s serviceAccountsEnabled=false \
  -s "rootUrl=http://localhost:${wallet_port}" \
  -s "baseUrl=http://localhost:${wallet_port}/wallet-api/docs" \
  -s "redirectUris=[\"http://localhost:${wallet_port}/*\",\"http://127.0.0.1:${wallet_port}/*\"]" \
  -s "webOrigins=[\"http://localhost:${wallet_port}\",\"http://127.0.0.1:${wallet_port}\"]"

mappers=$("${kcadm}" get "clients/${client_uuid}/protocol-mappers/models" --config "${config}" -r local-wallet)
if ! grep -q '"name" : "wallet-api-audience"' <<<"${mappers}"; then
  "${kcadm}" create "clients/${client_uuid}/protocol-mappers/models" --config "${config}" -r local-wallet \
    -s name=wallet-api-audience \
    -s protocol=openid-connect \
    -s protocolMapper=oidc-audience-mapper \
    -s consentRequired=false \
    -s 'config."included.client.audience"=wallet-api' \
    -s 'config."id.token.claim"=false' \
    -s 'config."access.token.claim"=true' \
    -s 'config."lightweight.claim"=false' >/dev/null
fi

echo "PerkBridge Swagger OAuth client is ready."
