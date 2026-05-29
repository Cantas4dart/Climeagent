import json
import os
import inspect
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import HTTPProvider, Web3

from .relayer import RelayClient, build_builder_config_from_env

try:
    from py_clob_client_v2 import (
        ApiCreds,
        ClobClient,
        MarketOrderArgs,
        OrderArgs,
        OrderType,
        PartialCreateOrderOptions,
        Side,
    )
    from py_clob_client_v2.order_builder.constants import COLLATERAL as COLLATERAL_ASSET_TYPE
    BalanceAllowanceParams = None
    V2_CLOB_CLIENT = True
except ImportError:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        AssetType,
        ApiCreds,
        BalanceAllowanceParams,
        MarketOrderArgs,
        OrderArgs,
        OrderType,
        PartialCreateOrderOptions,
    )
    Side = None
    COLLATERAL_ASSET_TYPE = AssetType.COLLATERAL
    V2_CLOB_CLIENT = hasattr(ClobClient, "create_and_post_order") or hasattr(ClobClient, "create_market_order")

load_dotenv()

POLYMARKET_ALLOWANCE_SPENDERS = [
    "0xAdA100Db00Ca00073811820692005400218FcE1f",  # CTF Collateral Adapter
    "0xadA2005600Dec949baf300f4C6120000bDB6eAab",  # NegRisk CTF Collateral Adapter
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",  # Neg Risk Adapter
    "0xE111180000d2663C0091e4f400237545B87B996B",  # CTF Exchange V2
]


def get_polygon_rpc_candidates() -> list[str]:
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


