#!/usr/bin/env python3
"""Development-only custodial wallet API for the Docker localnet."""

import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa


DATA_DIR = Path(os.environ.get("WALLET_DATA_DIR", "/wallets"))
RPC_URL = os.environ.get("SOLANA_RPC_URL", "http://rpc:8899")
MASTER_KEY = os.environ.get("WALLET_MASTER_KEY", "")
PORT = int(os.environ.get("WALLET_SERVICE_PORT", "8787"))
KEYCLOAK_ISSUER = os.environ.get("KEYCLOAK_ISSUER", "http://localhost:8080/realms/local-wallet")
KEYCLOAK_JWKS_URL = os.environ.get("KEYCLOAK_JWKS_URL", "http://keycloak:8080/realms/local-wallet/protocol/openid-connect/certs")
KEYCLOAK_AUDIENCE = os.environ.get("KEYCLOAK_AUDIENCE", "wallet-api")
PRIVATE_KEY_EXPORT_ENABLED = os.environ.get("WALLET_PRIVATE_KEY_EXPORT_ENABLED", "false").lower() == "true"
TRANSACTION_SIGNING_ENABLED = os.environ.get("WALLET_TRANSACTION_SIGNING_ENABLED", "true").lower() == "true"
LEGACY_API_ENABLED = os.environ.get("WALLET_LEGACY_API_ENABLED", "false").lower() == "true"
SWAGGER_DIR = Path(os.environ.get("SWAGGER_UI_DIR", "/opt/wallet-service/swagger-ui"))
MAX_BODY = 16 * 1024
BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
SIGNATURE_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{80,90}")
LOCK = threading.RLock()
JWKS_CACHE = {"expires": 0, "keys": {}}


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message
        super().__init__(message)


def atomic_write(path, data, mode=0o600):
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build_cipher():
    if len(MASTER_KEY) < 16:
        raise RuntimeError("WALLET_MASTER_KEY must contain at least 16 characters")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)
    salt_path = DATA_DIR / ".salt"
    if not salt_path.exists():
        atomic_write(salt_path, os.urandom(16))
    salt = salt_path.read_bytes()
    key = hashlib.pbkdf2_hmac("sha256", MASTER_KEY.encode(), salt, 600_000, dklen=32)
    return Fernet(base64.urlsafe_b64encode(key))


CIPHER = build_cipher()


def wallet_paths(wallet_id):
    try:
        normalized = str(uuid.UUID(wallet_id))
    except (ValueError, AttributeError):
        raise ApiError(404, "Wallet not found")
    return DATA_DIR / f"{normalized}.json", DATA_DIR / f"{normalized}.key"


def load_wallet(wallet_id):
    metadata_path, key_path = wallet_paths(wallet_id)
    if not metadata_path.exists() or not key_path.exists():
        raise ApiError(404, "Wallet not found")
    try:
        return json.loads(metadata_path.read_text())
    except (OSError, json.JSONDecodeError):
        raise ApiError(500, "Wallet metadata is damaged")


def load_legacy_wallet(wallet_id):
    metadata = load_wallet(wallet_id)
    if metadata.get("ownerSub"):
        raise ApiError(404, "Wallet not found")
    return metadata


def all_metadata():
    for metadata_path in sorted(DATA_DIR.glob("*.json")):
        try:
            yield json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue


