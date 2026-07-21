# syntax=docker/dockerfile:1.7
ARG AGAVE_VERSION=v4.1.2
ARG AGAVE_COMMIT=182084b82aae88b1b0731540f63bf7416f27773a

# The v4.1.2 release archive contains client tools but omits the validator,
# genesis, and faucet executables. Build those from the exact signed tag.
FROM rust:1.95-bookworm AS server-builder

ARG AGAVE_VERSION
ARG AGAVE_COMMIT

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
      ca-certificates clang cmake curl libclang-dev libssl-dev libudev-dev \
      pkg-config protobuf-compiler zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    curl --fail --location --retry 5 \
      "https://github.com/anza-xyz/agave/archive/${AGAVE_COMMIT}.tar.gz" \
      --output /tmp/agave-source.tar.gz; \
    mkdir /usr/src/agave; \
    tar --extract --gzip --file /tmp/agave-source.tar.gz \
      --directory /usr/src/agave --strip-components=1; \
    rm /tmp/agave-source.tar.gz

WORKDIR /usr/src/agave
RUN cargo build --release --locked \
      --package agave-validator \
      --package solana-genesis \
      --package solana-faucet-cli \
    && ./target/release/agave-validator --version | grep -F "${AGAVE_VERSION#v}" \
    && ./target/release/solana-genesis --version | grep -F "${AGAVE_VERSION#v}" \
    && ./target/release/solana-faucet --version | grep -F "${AGAVE_VERSION#v}"

# Agave v4.1.2 unconditionally asserts io_uring support on Linux in its
# directory-removal helper. Docker Desktop's Linux VM can return ENOSYS, so
# retain Agave's existing synchronous fallback instead of panicking. Its
# production RocksDB write buffers are also excessive for a tiny local ledger;
# reduce them from 256 MiB to 8 MiB so four nodes fit in Docker Desktop.
FROM server-builder AS portable-server-builder
RUN sed -i '0,/        assert!(io_uring_supported());/s//        if !io_uring_supported() {\n            return fs::remove_dir_all(path);\n        }/' fs/src/dirs.rs \
    && sed -i '0,/        assert!(io_uring_supported());/s//        if !io_uring_supported() {\n            remove_dir_contents_slow(path);\n            return;\n        }/' fs/src/dirs.rs \
    && sed -i 's/const MAX_WRITE_BUFFER_SIZE: u64 = 256 \* 1024 \* 1024;/const MAX_WRITE_BUFFER_SIZE: u64 = 8 * 1024 * 1024;/' ledger/src/blockstore_db.rs \
    && cargo build --release --locked \
      --package agave-validator \
      --package solana-genesis \
      --package solana-faucet-cli

FROM debian:bookworm-slim AS agave-runtime

ARG AGAVE_VERSION
ARG TARGETARCH
ARG AGAVE_LINUX_AMD64_SHA256=5991d027a686eb419a709a479178b33eb83501e8a2bfbf599a81a286bfcbf770

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl bzip2 hostname libssl3 libudev1 zlib1g \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) agave_arch=x86_64-unknown-linux-gnu ;; \
      *) echo "Agave ${AGAVE_VERSION} has no official Linux archive for ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl --fail --location --retry 5 \
      "https://github.com/anza-xyz/agave/releases/download/${AGAVE_VERSION}/solana-release-${agave_arch}.tar.bz2" \
      --output /tmp/agave.tar.bz2; \
    echo "${AGAVE_LINUX_AMD64_SHA256}  /tmp/agave.tar.bz2" | sha256sum --check --strict; \
    mkdir /opt/agave; \
    tar --extract --bzip2 --file /tmp/agave.tar.bz2 --directory /opt/agave --strip-components=1; \
    rm /tmp/agave.tar.bz2; \
    /opt/agave/bin/solana --version | grep -F "${AGAVE_VERSION#v}"

COPY --from=portable-server-builder \
  /usr/src/agave/target/release/agave-validator \
  /usr/src/agave/target/release/solana-genesis \
  /usr/src/agave/target/release/solana-faucet \
  /opt/agave/bin/

RUN /opt/agave/bin/agave-validator --version | grep -F "${AGAVE_VERSION#v}" \
    && /opt/agave/bin/solana-genesis --version | grep -F "${AGAVE_VERSION#v}" \
    && /opt/agave/bin/solana-faucet --version | grep -F "${AGAVE_VERSION#v}"

ENV PATH=/opt/agave/bin:${PATH}
ENV RUST_BACKTRACE=1

COPY docker/ /usr/local/lib/agave-localnet/
RUN chmod +x /usr/local/lib/agave-localnet/*.sh

WORKDIR /workspace
ENTRYPOINT ["/usr/local/lib/agave-localnet/entrypoint.sh"]
CMD ["bash"]

FROM agave-runtime AS wallet-service

RUN apt-get update \
    && apt-get install --yes --no-install-recommends python3 python3-cryptography \
    && rm -rf /var/lib/apt/lists/*

ARG SWAGGER_UI_VERSION=5.32.6
RUN mkdir -p /opt/wallet-service/swagger-ui \
    && curl --fail --location --retry 5 \
      "https://raw.githubusercontent.com/swagger-api/swagger-ui/v${SWAGGER_UI_VERSION}/dist/swagger-ui.css" \
      --output /opt/wallet-service/swagger-ui/swagger-ui.css \
    && curl --fail --location --retry 5 \
      "https://raw.githubusercontent.com/swagger-api/swagger-ui/v${SWAGGER_UI_VERSION}/dist/swagger-ui-bundle.js" \
      --output /opt/wallet-service/swagger-ui/swagger-ui-bundle.js \
    && curl --fail --location --retry 5 \
      "https://raw.githubusercontent.com/swagger-api/swagger-ui/v${SWAGGER_UI_VERSION}/dist/oauth2-redirect.html" \
      --output /opt/wallet-service/swagger-ui/oauth2-redirect.html \
    && curl --fail --location --retry 5 \
      "https://raw.githubusercontent.com/swagger-api/swagger-ui/v${SWAGGER_UI_VERSION}/dist/oauth2-redirect.js" \
      --output /opt/wallet-service/swagger-ui/oauth2-redirect.js

COPY wallet-service/server.py /opt/wallet-service/server.py

EXPOSE 8787
HEALTHCHECK --interval=10s --timeout=3s --retries=10 \
  CMD curl --fail --silent http://127.0.0.1:8787/health || exit 1

ENTRYPOINT ["python3", "/opt/wallet-service/server.py"]
CMD []

# Keep the default build target as the general Agave runtime image.
FROM agave-runtime AS agave-localnet
