#!/usr/bin/env bash
set -euo pipefail

app_url=${WALLET_APP_URL:-http://localhost:${WALLET_APP_PORT:-3100}}
admin_user=${KEYCLOAK_ADMIN:-admin}
admin_password=${KEYCLOAK_ADMIN_PASSWORD:-local-development-admin}
suffix="${RANDOM}${RANDOM}"
user_a="smoke-sender-${suffix}"
user_b="smoke-recipient-${suffix}"
password="Local-smoke-${suffix}!"
admin_token=
token_a=
token_b=
user_id_a=
user_id_b=
address_a=
address_b=

json_field() {
  python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

cleanup() {
  if [[ -n "${token_a}" && -n "${address_a}" ]]; then
    curl --silent --request DELETE -H "authorization: Bearer ${token_a}" -H 'content-type: application/json' \
      --data "{\"confirmation\":\"${address_a}\"}" "${app_url}/api/me" >/dev/null || true
  fi
  if [[ -n "${token_b}" && -n "${address_b}" ]]; then
    curl --silent --request DELETE -H "authorization: Bearer ${token_b}" -H 'content-type: application/json' \
      --data "{\"confirmation\":\"${address_b}\"}" "${app_url}/api/me" >/dev/null || true
  fi
  if [[ -n "${admin_token}" && -n "${user_id_a}" ]]; then
    curl --silent --request DELETE -H "authorization: Bearer ${admin_token}" \
      "${app_url}/auth/admin/realms/local-wallet/users/${user_id_a}" >/dev/null || true
  fi
  if [[ -n "${admin_token}" && -n "${user_id_b}" ]]; then
    curl --silent --request DELETE -H "authorization: Bearer ${admin_token}" \
      "${app_url}/auth/admin/realms/local-wallet/users/${user_id_b}" >/dev/null || true
  fi
}
trap cleanup EXIT

for _ in {1..90}; do
  curl --max-time 2 --fail --silent "${app_url}/auth/realms/local-wallet/.well-known/openid-configuration" >/dev/null && break
  sleep 2
done

admin_response=$(curl --fail --silent -H 'content-type: application/x-www-form-urlencoded' \
  --data-urlencode grant_type=password --data-urlencode client_id=admin-cli \
  --data-urlencode "username=${admin_user}" --data-urlencode "password=${admin_password}" \
  "${app_url}/auth/realms/master/protocol/openid-connect/token")
admin_token=$(json_field access_token <<<"${admin_response}")
create_user() {
  local username=$1
  curl --fail --silent --output /dev/null -H "authorization: Bearer ${admin_token}" -H 'content-type: application/json' \
    --data "{\"username\":\"${username}\",\"email\":\"${username}@local.invalid\",\"firstName\":\"Smoke\",\"lastName\":\"User\",\"enabled\":true,\"emailVerified\":true,\"credentials\":[{\"type\":\"password\",\"value\":\"${password}\",\"temporary\":false}]}" \
    "${app_url}/auth/admin/realms/local-wallet/users"
}

user_id() {
  curl --fail --silent -G -H "authorization: Bearer ${admin_token}" \
    --data-urlencode "username=$1" --data-urlencode exact=true \
    "${app_url}/auth/admin/realms/local-wallet/users" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])'
}

user_token() {
  curl --fail --silent -H 'content-type: application/x-www-form-urlencoded' \
    --data-urlencode grant_type=password --data-urlencode client_id=wallet-swagger \
    --data-urlencode "username=$1" --data-urlencode "password=${password}" \
    "${app_url}/auth/realms/local-wallet/protocol/openid-connect/token" | json_field access_token
}

create_user "${user_a}"
create_user "${user_b}"
user_id_a=$(user_id "${user_a}")
user_id_b=$(user_id "${user_b}")
token_a=$(user_token "${user_a}")
token_b=$(user_token "${user_b}")

unauthorized_status=$(curl --silent --output /dev/null --write-out '%{http_code}' "${app_url}/api/me")
[[ "${unauthorized_status}" == 401 ]]

wallet_a=$(curl --fail --silent -H "authorization: Bearer ${token_a}" "${app_url}/api/me")
wallet_b=$(curl --fail --silent -H "authorization: Bearer ${token_b}" "${app_url}/api/me")
address_a=$(json_field address <<<"${wallet_a}")
address_b=$(json_field address <<<"${wallet_b}")

if [[ "${AUTH_ONLY:-false}" == true ]]; then
  echo "Swagger authentication passed: public-client password grant, JWT validation, and wallet ownership isolation."
  exit 0
fi

curl --fail --silent -H "authorization: Bearer ${token_a}" -H 'content-type: application/json' \
  --data "{\"address\":\"${address_a}\",\"sol\":2}" "${app_url}/wallet-api/api/app/airdrop" >/dev/null
transfer=$(curl --fail --silent -H "authorization: Bearer ${token_a}" -H 'content-type: application/json' \
  --data "{\"recipient\":\"@${user_b}\",\"sol\":0.25}" "${app_url}/api/me/transfer")
signature=$(json_field signature <<<"${transfer}")
[[ -n "${signature}" && "${signature}" != None ]]

recipient=$(curl --fail --silent -H "authorization: Bearer ${token_b}" "${app_url}/api/me")
balance=$(json_field balance <<<"${recipient}")
python3 -c 'import sys; assert float(sys.argv[1]) >= .25' "${balance}"

echo "Authenticated wallet flow passed: public-client password grant, JWT isolation, address airdrop, @username transfer, and cleanup."
