FROM quay.io/keycloak/keycloak:26.7.0

COPY realm.json /opt/keycloak/data/import/local-wallet-realm.json
COPY themes/local-wallet /opt/keycloak/themes/local-wallet
COPY provision-swagger-client.sh /opt/keycloak/bin/provision-swagger-client.sh

USER root
RUN chmod 0755 /opt/keycloak/bin/provision-swagger-client.sh
