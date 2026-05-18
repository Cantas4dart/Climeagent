import os
import json
import time
from dataclasses import dataclass
from typing import Any

import requests
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
from eth_utils import keccak, to_bytes
from py_builder_signing_sdk.config import BuilderConfig
from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
from requests.exceptions import RequestException
from web3 import Web3


PROXY_INIT_CODE_HASH = "0xd21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b"
SAFE_INIT_CODE_HASH = "0x2bce2127ff07fb632d16c8347c4ebf501f4841168bed00d9e6ef715ddb6fcecf"
SAFE_FACTORY_NAME = "Polymarket Contract Proxy Factory"

POLY_CONTRACTS = {
    137: {
        "ProxyContracts": {
            "ProxyFactory": "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052",
            "RelayHub": "0xD216153c06E857cD7f72665E0aF1d7D82172F494",
        },
        "SafeContracts": {
            "SafeFactory": "0xaacFeEa03eb1561C4e67d661e40682Bd20E3541b",
        },
    }
}


@dataclass
class RelayerResponse:
    transaction_id: str
    state: str
    transaction_hash: str
    client: "RelayClient"

    def wait(self):
        return self.client.poll_until_state(
            self.transaction_id,
            ["STATE_MINED", "STATE_CONFIRMED"],
            "STATE_FAILED",
            max_polls=100,
        )