def rpc(method, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    request = Request(RPC_URL, data=body, headers={"content-type": "application/json"})
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        raise ApiError(503, f"RPC unavailable: {error}")
    if payload.get("error"):
        raise ApiError(502, payload["error"].get("message", "RPC request failed"))
    return payload.get("result")


def balance(address):
    result = rpc("getBalance", [address, {"commitment": "confirmed"}])
    value = (Decimal(result["value"]) / Decimal(1_000_000_000)).quantize(Decimal("0.000000001"))
    return format(value, "f")


def history(address):
    signatures = rpc("getSignaturesForAddress", [address, {"limit": 25, "commitment": "confirmed"}])
    return [{
        "signature": item["signature"],
        "slot": item["slot"],
        "status": "failed" if item.get("err") else "confirmed",
        "blockTime": item.get("blockTime"),
        "memo": item.get("memo"),
    } for item in signatures]


def public_wallet(metadata, include_history=False):
    result = dict(metadata)
    result["balance"] = balance(metadata["address"])
    if include_history:
        result["history"] = history(metadata["address"])
    return result


def amount(value):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ApiError(400, "Amount must be a number")
    if not parsed.is_finite() or parsed <= 0 or parsed > Decimal("1000000"):
        raise ApiError(400, "Amount must be greater than 0 and at most 1,000,000 SOL")
    if parsed.as_tuple().exponent < -9:
        raise ApiError(400, "Amount supports at most 9 decimal places")
    return format(parsed, "f")


def run_cli(arguments, timeout=60):
    try:
        completed = subprocess.run(arguments, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ApiError(504, "Solana CLI operation timed out")
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "Solana CLI operation failed").strip()
        raise ApiError(422, detail[-1000:])
    return completed.stdout.strip()


class DecryptedKeypair:
    def __init__(self, wallet_id):
        self.wallet_id = wallet_id
        self.path = None

    def __enter__(self):
        _, key_path = wallet_paths(self.wallet_id)
        try:
            plaintext = CIPHER.decrypt(key_path.read_bytes())
        except (OSError, InvalidToken):
            raise ApiError(500, "Wallet key cannot be decrypted with the configured master key")
        fd, self.path = tempfile.mkstemp(prefix="wallet-key-", suffix=".json")
        try:
            os.write(fd, plaintext)
        finally:
            os.close(fd)
        os.chmod(self.path, 0o600)
        return self.path

    def __exit__(self, *_):
        if self.path:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass


def create_wallet(name, owner_sub=None, username=None):
    clean_name = str(name or "").strip()
    if not clean_name or len(clean_name) > 60:
        raise ApiError(400, "Name must contain 1 to 60 characters")
    wallet_id = str(uuid.uuid4())
    metadata_path, key_path = wallet_paths(wallet_id)
    fd, temporary = tempfile.mkstemp(prefix="new-wallet-", suffix=".json")
    os.close(fd)
    try:
        run_cli(["solana-keygen", "new", "--no-bip39-passphrase", "--silent", "--force", "--outfile", temporary])
        keypair = Path(temporary).read_bytes()
        address = run_cli(["solana-keygen", "pubkey", temporary])
        metadata = {"id": wallet_id, "name": clean_name, "address": address, "createdAt": int(time.time())}
        if owner_sub:
            metadata["ownerSub"] = owner_sub
            metadata["username"] = username
        with LOCK:
            atomic_write(key_path, CIPHER.encrypt(keypair))
            atomic_write(metadata_path, json.dumps(metadata, separators=(",", ":")).encode())
        return public_wallet(metadata)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def decode_segment(value):
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        raise ApiError(401, "Malformed access token")


def jwks(force=False):
    now = time.time()
    with LOCK:
        if not force and JWKS_CACHE["expires"] > now:
            return JWKS_CACHE["keys"]
    try:
        with urlopen(KEYCLOAK_JWKS_URL, timeout=10) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        raise ApiError(503, f"Identity provider unavailable: {error}")
    keys = {item["kid"]: item for item in payload.get("keys", []) if item.get("kid") and item.get("kty") == "RSA"}
    with LOCK:
        JWKS_CACHE.update({"expires": now + 300, "keys": keys})
    return keys


def verify_access_token(token):
    parts = token.split(".")
    if len(parts) != 3:
        raise ApiError(401, "Malformed access token")
    try:
        header = json.loads(decode_segment(parts[0]))
        claims = json.loads(decode_segment(parts[1]))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError(401, "Malformed access token")
    if header.get("alg") != "RS256" or not header.get("kid"):
        raise ApiError(401, "Unsupported access token")
    key = jwks().get(header["kid"]) or jwks(force=True).get(header["kid"])
    if not key:
        raise ApiError(401, "Unknown token signing key")
    try:
        public_key = rsa.RSAPublicNumbers(
            int.from_bytes(decode_segment(key["e"]), "big"),
            int.from_bytes(decode_segment(key["n"]), "big"),
        ).public_key()
        public_key.verify(
            decode_segment(parts[2]),
            f"{parts[0]}.{parts[1]}".encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except (InvalidSignature, KeyError, ValueError, TypeError):
        raise ApiError(401, "Invalid access token signature")
    now = int(time.time())
    audiences = claims.get("aud", [])
    if isinstance(audiences, str):
        audiences = [audiences]
    if claims.get("iss") != KEYCLOAK_ISSUER or KEYCLOAK_AUDIENCE not in audiences:
        raise ApiError(401, "Access token was issued for another application")
    if int(claims.get("exp", 0)) <= now or int(claims.get("nbf", 0)) > now:
        raise ApiError(401, "Access token has expired or is not active")
    if not claims.get("sub") or not claims.get("preferred_username"):
        raise ApiError(401, "Access token is missing user identity")
    return claims


def user_wallet(subject, username, create=True):
    wallets = user_wallets(subject)
    with LOCK:
        if wallets:
            return wallets[0]
        if not create:
            return None
        return create_wallet("Primary wallet", owner_sub=subject, username=username)


def user_wallets(subject):
    return sorted(
        (metadata for metadata in all_metadata() if metadata.get("ownerSub") == subject),
        key=lambda metadata: (metadata.get("createdAt", 0), metadata.get("id", "")),
    )


def owned_wallet(wallet_id, subject):
    metadata = load_wallet(wallet_id)
    if metadata.get("ownerSub") != subject:
        raise ApiError(404, "Wallet not found")
    return metadata


def app_wallet(metadata, include_history=False, owner_name=None):
    result = public_wallet(metadata, include_history=include_history)
    result.pop("ownerSub", None)
    username = result.pop("username", None)
    result["publicKey"] = result["address"]
    result["owner"] = {"username": username, "name": owner_name or username}
    result["privateKeyExportEnabled"] = PRIVATE_KEY_EXPORT_ENABLED
    return result


def create_user_wallet(subject, username, name):
    wallets = user_wallets(subject)
    if len(wallets) >= 10:
        raise ApiError(409, "A development account can contain at most 10 wallets")
    clean_name = str(name or "").strip()
    if any(str(item.get("name", "")).casefold() == clean_name.casefold() for item in wallets):
        raise ApiError(409, "Choose a different wallet name")
    return app_wallet(create_wallet(clean_name, owner_sub=subject, username=username))


def export_private_key(metadata, confirmation):
    if not PRIVATE_KEY_EXPORT_ENABLED:
        raise ApiError(403, "Private-key export is disabled by the wallet service")
    if confirmation != metadata["address"]:
        raise ApiError(400, "Type the full wallet address to confirm private-key export")
    with DecryptedKeypair(metadata["id"]) as keypair_path:
        try:
            private_key = json.loads(Path(keypair_path).read_text())
        except (OSError, json.JSONDecodeError):
            raise ApiError(500, "Wallet key could not be exported")
    return {
        "walletId": metadata["id"],
        "publicKey": metadata["address"],
        "privateKey": private_key,
        "encoding": "Solana JSON keypair byte array",
        "warning": "Development use only. Anyone with this value can control the wallet.",
    }


def wallet_by_username(username):
    target = username.casefold()
    matches = [
        metadata for metadata in all_metadata()
        if metadata.get("ownerSub") and str(metadata.get("username", "")).casefold() == target
    ]
    return min(matches, key=lambda metadata: (metadata.get("createdAt", 0), metadata.get("id", ""))) if matches else None


def fund_address(address, sol):
    if not BASE58_RE.fullmatch(str(address or "")):
        raise ApiError(400, "Address must be a valid Solana address")
    output = run_cli(["solana", "--url", RPC_URL, "--commitment", "confirmed", "airdrop", sol, address], 90)
    match = SIGNATURE_RE.search(output)
    return {"signature": match.group(0) if match else None, "address": address, "balance": balance(address)}


def fund_wallet(metadata, sol):
    return fund_address(metadata["address"], sol)


def transfer_wallet(metadata, recipient, sol):
    with DecryptedKeypair(metadata["id"]) as keypair:
        output = run_cli(["solana", "transfer", "--url", RPC_URL, "--commitment", "confirmed", "--output", "json-compact", "--allow-unfunded-recipient", "--from", keypair, "--fee-payer", keypair, recipient, sol], 90)
    try:
        signature = json.loads(output).get("signature")
    except (json.JSONDecodeError, AttributeError):
        match = SIGNATURE_RE.search(output)
        signature = match.group(0) if match else None
    return {"signature": signature, "balance": balance(metadata["address"])}


def base58_encode(value):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = int.from_bytes(value, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = alphabet[remainder] + encoded
    leading_zeroes = len(value) - len(value.lstrip(b"\0"))
    return "1" * leading_zeroes + (encoded or ("" if leading_zeroes else "1"))


def base58_decode(value):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = 0
    try:
        for character in value:
            number = number * 58 + alphabet.index(character)
    except ValueError:
        raise ApiError(400, "Wallet public key is invalid")
    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * (len(value) - len(value.lstrip("1"))) + decoded


def decode_shortvec(value, offset):
    result = 0
    shift = 0
    for _ in range(3):
        if offset >= len(value):
            raise ApiError(400, "Transaction message is truncated")
        byte = value[offset]
        offset += 1
        result |= (byte & 0x7f) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
    raise ApiError(400, "Transaction message contains an invalid compact length")


def decode_transaction_message(metadata, payload, single_signer=False):
    if not TRANSACTION_SIGNING_ENABLED:
        raise ApiError(403, "Transaction signing is disabled by the wallet service")
    encoded = str(payload.get("messageBase64", "")).strip()
    try:
        message = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise ApiError(400, "messageBase64 must be valid standard Base64")
    if len(message) < 36 or len(message) > 1167:
        raise ApiError(400, "Serialized transaction message has an invalid size")
    versioned = bool(message[0] & 0x80)
    if versioned and (message[0] & 0x7f) != 0:
        raise ApiError(400, "Only legacy and version-0 Solana messages are supported")
    header_offset = 1 if versioned else 0
    required_signatures = message[header_offset]
    if required_signatures < 1 or (single_signer and required_signatures != 1):
        detail = "Broadcast currently supports transactions requiring exactly one signature" if single_signer else "Transaction does not require a signer"
        raise ApiError(400, detail)
    account_count, account_offset = decode_shortvec(message, header_offset + 3)
    if account_count < required_signatures or account_offset + account_count * 32 > len(message):
        raise ApiError(400, "Serialized transaction message has an invalid account list")
    if message[account_offset:account_offset + 32] != base58_decode(metadata["address"]):
        raise ApiError(400, "Selected wallet must be the transaction fee payer and first signer")
    return message


def sign_transaction(metadata, payload, broadcast=False):
    message = decode_transaction_message(metadata, payload, single_signer=broadcast)
    with DecryptedKeypair(metadata["id"]) as keypair_path:
        try:
            keypair = json.loads(Path(keypair_path).read_text())
            seed = bytes(keypair[:32])
            if len(keypair) != 64 or len(seed) != 32:
                raise ValueError
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            raise ApiError(500, "Wallet key has an invalid format")
    signature = ed25519.Ed25519PrivateKey.from_private_bytes(seed).sign(message)
    result = {
        "walletId": metadata["id"],
        "publicKey": metadata["address"],
        "signature": base58_encode(signature),
        "signatureBase64": base64.b64encode(signature).decode(),
    }
    if broadcast:
        wire_transaction = b"\x01" + signature + message
        result["transactionSignature"] = rpc("sendTransaction", [
            base64.b64encode(wire_transaction).decode(),
            {"encoding": "base64", "preflightCommitment": "confirmed"},
        ])
        result["submitted"] = True
    return result


def app_transfer(metadata, claims, payload):
    recipient_input = str(payload.get("recipient", "")).strip()
    recipient_user = None
    if recipient_input.startswith("@"):
        recipient_wallet = wallet_by_username(recipient_input[1:])
        if not recipient_wallet:
            raise ApiError(404, "No active wallet exists for that username")
        if recipient_wallet.get("ownerSub") == claims["sub"]:
            raise ApiError(400, "Choose another user as the recipient")
        recipient = recipient_wallet["address"]
        recipient_user = recipient_wallet["username"]
    else:
        recipient = recipient_input
        if not BASE58_RE.fullmatch(recipient):
            raise ApiError(400, "Recipient must be an @username or valid Solana address")
    result = transfer_wallet(metadata, recipient, amount(payload.get("sol")))
    result.update({"recipient": recipient, "recipientUsername": recipient_user})
    return result


def openapi_document():
    bearer = [{"keycloak": ["openid", "profile", "email"]}]
    wallet_id = {"name": "walletId", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}}
    error_response = {"description": "Error", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "PerkBridge Custodial Wallet API",
            "version": "2.0.0",
            "description": "OAuth-secured development API for owner-scoped Solana custodial wallets. Private-key export is disabled unless explicitly enabled and always requires address confirmation.",
        },
        "servers": [{"url": "/wallet-api", "description": "Wallet application reverse proxy"}],
        "security": bearer,
        "tags": [
            {"name": "Wallets", "description": "Create, list, inspect, and delete wallets owned by the signed-in user."},
            {"name": "Keys", "description": "Inspect public-key information and explicitly export a private key in development mode."},
            {"name": "Transactions", "description": "Fund wallets and send local SOL."},
            {"name": "Users", "description": "Resolve registered payment handles."},
        ],
        "paths": {
            "/health": {"get": {"security": [], "summary": "Health check", "responses": {"200": {"description": "Healthy"}}}},
            "/api/app/airdrop": {
                "post": {"tags": ["Transactions"], "summary": "Airdrop local SOL to an address", "description": "Funds any valid localnet Solana address; no custodial wallet UUID is required.", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/AddressAirdrop"}}}}, "responses": {"200": {"description": "Airdrop submitted"}, "400": error_response, "401": error_response, "422": error_response}},
            },
            "/api/app/wallets": {
                "get": {"tags": ["Wallets"], "summary": "List my wallets", "responses": {"200": {"description": "Owner-scoped wallets", "content": {"application/json": {"schema": {"type": "object", "properties": {"wallets": {"type": "array", "items": {"$ref": "#/components/schemas/Wallet"}}}}}}}, "401": error_response}},
                "post": {"tags": ["Wallets"], "summary": "Create a wallet", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/CreateWallet"}}}}, "responses": {"201": {"description": "Created", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Wallet"}}}}, "400": error_response, "401": error_response, "409": error_response}},
            },
            "/api/app/wallets/{walletId}": {
                "get": {"tags": ["Wallets"], "summary": "Get wallet information and transactions", "parameters": [wallet_id], "responses": {"200": {"description": "Wallet details", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/WalletDetails"}}}}, "401": error_response, "404": error_response}},
                "delete": {"tags": ["Wallets"], "summary": "Delete a wallet", "description": "Requires the full public address and refuses to delete the account's only wallet.", "parameters": [wallet_id], "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Confirmation"}}}}, "responses": {"200": {"description": "Deleted"}, "400": error_response, "401": error_response, "404": error_response, "409": error_response}},
            },
            "/api/app/wallets/{walletId}/keys": {
                "get": {"tags": ["Keys"], "summary": "Get public-key and owner information", "parameters": [wallet_id], "responses": {"200": {"description": "Key information", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/KeyInfo"}}}}, "401": error_response, "404": error_response}},
            },
            "/api/app/wallets/{walletId}/export": {
                "post": {"tags": ["Keys"], "summary": "Export the private key", "description": "DANGER: development-only secret export. Requires OAuth, wallet ownership, the feature flag, and exact public-address confirmation. Never log or persist the response.", "parameters": [wallet_id], "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Confirmation"}}}}, "responses": {"200": {"description": "Sensitive key material; never cache", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/PrivateKeyExport"}}}}, "400": error_response, "401": error_response, "403": error_response, "404": error_response}},
            },
            "/api/app/wallets/{walletId}/sign": {
                "post": {"tags": ["Transactions"], "summary": "Sign a serialized Solana transaction message", "description": "Returns an Ed25519 signature without broadcasting. The selected wallet must be the message's fee payer and first signer. Attach the returned signature with a Solana SDK.", "parameters": [wallet_id], "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/TransactionMessage"}}}}, "responses": {"200": {"description": "Signature", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/TransactionSignature"}}}}, "400": error_response, "401": error_response, "403": error_response, "404": error_response}},
            },
            "/api/app/wallets/{walletId}/sign-and-send": {
                "post": {"tags": ["Transactions"], "summary": "Sign and broadcast a single-signer transaction", "description": "Signs a legacy or version-0 message requiring exactly one signature, assembles the wire transaction, and submits it to localnet.", "parameters": [wallet_id], "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/TransactionMessage"}}}}, "responses": {"200": {"description": "Signed and submitted", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/TransactionSignature"}}}}, "400": error_response, "401": error_response, "403": error_response, "404": error_response, "502": error_response}},
            },
            "/api/app/wallets/{walletId}/airdrop": {
                "post": {"tags": ["Transactions"], "summary": "Fund a wallet from the local faucet", "parameters": [wallet_id], "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Amount"}}}}, "responses": {"200": {"description": "Airdrop submitted"}, "400": error_response, "401": error_response, "404": error_response}},
            },
            "/api/app/wallets/{walletId}/transfer": {
                "post": {"tags": ["Transactions"], "summary": "Send local SOL", "parameters": [wallet_id], "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Transfer"}}}}, "responses": {"200": {"description": "Transfer submitted"}, "400": error_response, "401": error_response, "404": error_response}},
            },
            "/api/app/users": {
                "get": {"tags": ["Users"], "summary": "Search payment handles", "parameters": [{"name": "query", "in": "query", "required": True, "schema": {"type": "string", "minLength": 2}}], "responses": {"200": {"description": "Matching users"}, "401": error_response}},
            },
        },
        "components": {
            "securitySchemes": {"keycloak": {"type": "oauth2", "description": "Enter your Keycloak username and password. The preconfigured wallet-swagger client is public and does not use a client secret.", "flows": {"password": {"tokenUrl": f"{KEYCLOAK_ISSUER}/protocol/openid-connect/token", "scopes": {"openid": "Authenticate", "profile": "Read profile", "email": "Read email"}}}}},
            "schemas": {
                "Owner": {"type": "object", "properties": {"username": {"type": "string"}, "name": {"type": "string"}}, "required": ["username", "name"]},
                "Wallet": {"type": "object", "properties": {"id": {"type": "string", "format": "uuid"}, "name": {"type": "string"}, "address": {"type": "string"}, "publicKey": {"type": "string"}, "balance": {"type": "string"}, "createdAt": {"type": "integer"}, "owner": {"$ref": "#/components/schemas/Owner"}, "privateKeyExportEnabled": {"type": "boolean"}}, "required": ["id", "name", "address", "publicKey", "balance", "owner"]},
                "WalletDetails": {"allOf": [{"$ref": "#/components/schemas/Wallet"}, {"type": "object", "properties": {"history": {"type": "array", "items": {"$ref": "#/components/schemas/Transaction"}}}}]},
                "Transaction": {"type": "object", "properties": {"signature": {"type": "string"}, "slot": {"type": "integer"}, "status": {"type": "string", "enum": ["confirmed", "failed"]}, "blockTime": {"type": ["integer", "null"]}, "memo": {"type": ["string", "null"]}}},
                "KeyInfo": {"type": "object", "properties": {"walletId": {"type": "string", "format": "uuid"}, "publicKey": {"type": "string"}, "owner": {"$ref": "#/components/schemas/Owner"}, "privateKeyExportEnabled": {"type": "boolean"}}},
                "PrivateKeyExport": {"type": "object", "properties": {"walletId": {"type": "string", "format": "uuid"}, "publicKey": {"type": "string"}, "privateKey": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 255}, "writeOnly": True}, "encoding": {"type": "string"}, "warning": {"type": "string"}}, "required": ["walletId", "publicKey", "privateKey", "warning"]},
                "TransactionMessage": {"type": "object", "properties": {"messageBase64": {"type": "string", "format": "byte", "description": "Standard-Base64 serialized legacy or version-0 Solana transaction message (not a full wire transaction)."}}, "required": ["messageBase64"]},
                "TransactionSignature": {"type": "object", "properties": {"walletId": {"type": "string", "format": "uuid"}, "publicKey": {"type": "string"}, "signature": {"type": "string", "description": "Base58 Ed25519 signature"}, "signatureBase64": {"type": "string", "format": "byte"}, "transactionSignature": {"type": "string"}, "submitted": {"type": "boolean"}}, "required": ["walletId", "publicKey", "signature", "signatureBase64"]},
                "CreateWallet": {"type": "object", "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 60}}, "required": ["name"]},
                "Confirmation": {"type": "object", "properties": {"confirmation": {"type": "string", "description": "Exact full public wallet address"}}, "required": ["confirmation"]},
                "Amount": {"type": "object", "properties": {"sol": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000000}}, "required": ["sol"]},
                "AddressAirdrop": {"type": "object", "properties": {"address": {"type": "string", "description": "Solana address to fund"}, "sol": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000000}}, "required": ["address", "sol"]},
                "Transfer": {"allOf": [{"$ref": "#/components/schemas/Amount"}, {"type": "object", "properties": {"recipient": {"type": "string", "description": "@username or Solana address"}}, "required": ["recipient"]}]},
                "Error": {"type": "object", "properties": {"error": {"type": "string"}}, "required": ["error"]},
            },
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalnetWallet/1.0"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def send_json(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, status, body, content_type):
        self.send_response(status)
        self.send_header("content-type", content_type)
        dynamic = content_type.startswith("text/html") or content_type.startswith("application/json")
        self.send_header("cache-control", "no-store" if dynamic else "public, max-age=86400")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_documentation(self):
        path = self.path.split("?", 1)[0]
        if self.command != "GET":
            return False
        if path == "/openapi.json":
            self.send_bytes(200, json.dumps(openapi_document(), separators=(",", ":")).encode(), "application/json; charset=utf-8")
            return True
        if path in ("/docs", "/docs/"):
            asset_prefix = "" if path.endswith("/") else "docs/"
            html = f"""<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>PerkBridge Wallet API</title><link rel=\"stylesheet\" href=\"{asset_prefix}swagger-ui.css\"></head><body><div id=\"swagger-ui\"></div><script src=\"{asset_prefix}swagger-ui-bundle.js\"></script><script src=\"{asset_prefix}swagger-initializer.js?v=20260721-4\"></script></body></html>"""
            self.send_bytes(200, html.encode(), "text/html; charset=utf-8")
            return True
        if path == "/docs/swagger-initializer.js":
            script = b"""window.onload=function(){const base=window.location.pathname.startsWith('/wallet-api/')?'/wallet-api':'';const hideClientSecret=function(){document.querySelectorAll('.auth-container input').forEach(function(input){const text=((input.name||'')+' '+(input.placeholder||'')+' '+((input.closest('.wrapper')||input.closest('label'))?.textContent||''));if(/client[_ ]?secret/i.test(text)){const row=input.closest('.wrapper')||input.closest('label');if(row)row.style.display='none';input.value='';}});};const observer=new MutationObserver(hideClientSecret);observer.observe(document.body,{childList:true,subtree:true});const ui=SwaggerUIBundle({url:base+'/openapi.json',dom_id:'#swagger-ui',deepLinking:true,persistAuthorization:false,displayRequestDuration:true,filter:true,tryItOutEnabled:true,validatorUrl:null,oauth2RedirectUrl:window.location.origin+base+'/oauth2-redirect.html',presets:[SwaggerUIBundle.presets.apis,SwaggerUIBundle.SwaggerUIStandalonePreset],layout:'BaseLayout'});ui.initOAuth({clientId:'wallet-swagger',appName:'PerkBridge Wallet API',scopes:'openid profile email'});hideClientSecret();window.ui=ui;};"""
            self.send_bytes(200, script, "text/javascript; charset=utf-8")
            return True
        assets = {
            "/docs/swagger-ui.css": ("swagger-ui.css", "text/css; charset=utf-8"),
            "/docs/swagger-ui-bundle.js": ("swagger-ui-bundle.js", "text/javascript; charset=utf-8"),
            "/docs/oauth2-redirect.html": ("oauth2-redirect.html", "text/html; charset=utf-8"),
            "/docs/oauth2-redirect.js": ("oauth2-redirect.js", "text/javascript; charset=utf-8"),
            "/oauth2-redirect.html": ("oauth2-redirect.html", "text/html; charset=utf-8"),
            "/oauth2-redirect.js": ("oauth2-redirect.js", "text/javascript; charset=utf-8"),
        }
        if path in assets:
            filename, content_type = assets[path]
            try:
                body = (SWAGGER_DIR / filename).read_bytes()
            except OSError:
                raise ApiError(503, "Swagger UI assets are unavailable")
            self.send_bytes(200, body, content_type)
            return True
        return False

    def read_json(self):
        try:
            length = int(self.headers.get("content-length", "0"))
        except ValueError:
            raise ApiError(400, "Invalid Content-Length")
        if length > MAX_BODY:
            raise ApiError(413, "Request body is too large")
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            raise ApiError(400, "Request body must be valid JSON")
        if not isinstance(value, dict):
            raise ApiError(400, "Request body must be a JSON object")
        return value

    def authenticate(self):
        authorization = self.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise ApiError(401, "Bearer access token required")
        return verify_access_token(authorization[7:].strip())

    def route(self):
        path = self.path.split("?", 1)[0]
        parts = [part for part in path.split("/") if part]
        if path == "/health" and self.command == "GET":
            return 200, {"status": "ok"}
        if len(parts) >= 2 and parts[:2] == ["api", "app"]:
            claims = self.authenticate()
            if parts == ["api", "app", "airdrop"] and self.command == "POST":
                payload = self.read_json()
                return 200, fund_address(str(payload.get("address", "")).strip(), amount(payload.get("sol")))
            if parts == ["api", "app", "wallets"]:
                if self.command == "GET":
                    wallets = user_wallets(claims["sub"])
                    if not wallets:
                        wallets = [user_wallet(claims["sub"], claims["preferred_username"])]
                    owner_name = claims.get("name") or claims["preferred_username"]
                    return 200, {"wallets": [app_wallet(item, owner_name=owner_name) for item in wallets]}
                if self.command == "POST":
                    created = create_user_wallet(
                        claims["sub"], claims["preferred_username"], self.read_json().get("name")
                    )
                    return 201, created
            if len(parts) >= 4 and parts[:3] == ["api", "app", "wallets"]:
                metadata = owned_wallet(parts[3], claims["sub"])
                if len(parts) == 4 and self.command == "GET":
                    return 200, app_wallet(metadata, include_history=True, owner_name=claims.get("name") or claims["preferred_username"])
                if len(parts) == 4 and self.command == "DELETE":
                    if len(user_wallets(claims["sub"])) <= 1:
                        raise ApiError(409, "Create another wallet before deleting your only wallet")
                    confirmation = self.read_json().get("confirmation")
                    if confirmation != metadata["address"]:
                        raise ApiError(400, "Type the full wallet address to confirm permanent deletion")
                    metadata_path, key_path = wallet_paths(metadata["id"])
                    with LOCK:
                        metadata_path.unlink(missing_ok=True)
                        key_path.unlink(missing_ok=True)
                    return 200, {"deleted": True, "address": metadata["address"]}
                if len(parts) == 5 and parts[4] == "airdrop" and self.command == "POST":
                    return 200, fund_wallet(metadata, amount(self.read_json().get("sol")))
                if len(parts) == 5 and parts[4] == "transfer" and self.command == "POST":
                    return 200, app_transfer(metadata, claims, self.read_json())
                if len(parts) == 5 and parts[4] == "keys" and self.command == "GET":
                    return 200, {
                        "walletId": metadata["id"],
                        "publicKey": metadata["address"],
                        "owner": {
                            "username": metadata.get("username"),
                            "name": claims.get("name") or claims["preferred_username"],
                        },
                        "privateKeyExportEnabled": PRIVATE_KEY_EXPORT_ENABLED,
                    }
                if len(parts) == 5 and parts[4] == "export" and self.command == "POST":
                    return 200, export_private_key(metadata, self.read_json().get("confirmation"))
                if len(parts) == 5 and parts[4] == "sign" and self.command == "POST":
                    return 200, sign_transaction(metadata, self.read_json())
                if len(parts) == 5 and parts[4] == "sign-and-send" and self.command == "POST":
                    return 200, sign_transaction(metadata, self.read_json(), broadcast=True)
            if parts == ["api", "app", "me"] and self.command == "GET":
                return 200, app_wallet(user_wallet(claims["sub"], claims["preferred_username"]), include_history=True, owner_name=claims.get("name") or claims["preferred_username"])
            if parts == ["api", "app", "users"] and self.command == "GET":
                query = self.path.partition("?")[2]
                query = query.split("query=", 1)[1].split("&", 1)[0] if "query=" in query else ""
                from urllib.parse import unquote_plus
                query = unquote_plus(query).lstrip("@").casefold()
                matches = []
                seen = set()
                if len(query) >= 2:
                    for item in sorted(all_metadata(), key=lambda entry: (entry.get("createdAt", 0), entry.get("id", ""))):
                        username = str(item.get("username", ""))
                        normalized = username.casefold()
                        if item.get("ownerSub") and query in normalized and item.get("ownerSub") != claims["sub"] and normalized not in seen:
                            matches.append({"username": username, "address": item["address"]})
                            seen.add(normalized)
                return 200, {"users": matches[:8]}
            if parts == ["api", "app", "me", "airdrop"] and self.command == "POST":
                return 200, fund_wallet(user_wallet(claims["sub"], claims["preferred_username"]), amount(self.read_json().get("sol")))
            if parts == ["api", "app", "me", "transfer"] and self.command == "POST":
                return 200, app_transfer(user_wallet(claims["sub"], claims["preferred_username"]), claims, self.read_json())
            if parts == ["api", "app", "me"] and self.command == "DELETE":
                metadata = user_wallet(claims["sub"], claims["preferred_username"])
                confirmation = self.read_json().get("confirmation")
                if confirmation != metadata["address"]:
                    raise ApiError(400, "Type the full wallet address to confirm permanent deletion")
                metadata_path, key_path = wallet_paths(metadata["id"])
                with LOCK:
                    metadata_path.unlink(missing_ok=True)
                    key_path.unlink(missing_ok=True)
                return 200, {"deleted": True, "address": metadata["address"]}
        if LEGACY_API_ENABLED and parts == ["api", "wallets"]:
            if self.command == "GET":
                wallets = []
                for metadata in all_metadata():
                    if metadata.get("ownerSub"):
                        continue
                    try:
                        wallets.append(public_wallet(metadata))
                    except ApiError:
                        continue
                return 200, {"wallets": wallets}
            if self.command == "POST":
                return 201, create_wallet(self.read_json().get("name"))
        if LEGACY_API_ENABLED and len(parts) >= 3 and parts[:2] == ["api", "wallets"]:
            wallet_id = parts[2]
            metadata = load_legacy_wallet(wallet_id)
            if len(parts) == 3 and self.command == "GET":
                return 200, public_wallet(metadata, include_history=True)
            if len(parts) == 3 and self.command == "DELETE":
                confirmation = self.read_json().get("confirmation")
                if confirmation != metadata["address"]:
                    raise ApiError(400, "Type the full wallet address to confirm permanent deletion")
                metadata_path, key_path = wallet_paths(wallet_id)
                with LOCK:
                    metadata_path.unlink(missing_ok=True)
                    key_path.unlink(missing_ok=True)
                return 200, {"deleted": True, "address": metadata["address"]}
            if len(parts) == 4 and parts[3] == "airdrop" and self.command == "POST":
                sol = amount(self.read_json().get("sol"))
                return 200, fund_wallet(metadata, sol)
            if len(parts) == 4 and parts[3] == "transfer" and self.command == "POST":
                payload = self.read_json()
                recipient = str(payload.get("recipient", "")).strip()
                if not BASE58_RE.fullmatch(recipient):
                    raise ApiError(400, "Recipient must be a valid Solana address")
                sol = amount(payload.get("sol"))
                return 200, transfer_wallet(metadata, recipient, sol)
        raise ApiError(404, "Route not found")

    def dispatch(self):
        try:
            if self.serve_documentation():
                return
            status, payload = self.route()
            self.send_json(status, payload)
        except ApiError as error:
            self.send_json(error.status, {"error": error.message})
        except Exception as error:
            print(f"Unhandled error: {error!r}", flush=True)
            self.send_json(500, {"error": "Internal wallet service error"})

    do_GET = dispatch
    do_POST = dispatch
    do_DELETE = dispatch


if __name__ == "__main__":
    print(f"Development custodial wallet API listening on 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
