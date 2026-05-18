from typing import Any

from py_clob_client.client import ClobClient


class CryptoManager:
    def derive_api_keys(self, private_key: str, options: dict[str, Any] | None = None):
        host = "https://clob.polymarket.com"
        chain_id = 137
        formatted = private_key if private_key.startswith("0x") else f"0x{private_key}"
        client = ClobClient(
            host,
            chain_id=chain_id,
            key=formatted,
            signature_type=(options or {}).get("signatureType"),
            funder=(options or {}).get("funderAddress") or None,
        )
        try:
            creds = client.create_or_derive_api_creds()
            return {
                "key": creds.api_key,
                "secret": creds.api_secret,
                "passphrase": creds.api_passphrase,
            }
        except Exception:
            print("Error deriving API keys.")
            raise