def _to_plain(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    if hasattr(value, "__dict__") and not isinstance(value, (str, bytes, int, float, bool, type(None))):
        return {k: _to_plain(v) for k, v in value.__dict__.items()}
    return value


def extract_allowance_amount(balance_data: dict[str, Any]) -> float:
    direct_allowance = balance_data.get("allowance")
    try:
        if direct_allowance is not None:
            return float(direct_allowance)
    except (TypeError, ValueError):
        pass

    allowances = balance_data.get("allowances") or {}
    for spender in POLYMARKET_ALLOWANCE_SPENDERS:
        try:
            value = allowances.get(spender)
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue

    return 0.0


class PolyMarketAPI:
    def __init__(
        self,
        creds: dict[str, str],
        private_key: str | None = None,
        options: dict[str, Any] | None = None,
    ):
        self.data_api_url = "https://data-api.polymarket.com"
        self.gamma_api_url = "https://gamma-api.polymarket.com"
        self.market_lookup_cache: dict[str, str] = {}
        self.private_key = private_key
        self.funder_address = (options or {}).get("funderAddress") or None
        self.signature_type = (options or {}).get("signatureType")
        self.usdc_address = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        self.pusd_address = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
        self.collateral_onramp_address = "0x93070a847efEf7F70739046A929D47a521F5B8ee"
        self.collateral_offramp_address = "0x2957922Eb93258b93368531d39fAcCA3B4dC5854"
        self.conditional_tokens_address = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
        self.client: ClobClient | None = None
        self._init_client(creds, private_key)

    def _init_client(self, creds: dict[str, str], private_key: str | None):
        host = "https://clob.polymarket.com"
        chain_id = 137
        key = None
        if private_key:
            key = private_key if private_key.startswith("0x") else f"0x{private_key}"
        api_creds = ApiCreds(
            api_key=creds.get("key", ""),
            api_secret=creds.get("secret", ""),
            api_passphrase=creds.get("passphrase", ""),
        )
        self.client = ClobClient(
            host,
            chain_id=chain_id,
            key=key,
            creds=api_creds,
            signature_type=self.signature_type,
            funder=self.funder_address,
        )

    @staticmethod
    def _resolve_side(side: str):
        normalized = str(side or "").upper()
        if Side is None:
            return normalized
        return getattr(Side, normalized)

    def _get_signer_account(self):
        if not self.private_key:
            raise ValueError("Private Key not found")
        formatted = self.private_key if self.private_key.startswith("0x") else f"0x{self.private_key}"
        return Account.from_key(formatted)

    def get_signer_address(self) -> str:
        return self._get_signer_account().address

    def get_configured_funder_address(self) -> str:
        return self.funder_address or self.get_signer_address()

    def _resolve_relay_tx_type(self) -> str:
        """
        Polymarket docs map signature types as:
        0 = EOA
        1 = POLY_PROXY
        2 = GNOSIS_SAFE
        3 = POLY_1271
        """
        if self.signature_type == 1:
            return "PROXY"
        return "SAFE"

    def test_relayer_connection(self) -> dict[str, Any]:
        if self.signature_type is None or self.signature_type == 0:
            raise ValueError("Relayer connection test requires signature_type to be configured for POLY_PROXY or GNOSIS_SAFE.")

        relayer_url = (os.getenv("POLY_RELAYER_URL") or "https://relayer-v2.polymarket.com/").strip()
        relay_tx_type = self._resolve_relay_tx_type()
        relay_client = RelayClient(
            relayer_url=relayer_url,
            chain_id=137,
            private_key=self.private_key or "",
            builder_config=build_builder_config_from_env(),
            relay_tx_type=relay_tx_type,
            relayer_api_key=(os.getenv("RELAYER_API_KEY") or "").strip() or None,
            relayer_api_key_address=(os.getenv("RELAYER_API_KEY_ADDRESS") or "").strip() or None,
        )
        return relay_client.test_connection()

    def _build_web3(self, rpc_url: str) -> Web3:
        return Web3(HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))

    def _build_signer_clients(self) -> tuple[LocalAccount, Web3]:
        signer = self._get_signer_account()
        last_error = None
        for rpc_url in get_polygon_rpc_candidates():
            try:
                web3 = self._build_web3(rpc_url)
                web3.eth.chain_id
                return signer, web3
            except Exception as exc:
                last_error = exc
                print(f"[POLY] RPC signer init failed on {rpc_url}: {exc}")
        if last_error:
            raise last_error
        raise RuntimeError("All Polygon RPC endpoints failed.")

    def get_wallet_pusd_balance(self, address: str) -> int:
        abi = [
            {
                "constant": True,
                "inputs": [{"name": "owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "", "type": "uint256"}],
                "type": "function",
            }
        ]
        last_error = None
        for rpc_url in get_polygon_rpc_candidates():
            try:
                web3 = self._build_web3(rpc_url)
                contract = web3.eth.contract(address=Web3.to_checksum_address(self.pusd_address), abi=abi)
                return int(contract.functions.balanceOf(Web3.to_checksum_address(address)).call())
            except Exception as exc:
                last_error = exc
                print(f"[POLY] RPC read failed on {rpc_url}: {exc}")
        if last_error:
            raise last_error
        raise RuntimeError("All Polygon RPC endpoints failed.")

    def get_wallet_usdc_balance(self, address: str) -> int:
        return self.get_wallet_pusd_balance(address)

    def get_balance(self, retries: int = 3) -> dict[str, Any]:
        if not self.client:
            raise ValueError("Client not initialized")
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                if V2_CLOB_CLIENT:
                    if BalanceAllowanceParams is not None:
                        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                        if hasattr(self.client, "update_balance_allowance"):
                            self.client.update_balance_allowance(params)
                        return _to_plain(self.client.get_balance_allowance(params))
                    try:
                        return _to_plain(self.client.get_balance_allowance(COLLATERAL_ASSET_TYPE))
                    except TypeError:
                        return _to_plain(self.client.get_balance_allowance())
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                self.client.update_balance_allowance(params)
                return _to_plain(self.client.get_balance_allowance(params))
            except Exception as exc:
                last_error = exc
                if attempt == retries:
                    break
                print(f"[POLY] Balance fetch failed (Attempt {attempt}/{retries}): {exc}. Retrying...")
                time.sleep(2)
        raise last_error or RuntimeError("Failed to fetch balance after retries")

    def approve_collateral(self):
        if not self.client:
            raise ValueError("Client not initialized")

        spenders = [
            *POLYMARKET_ALLOWANCE_SPENDERS,
            self.collateral_onramp_address,
            self.collateral_offramp_address,
        ]
        abi = [
            {
                "constant": False,
                "inputs": [
                    {"name": "spender", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                ],
                "name": "approve",
                "outputs": [{"name": "", "type": "bool"}],
                "type": "function",
            }
        ]
        infinite = (1 << 256) - 1

        if self.signature_type and self.signature_type != 0:
            if self.signature_type == 3:
                raise RuntimeError(
                    "Deposit wallet approvals (POLY_1271) must be submitted as a relayer WALLET batch from the deposit wallet. "
                    "See https://docs.polymarket.com/trading/deposit-wallets for the correct flow."
                )

            print(f"[POLY] Approve: signature_type={self.signature_type}, funder_address={self.funder_address}")
            print("[POLY] Using RELAYER PATH")
            relayer_url = (os.getenv("POLY_RELAYER_URL") or "https://relayer-v2.polymarket.com/").strip()
            print(f"[POLY] Debug: relayer_url = '{relayer_url}'")
            print(f"[POLY] Debug: signature_type = {self.signature_type}, funder_address = {self.funder_address}")
            print(f"[POLY] Debug: private_key length = {len(self.private_key or '')}")
            relay_tx_type = self._resolve_relay_tx_type()
            print(f"[POLY] Debug: relay_tx_type = {relay_tx_type}")
            relay_client = RelayClient(
                relayer_url=relayer_url,
                chain_id=137,
                private_key=self.private_key or "",
                builder_config=build_builder_config_from_env(),
                relay_tx_type=relay_tx_type,
                relayer_api_key=(os.getenv("RELAYER_API_KEY") or "").strip() or None,
                relayer_api_key_address=(os.getenv("RELAYER_API_KEY_ADDRESS") or "").strip() or None,
            )

            if relay_tx_type == "SAFE":
                safe_address = relay_client._derive_safe(relay_client.account.address)
                if not relay_client.get_deployed(safe_address):
                    print(f"[POLY] Deploying safe for funder {self.funder_address} via relayer...")
                    deployment = relay_client.deploy()
                    deployment_result = deployment.wait()
                    if not deployment_result:
                        raise RuntimeError("Safe deployment failed through relayer.")

            contract = Web3().eth.contract(abi=abi)
            tx_hashes = []
            for spender in spenders:
                calldata = contract.encode_abi("approve", args=[Web3.to_checksum_address(spender), infinite])
                print(f"[POLY] Sending gasless approval for spender {spender} via relayer...")
                response = relay_client.execute_single(
                    Web3.to_checksum_address(self.pusd_address),
                    calldata,
                    "Approve Polymarket spender",
                )
                result = response.wait()
                if not result or not result.get("transactionHash"):
                    raise RuntimeError("Gasless approval did not confirm.")
                tx_hashes.append(result["transactionHash"])
            return tx_hashes

        print("[POLY] Using DIRECT SIGNER PATH - sending raw transactions")
        signer, web3 = self._build_signer_clients()
        print(f"[POLY] Signer address: {signer.address}")
        contract = web3.eth.contract(
            address=Web3.to_checksum_address(self.pusd_address),
            abi=abi,
        )
        spenders = [
            *POLYMARKET_ALLOWANCE_SPENDERS,
            self.collateral_onramp_address,
            self.collateral_offramp_address,
        ]
        nonce = web3.eth.get_transaction_count(signer.address)
        gas_price = web3.eth.gas_price
        tx_hashes = []

        print(f"[POLY] Sending direct approvals for {signer.address}...")
        for spender in spenders:
            txn = contract.functions.approve(
                Web3.to_checksum_address(spender),
                infinite,
            ).build_transaction(
                {
                    "from": signer.address,
                    "nonce": nonce,
                    "chainId": 137,
                    "gas": 120000,
                    "gasPrice": gas_price,
                }
            )
            signed = web3.eth.account.sign_transaction(txn, private_key=signer.key)
            tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
            tx_hashes.append(Web3.to_hex(tx_hash))
            nonce += 1
            time.sleep(1)
        return tx_hashes

    def approve_usdc(self):
        return self.approve_collateral()

    def transfer_pusd(self, recipient: str, amount: float):
        if not recipient:
            raise ValueError("No recipient address was provided.")
        signer, web3 = self._build_signer_clients()
        signer_address = str(signer.address)
        recipient_address = str(recipient)
        if signer_address.lower() == recipient_address.lower():
            raise ValueError("Signer wallet and recipient wallet are already the same address.")
        if not (amount > 0):
            raise ValueError("Amount must be greater than zero.")

        contract = web3.eth.contract(
            address=Web3.to_checksum_address(self.pusd_address),
            abi=[
                {
                    "constant": False,
                    "inputs": [
                        {"name": "to", "type": "address"},
                        {"name": "amount", "type": "uint256"},
                    ],
                    "name": "transfer",
                    "outputs": [{"name": "", "type": "bool"}],
                    "type": "function",
                }
            ],
        )
        amount_units = int(amount * 1_000_000)
        if amount_units <= 0:
            raise ValueError("Amount is too small to transfer in pUSD units.")
        txn = contract.functions.transfer(
            Web3.to_checksum_address(recipient_address),
            amount_units,
        ).build_transaction(
            {
                "from": signer.address,
                "nonce": web3.eth.get_transaction_count(signer.address),
                "chainId": 137,
                "gas": 120000,
                "gasPrice": web3.eth.gas_price,
            }
        )
        signed = web3.eth.account.sign_transaction(txn, private_key=signer.key)
        tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
        return Web3.to_hex(tx_hash)

    def transfer_pusd_to_funder(self, amount: float):
        if not self.funder_address:
            raise ValueError("No funder address is configured for this user.")
        return self.transfer_pusd(self.funder_address, amount)

    def transfer_usdc_to_funder(self, amount: float):
        return self.transfer_pusd_to_funder(amount)

    def get_positions(self, user_address: str):
        url = f"{self.data_api_url}/positions"
        response = requests.get(url, params={"user": user_address}, timeout=20)
        response.raise_for_status()
        return response.json()

    def get_open_orders(self):
        if not self.client:
            raise ValueError("Client not initialized")
        return _to_plain(self.client.get_orders())

    def _require_v2_order_client(self):
        if not V2_CLOB_CLIENT:
            raise RuntimeError(
                "Polymarket CLOB order placement requires a py-clob-client build with order helpers. "
                "Install dependencies from requirements.txt before running live trading."
            )
        if not hasattr(self.client, "get_order_book"):
            raise RuntimeError(
                "Polymarket CLOB order placement client is missing required methods: get_order_book"
            )
        has_one_step_order = hasattr(self.client, "create_and_post_order") or hasattr(
            self.client,
            "create_and_post_market_order",
        )
        has_two_step_order = hasattr(self.client, "post_order") and (
            hasattr(self.client, "create_order") or hasattr(self.client, "create_market_order")
        )
        if not has_one_step_order and not has_two_step_order:
            raise RuntimeError(
                "Polymarket CLOB order placement client is missing required order submission methods"
            )

    def _submit_limit_order(self, order, options):
        create_and_post = getattr(self.client, "create_and_post_order", None)
        if create_and_post:
            parameter_count = len(inspect.signature(create_and_post).parameters)
            if parameter_count >= 3:
                return create_and_post(order, options, OrderType.GTC)
            return create_and_post(order, options)

        signed_order = self.client.create_order(order, options)
        return self.client.post_order(signed_order, OrderType.GTC)

    def _submit_market_order(self, order, options):
        create_and_post_market = getattr(self.client, "create_and_post_market_order", None)
        if create_and_post_market:
            parameter_count = len(inspect.signature(create_and_post_market).parameters)
            if parameter_count >= 3:
                return create_and_post_market(order, options, OrderType.FAK)
            return create_and_post_market(order, options)

        create_market_order = getattr(self.client, "create_market_order", None)
        if create_market_order:
            signed_order = create_market_order(order, options)
        else:
            signed_order = self.client.create_order(order, options)
        return self.client.post_order(signed_order, OrderType.FAK)

    def place_limit_order(self, token_id: str, side: str, price: float, size: float):
        if not self.client:
            raise ValueError("Client not initialized")
        self._require_v2_order_client()
        print(f"[POLY] Placing {side} order for {token_id}: {size} shares @ {price}")
        try:
            book = self.client.get_order_book(token_id)
            tick = str(getattr(book, "tick_size", None) or self.client.get_tick_size(token_id))
            neg_risk = getattr(book, "neg_risk", None)
            if neg_risk is None:
                neg_risk = self.client.get_neg_risk(token_id)

            order = OrderArgs(token_id=token_id, price=price, size=float(size), side=self._resolve_side(side))
            options = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
            response = self._submit_limit_order(order, options)
            result = _to_plain(response)
            if not result.get("success") or not result.get("orderID"):
                details = result.get("errorMsg") or result.get("error") or result.get("status") or str(result)
                raise RuntimeError(f"Order was not accepted by Polymarket: {details}")
            print(f"[POLY] Order Submitted Successfully: {result.get('orderID')} ({result.get('status', 'unknown')})")
            return result
        except Exception as exc:
            print(f"[POLY] Order Failed: {exc}")
            raise

    def place_market_order(self, token_id: str, side: str, amount: float):
        if not self.client:
            raise ValueError("Client not initialized")
        self._require_v2_order_client()
        print(f"[POLY] Placing {side} market order for {token_id}: amount={amount}")
        try:
            book = self.client.get_order_book(token_id)
            tick = str(getattr(book, "tick_size", None) or self.client.get_tick_size(token_id))
            neg_risk = getattr(book, "neg_risk", None)
            if neg_risk is None:
                neg_risk = self.client.get_neg_risk(token_id)

            options = PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk)
            worst_price = 0.99 if str(side).upper() == "BUY" else 0.01
            order = MarketOrderArgs(
                token_id=token_id,
                amount=float(amount),
                side=self._resolve_side(side),
                price=worst_price,
                order_type=OrderType.FAK,
            )
            response = self._submit_market_order(order, options)
            result = _to_plain(response)
            if not result.get("success") or not result.get("orderID"):
                details = result.get("errorMsg") or result.get("error") or result.get("status") or str(result)
                raise RuntimeError(f"Market order was not accepted by Polymarket: {details}")
            print(
                f"[POLY] Market Order Submitted Successfully: "
                f"{result.get('orderID')} ({result.get('status', 'unknown')})"
            )
            return result
        except Exception as exc:
            print(f"[POLY] Market Order Failed: {exc}")
            raise

    def get_market_by_condition_id(self, condition_id: str):
        url = f"{self.gamma_api_url}/markets"
        for attempt in range(1, 4):
            try:
                response = requests.get(url, params={"conditionId": condition_id}, timeout=20)
                response.raise_for_status()
                markets = response.json() if isinstance(response.json(), list) else []
                exact = next(
                    (
                        market
                        for market in markets
                        if str(market.get("conditionId", "")).lower() == str(condition_id).lower()
                    ),
                    None,
                )
                return exact or (markets[0] if markets else None)
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(attempt)
        return None

    def get_market_by_id(self, market_id: str):
        url = f"{self.gamma_api_url}/markets/{market_id}"
        for attempt in range(1, 4):
            try:
                response = requests.get(url, timeout=20)
                response.raise_for_status()
                return response.json()
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(attempt)
        return None

    def get_public_profile_by_wallet(self, address: str):
        url = f"{self.gamma_api_url}/public-profile"
        response = requests.get(url, params={"address": address}, timeout=20)
        response.raise_for_status()
        return response.json()

    def redeem_winnings(self, condition_id: str):
        signer, web3 = self._build_signer_clients()
        contract = web3.eth.contract(
            address=Web3.to_checksum_address(self.conditional_tokens_address),
            abi=[
                {
                    "constant": False,
                    "inputs": [
                        {"name": "collateralToken", "type": "address"},
                        {"name": "parentCollectionId", "type": "bytes32"},
                        {"name": "conditionId", "type": "bytes32"},
                        {"name": "indexSets", "type": "uint256[]"},
                    ],
                    "name": "redeemPositions",
                    "outputs": [],
                    "type": "function",
                }
            ],
        )
        zero_bytes32 = "0x" + ("0" * 64)
        print(f"[POLY] Redeeming winnings for condition {condition_id}...")
        txn = contract.functions.redeemPositions(
            Web3.to_checksum_address(self.usdc_address),
            zero_bytes32,
            condition_id,
            [1, 2],
        ).build_transaction(
            {
                "from": signer.address,
                "nonce": web3.eth.get_transaction_count(signer.address),
                "chainId": 137,
                "gas": 250000,
                "gasPrice": web3.eth.gas_price,
            }
        )
        signed = web3.eth.account.sign_transaction(txn, private_key=signer.key)
        tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
        return Web3.to_hex(tx_hash)