class RelayClient:
    def __init__(
        self,
        relayer_url: str,
        chain_id: int,
        private_key: str,
        builder_config: BuilderConfig | None,
        relay_tx_type: str,
        relayer_api_key: str | None = None,
        relayer_api_key_address: str | None = None,
    ):
        self.relayer_url = relayer_url.rstrip("/")
        self.chain_id = chain_id
        self.private_key = private_key if private_key.startswith("0x") else f"0x{private_key}"
        self.account = Account.from_key(self.private_key)
        self.builder_config = builder_config
        self.relay_tx_type = relay_tx_type
        self.relayer_api_key = (relayer_api_key or "").strip() or None
        self.relayer_api_key_address = (
            Web3.to_checksum_address(relayer_api_key_address)
            if relayer_api_key_address
            else None
        )
        self.contracts = POLY_CONTRACTS[chain_id]

    def _headers(self, method: str, path: str, body: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.relayer_api_key and self.relayer_api_key_address:
            headers["RELAYER_API_KEY"] = self.relayer_api_key
            headers["RELAYER_API_KEY_ADDRESS"] = self.relayer_api_key_address
        elif self.builder_config:
            payload = self.builder_config.generate_builder_headers(method, path, body)
            headers = payload.to_dict() if payload else {}
        else:
            raise ValueError(
                "Relayer auth is not configured. Set RELAYER_API_KEY and RELAYER_API_KEY_ADDRESS or builder credentials."
            )

        if method in {"POST", "PUT", "PATCH"}:
            headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"
        headers["Access-Control-Allow-Credentials"] = "true"
        return headers

    def _send(self, endpoint: str, method: str, params: dict[str, Any] | None = None, data: str | None = None):
        url = f"{self.relayer_url}{endpoint}"
        headers = self._headers(method, endpoint, data if method != "GET" else None)
        print(f"[RELAY DEBUG] {method} {url}")
        print(f"[RELAY DEBUG] Headers: {headers}")
        if data:
            print(f"[RELAY DEBUG] Body: {data[:500]}...")
        try:
            response = requests.request(method, url, params=params, data=data, headers=headers, timeout=30)
        except RequestException as exc:
            error_text = str(exc)
            print(f"[RELAY ERROR] Connection failed to {url}: {error_text}")
            raise RuntimeError(f"Relayer connection failed to {url}: {error_text}") from exc

        try:
            response.raise_for_status()
        except Exception as e:
            print(f"[RELAY ERROR] Status {response.status_code}: {response.text}")
            raise
        return response.json()

    def get_nonce(self, signer_address: str, signer_type: str):
        return self._send("/nonce", "GET", params={"address": signer_address, "type": signer_type})

    def get_relay_payload(self, signer_address: str, signer_type: str):
        return self._send("/relay-payload", "GET", params={"address": signer_address, "type": signer_type})

    def get_transaction(self, transaction_id: str):
        return self._send("/transaction", "GET", params={"id": transaction_id})

    def test_connection(self) -> dict[str, Any]:
        return {
            "relay_url": self.relayer_url,
            "relay_tx_type": self.relay_tx_type,
            "address": self.account.address,
            "nonce": self.get_nonce(self.account.address, self.relay_tx_type),
            "relay_payload": self.get_relay_payload(self.account.address, self.relay_tx_type),
        }

    def get_deployed(self, safe: str) -> bool:
        resp = self._send("/deployed", "GET", params={"address": safe})
        return bool(resp.get("deployed"))

    def poll_until_state(self, transaction_id: str, states: list[str], fail_state: str | None = None, max_polls: int = 10, poll_frequency: int = 2000):
        for _ in range(max_polls):
            txns = self.get_transaction(transaction_id)
            if txns:
                txn = txns[0]
                if txn.get("state") in states:
                    return txn
                if fail_state and txn.get("state") == fail_state:
                    return None
            time.sleep(poll_frequency / 1000)
        return None

    def _derive_proxy_wallet(self, owner_address: str) -> str:
        proxy_factory = self.contracts["ProxyContracts"]["ProxyFactory"]
        salt = keccak(bytes.fromhex(owner_address[2:]))
        raw = keccak(b"\xff" + bytes.fromhex(proxy_factory[2:]) + salt + bytes.fromhex(PROXY_INIT_CODE_HASH[2:]))
        return Web3.to_checksum_address(raw[-20:].hex())

    def _derive_safe(self, owner_address: str) -> str:
        safe_factory = self.contracts["SafeContracts"]["SafeFactory"]
        encoded = bytes(12) + bytes.fromhex(owner_address[2:])
        salt = keccak(encoded)
        raw = keccak(b"\xff" + bytes.fromhex(safe_factory[2:]) + salt + bytes.fromhex(SAFE_INIT_CODE_HASH[2:]))
        return Web3.to_checksum_address(raw[-20:].hex())

    def _polygon_rpc_candidates(self) -> list[str]:
        configured = (os.getenv("POLYGON_RPC_URL") or "").strip()
        extra_configured = [
            value.strip()
            for value in (os.getenv("POLYGON_RPC_URLS") or "").split(",")
            if value.strip()
        ]
        candidates = [
            configured,
            *extra_configured,
            "https://polygon.drpc.org",
            "https://tenderly.rpc.polygon.community",
            "https://polygon.publicnode.com",
        ]
        seen = set()
        result = []
        for url in candidates:
            if not url:
                continue
            key = url.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(url)
        return result

    def _estimate_proxy_gas_limit(self, to_addr: str, data_hex: str, default: str = "10000000") -> str:
        tx = {
            "from": Web3.to_checksum_address(self.account.address),
            "to": Web3.to_checksum_address(to_addr),
            "data": data_hex,
        }
        last_error = None
        for rpc_url in self._polygon_rpc_candidates():
            try:
                web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))
                gas_estimate = web3.eth.estimate_gas(tx)
                return str(gas_estimate)
            except Exception as exc:
                last_error = exc
                print(f"[RELAY DEBUG] Gas estimate failed on {rpc_url}: {exc}")
        if last_error:
            print(f"[RELAY DEBUG] Falling back to default proxy gas limit {default}: {last_error}")
        return default

    def _sign_message(self, digest_hex: str) -> str:
        signed = Account.sign_message(
            encode_defunct(primitive=bytes.fromhex(digest_hex[2:])),
            private_key=self.private_key,
        )
        sig = signed.signature.hex()
        return sig if sig.startswith("0x") else f"0x{sig}"

    def _sign_typed_data(self, domain: dict[str, Any], types: dict[str, Any], value: dict[str, Any], primary_type: str) -> str:
        signed = Account.sign_typed_data(
            self.private_key,
            domain_data=domain,
            message_types=types,
            message_data=value,
        )
        sig = signed.signature.hex()
        return sig if sig.startswith("0x") else f"0x{sig}"

    @staticmethod
    def _pack_safe_signature(sig_hex: str) -> str:
        sig = sig_hex[2:] if sig_hex.startswith("0x") else sig_hex
        sig_v = int(sig[-2:], 16)
        if sig_v in (0, 1):
            sig_v += 31
        elif sig_v in (27, 28):
            sig_v += 4
        else:
            raise ValueError("Invalid signature")
        sig = sig[:-2] + f"{sig_v:02x}"
        r = int(sig[:64], 16)
        s = int(sig[64:128], 16)
        v = int(sig[128:130], 16)
        return "0x" + r.to_bytes(32, "big").hex() + s.to_bytes(32, "big").hex() + v.to_bytes(1, "big").hex()

    def _submit(self, request_payload: dict[str, Any]) -> RelayerResponse:
        body = json.dumps(request_payload)
        resp = self._send("/submit", "POST", data=body)
        return RelayerResponse(
            transaction_id=resp["transactionID"],
            state=resp["state"],
            transaction_hash=resp["transactionHash"],
            client=self,
        )

    def deploy(self) -> RelayerResponse:
        safe_factory = self.contracts["SafeContracts"]["SafeFactory"]
        req = {
            "from": self.account.address,
            "to": safe_factory,
            "proxyWallet": self._derive_safe(self.account.address),
            "data": "0x",
            "type": "SAFE-CREATE",
        }
        domain = {
            "name": SAFE_FACTORY_NAME,
            "chainId": self.chain_id,
            "verifyingContract": safe_factory,
        }
        types = {
            "CreateProxy": [
                {"name": "paymentToken", "type": "address"},
                {"name": "payment", "type": "uint256"},
                {"name": "paymentReceiver", "type": "address"},
            ]
        }
        values = {
            "paymentToken": "0x0000000000000000000000000000000000000000",
            "payment": 0,
            "paymentReceiver": "0x0000000000000000000000000000000000000000",
        }
        req["signature"] = self._sign_typed_data(domain, types, values, "CreateProxy")
        req["signatureParams"] = values
        return self._submit(req)

    def execute_single(self, to: str, data: str, metadata: str = "") -> RelayerResponse:
        if self.relay_tx_type == "PROXY":
            return self._execute_proxy_single(to, data, metadata)
        return self._execute_safe_single(to, data, metadata)

    def _execute_proxy_single(self, to: str, data: str, metadata: str) -> RelayerResponse:
        rp = self.get_relay_payload(self.account.address, "PROXY")
        relay_hub = self.contracts["ProxyContracts"]["RelayHub"]
        proxy_factory = self.contracts["ProxyContracts"]["ProxyFactory"]
        gas_price = "0"
        relayer_fee = "0"
        calldata = self._encode_proxy_call(to, data)
        gas_limit = self._estimate_proxy_gas_limit(proxy_factory, calldata)
        proxy_wallet = self._derive_proxy_wallet(self.account.address)
        digest = self._create_proxy_struct_hash(
            self.account.address,
            proxy_factory,
            calldata,
            relayer_fee,
            gas_price,
            gas_limit,
            rp["nonce"],
            relay_hub,
            rp["address"],
        )
        signature = self._sign_message(digest)
        req = {
            "from": self.account.address,
            "to": proxy_factory,
            "proxyWallet": proxy_wallet,
            "data": calldata,
            "nonce": rp["nonce"],
            "signature": signature,
            "signatureParams": {
                "gasPrice": gas_price,
                "gasLimit": gas_limit,
                "relayerFee": relayer_fee,
                "relayHub": relay_hub,
                "relay": rp["address"],
            },
            "type": "PROXY",
            "metadata": metadata or "",
        }
        return self._submit(req)

    def _execute_safe_single(self, to: str, data: str, metadata: str) -> RelayerResponse:
        safe_factory = self.contracts["SafeContracts"]["SafeFactory"]
        safe_address = self._derive_safe(self.account.address)
        if not self.get_deployed(safe_address):
            raise RuntimeError("SAFE_NOT_DEPLOYED")
        nonce_payload = self.get_nonce(self.account.address, "SAFE")
        domain = {"chainId": self.chain_id, "verifyingContract": safe_address}
        types = {
            "SafeTx": [
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "data", "type": "bytes"},
                {"name": "operation", "type": "uint8"},
                {"name": "safeTxGas", "type": "uint256"},
                {"name": "baseGas", "type": "uint256"},
                {"name": "gasPrice", "type": "uint256"},
                {"name": "gasToken", "type": "address"},
                {"name": "refundReceiver", "type": "address"},
                {"name": "nonce", "type": "uint256"},
            ]
        }
        values = {
            "to": to,
            "value": 0,
            "data": data,
            "operation": 0,
            "safeTxGas": 0,
            "baseGas": 0,
            "gasPrice": 0,
            "gasToken": "0x0000000000000000000000000000000000000000",
            "refundReceiver": "0x0000000000000000000000000000000000000000",
            "nonce": int(nonce_payload["nonce"]),
        }
        signature = self._pack_safe_signature(self._sign_typed_data(domain, types, values, "SafeTx"))
        req = {
            "from": self.account.address,
            "to": to,
            "proxyWallet": safe_address,
            "data": data,
            "nonce": nonce_payload["nonce"],
            "signature": signature,
            "signatureParams": {
                "gasPrice": "0",
                "operation": "0",
                "safeTxGas": "0",
                "baseGas": "0",
                "gasToken": "0x0000000000000000000000000000000000000000",
                "refundReceiver": "0x0000000000000000000000000000000000000000",
            },
            "type": "SAFE",
            "metadata": metadata or "",
        }
        return self._submit(req)

    @staticmethod
    def _create_proxy_struct_hash(from_addr: str, to_addr: str, data_hex: str, tx_fee: str, gas_price: str, gas_limit: str, nonce: str, relay_hub: str, relay: str) -> str:
        parts = [
            Web3.to_bytes(text="rlx:"),
            bytes.fromhex(from_addr[2:]),
            bytes.fromhex(to_addr[2:]),
            bytes.fromhex(data_hex[2:]),
            int(tx_fee).to_bytes(32, "big"),
            int(gas_price).to_bytes(32, "big"),
            int(gas_limit).to_bytes(32, "big"),
            int(nonce).to_bytes(32, "big"),
            bytes.fromhex(relay_hub[2:]),
            bytes.fromhex(relay[2:]),
        ]
        return "0x" + keccak(b"".join(parts)).hex()

    @staticmethod
    def _encode_proxy_call(to_addr: str, data_hex: str) -> str:
        abi = [
            {
                "constant": False,
                "inputs": [
                    {
                        "components": [
                            {"name": "typeCode", "type": "uint8"},
                            {"name": "to", "type": "address"},
                            {"name": "value", "type": "uint256"},
                            {"name": "data", "type": "bytes"},
                        ],
                        "name": "calls",
                        "type": "tuple[]",
                    }
                ],
                "name": "proxy",
                "outputs": [{"name": "returnValues", "type": "bytes[]"}],
                "payable": True,
                "stateMutability": "payable",
                "type": "function",
            }
        ]
        contract = Web3().eth.contract(abi=abi)
        return contract.encode_abi("proxy", args=[[(1, Web3.to_checksum_address(to_addr), 0, bytes.fromhex(data_hex[2:]))]])


def build_builder_config_from_env() -> BuilderConfig | None:
    relayer_key = (os.getenv("RELAYER_API_KEY") or "").strip()
    relayer_key_address = (os.getenv("RELAYER_API_KEY_ADDRESS") or "").strip()
    if relayer_key and relayer_key_address:
        return None

    key = (os.getenv("POLY_BUILDER_API_KEY") or "").strip()
    secret = (os.getenv("POLY_BUILDER_SECRET") or "").strip()
    passphrase = (os.getenv("POLY_BUILDER_PASSPHRASE") or "").strip()
    if not key or not secret or not passphrase:
        raise ValueError(
            "Proxy wallet approval needs relayer credentials. Set RELAYER_API_KEY and RELAYER_API_KEY_ADDRESS, or set POLY_BUILDER_API_KEY, POLY_BUILDER_SECRET, and POLY_BUILDER_PASSPHRASE."
        )
    return BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=key,
            secret=secret,
            passphrase=passphrase,
        )
    )
