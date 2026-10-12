"""Local-only live Solana swap execution through Jupiter Swap API V2.

Security properties:
- Private key is read from environment/.env and never returned by health/config APIs.
- Public address is derived from the signer and optionally cross-checked against the
  configured Phantom address.
- No transaction is submitted unless the Jupiter order contains a valid transaction,
  the quote is within the entry price-deviation guard, and the wallet has enough SOL
  while preserving the configured native-SOL reserve.
- Execute is never blindly retried after a transport failure. Ambiguous results require
  reconciliation and trigger SAFE_STOP in PositionManager.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

from paper_execution import Fill

log = logging.getLogger("grok6.solana_live")

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000
JUPITER_BASE = "https://api.jup.ag/swap/v2"
DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/" + SOL_MINT
_PLACEHOLDERS = {
    "PASTE_YOUR_BASE58_PRIVATE_KEY_HERE", "PASTE_PRIVATE_KEY_HERE",
    "YOUR_PRIVATE_KEY", "YOUR_SOLANA_PRIVATE_KEY", "CHANGE_ME",
}
_ADDRESS_PLACEHOLDERS = {
    "PASTE_YOUR_PUBLIC_SOLANA_ADDRESS_HERE", "YOUR_SOLANA_PUBLIC_ADDRESS",
    "PASTE_YOUR_SOLANA_ADDRESS_HERE", "CHANGE_ME",
}


def configured_private_key() -> str:
    raw = os.environ.get("GROK6_SOLANA_PRIVATE_KEY", "").strip()
    if not raw or raw.upper() in _PLACEHOLDERS:
        return ""
    return raw


class ExecutionFailure(RuntimeError):
    def __init__(self, reason: str, ambiguous: bool = False, context: Optional[dict] = None):
        super().__init__(reason)
        self.reason = reason
        self.ambiguous = bool(ambiguous)
        self.context = dict(context or {})


class SolanaLiveExecutor:
    """Synchronous live executor for Jupiter V2 Meta-Aggregator order/execute."""
    mode = "LIVE"

    def __init__(self, cfg, *, session=None, timeout: Optional[float] = None):
        self.cfg = cfg
        self.timeout = max(2.0, float(timeout or cfg.provider_timeout_seconds))
        self.session = session or requests.Session()
        self.api_key = os.environ.get("JUPITER_API_KEY", "").strip()
        configured_rpc = os.environ.get("SOLANA_RPC_URL", "").strip()
        helius_key = os.environ.get("HELIUS_API_KEY", "").strip()
        # Prefer the user's configured Helius endpoint when they set its key but
        # leave SOLANA_RPC_URL blank. An explicit RPC URL always takes precedence.
        self.rpc_url = (configured_rpc
                        or (f"https://mainnet.helius-rpc.com/?api-key={helius_key}" if helius_key
                            else "https://api.mainnet-beta.solana.com"))
        self.public_address = ""
        self.keypair = None
        self.ready = False
        self.reason = "PRIVATE_KEY_NOT_CONFIGURED"
        self._secret_present = bool(configured_private_key())
        self._lock = threading.RLock()
        self._mint_decimals: dict[str, int] = {}
        self._sol_price_cache: tuple[float, float] | None = None
        self.max_trade_usd = _float_env("GROK6_LIVE_MAX_TRADE_USD", 2.0, minimum=0.01, maximum=5.0)
        self.min_sol_reserve = _float_env("GROK6_LIVE_MIN_SOL_RESERVE", 0.03, minimum=0.005, maximum=1.0)
        self.max_entry_deviation_pct = _float_env("GROK6_LIVE_MAX_PRICE_DEVIATION_PCT", 0.12, minimum=0.01, maximum=0.50)
        self.estimated_tx_fee_usd = _float_env("GROK6_LIVE_ESTIMATED_TX_FEE_USD", 0.01, minimum=0.0, maximum=0.25)
        self._requests = 0
        self._last_execution_ts: float | None = None
        self._last_error: str | None = None
        self._last_signature: str | None = None

        if not self._secret_present:
            return
        try:
            self.keypair = self._load_keypair(configured_private_key())
            self.public_address = str(self.keypair.pubkey())
        except Exception as exc:
            self.reason = f"PRIVATE_KEY_INVALID:{type(exc).__name__}"
            self._last_error = self.reason
            return

        expected = os.environ.get("GROK6_PHANTOM_PUBLIC_ADDRESS", "").strip()
        if expected and expected.upper() not in {x.upper() for x in _ADDRESS_PLACEHOLDERS}:
            if expected != self.public_address:
                self.reason = "PUBLIC_ADDRESS_PRIVATE_KEY_MISMATCH"
                self._last_error = self.reason
                return
        if not self.api_key:
            self.reason = "JUPITER_API_KEY_NOT_CONFIGURED"
            self._last_error = self.reason
            return
        if not self.rpc_url.startswith(("https://", "http://")):
            self.reason = "SOLANA_RPC_URL_INVALID"
            self._last_error = self.reason
            return
        self.ready = True
        self.reason = "READY_SIGNER_AND_JUPITER_CONFIGURED"

    @staticmethod
    def _load_keypair(raw: str):
        from solders.keypair import Keypair
        value = raw.strip()
        if value.startswith("["):
            data = json.loads(value)
            if not isinstance(data, list) or len(data) not in {32, 64}:
                raise ValueError("keypair JSON array must contain 32 or 64 bytes")
            numbers = bytes(int(n) for n in data)
            if len(numbers) == 32:
                return Keypair.from_seed(numbers)
            return Keypair.from_bytes(numbers)
        # Phantom's exported private key is normally base58-encoded 64-byte keypair.
        return Keypair.from_base58_string(value)

    @property
    def secret_present(self) -> bool:
        return self._secret_present

    def health(self) -> dict:
        return {
            "state": "READY" if self.ready else ("NOT_CONFIGURED" if not self._secret_present else "BLOCKED"),
            "configured": self._secret_present,
            "signer_valid": self.keypair is not None,
            "public_address": self.public_address or None,
            "jupiter_api_key_configured": bool(self.api_key),
            "ready": self.ready,
            "reason": self.reason,
            "requests_made": self._requests,
            "last_execution_ts": self._last_execution_ts,
            "last_signature": self._last_signature,
            "last_error": self._last_error,
            "max_trade_usd": self.max_trade_usd,
            "min_sol_reserve": self.min_sol_reserve,
            "note": "LIVE transactions are enabled only when signer/address/API configuration validates.",
        }

    def _require_ready(self):
        if not self.ready or self.keypair is None:
            raise ExecutionFailure("LIVE_EXECUTOR_NOT_READY:" + self.reason)

    def _rpc(self, method: str, params: list) -> dict:
        try:
            response = self.session.post(
                self.rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or body.get("error") or "result" not in body:
                raise RuntimeError("RPC_INVALID_RESPONSE")
            return body["result"]
        except Exception as exc:
            raise ExecutionFailure(f"SOLANA_RPC_{method}_FAILED:{type(exc).__name__}") from None

    def _sol_balance(self) -> float:
        result = self._rpc("getBalance", [self.public_address, {"commitment": "confirmed"}])
        try:
            return float(result["value"]) / LAMPORTS_PER_SOL
        except (KeyError, TypeError, ValueError):
            raise ExecutionFailure("SOLANA_RPC_BALANCE_INVALID") from None

    def _mint_decimals_for(self, mint: str) -> int:
        if mint in self._mint_decimals:
            return self._mint_decimals[mint]
        result = self._rpc("getTokenSupply", [mint, {"commitment": "confirmed"}])
        try:
            decimals = int(result["value"]["decimals"])
        except (KeyError, TypeError, ValueError):
            raise ExecutionFailure("TOKEN_DECIMALS_UNAVAILABLE", context={"mint": mint}) from None
        if not 0 <= decimals <= 18:
            raise ExecutionFailure("TOKEN_DECIMALS_OUT_OF_RANGE", context={"mint": mint})
        self._mint_decimals[mint] = decimals
        return decimals

    def _token_balance_raw(self, mint: str) -> int:
        result = self._rpc("getTokenAccountsByOwner", [
            self.public_address, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}
        ])
        total = 0
        try:
            for row in result.get("value", []):
                amount = row["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]
                total += int(amount)
            return total
        except (KeyError, TypeError, ValueError):
            raise ExecutionFailure("TOKEN_BALANCE_RESPONSE_INVALID", context={"mint": mint}) from None

    def _sol_usd(self, *, force: bool = False) -> float:
        now = time.time()
        if not force and self._sol_price_cache and now - self._sol_price_cache[1] <= 5.0:
            return self._sol_price_cache[0]
        try:
            response = self.session.get(DEXSCREENER_TOKEN_URL, timeout=self.timeout,
                                        headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
            pairs = payload.get("pairs", []) if isinstance(payload, dict) else []
            candidates = []
            for pair in pairs if isinstance(pairs, list) else []:
                if str(pair.get("chainId", "")).lower() != "solana":
                    continue
                base_address = (pair.get("baseToken") or {}).get("address")
                if base_address != SOL_MINT:
                    continue
                try:
                    price = float(pair.get("priceUsd"))
                    liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
                except (TypeError, ValueError):
                    continue
                if price > 0 and liquidity > 0:
                    candidates.append((liquidity, price))
            if not candidates:
                raise ValueError("SOL_USD_PRICE_MISSING")
            price = max(candidates)[1]
            self._sol_price_cache = (price, now)
            return price
        except Exception as exc:
            self._last_error = f"SOL_USD_PRICE_UNAVAILABLE:{type(exc).__name__}"
            raise ExecutionFailure(self._last_error) from None

    def _sign_transaction(self, transaction_b64: str) -> str:
        try:
            from solders.message import to_bytes_versioned
            from solders.transaction import VersionedTransaction
            raw = VersionedTransaction.from_bytes(base64.b64decode(transaction_b64, validate=True))
            required = int(raw.message.header.num_required_signatures)
            required_keys = list(raw.message.account_keys[:required])
            try:
                signer_index = required_keys.index(self.keypair.pubkey())
            except ValueError:
                raise ValueError("WALLET_NOT_REQUIRED_SIGNER") from None
            signatures = list(raw.signatures)
            if len(signatures) != required:
                raise ValueError("TRANSACTION_SIGNATURE_SLOT_MISMATCH")
            signatures[signer_index] = self.keypair.sign_message(to_bytes_versioned(raw.message))
            signed = VersionedTransaction.populate(raw.message, signatures)
            return base64.b64encode(bytes(signed)).decode("ascii")
        except Exception as exc:
            raise ExecutionFailure(f"TRANSACTION_SIGNING_FAILED:{type(exc).__name__}") from None

    def _order_and_execute(self, input_mint: str, output_mint: str, amount_raw: int) -> dict:
        self._require_ready()
        if amount_raw <= 0:
            raise ExecutionFailure("SWAP_AMOUNT_INVALID")
        params = {"inputMint": input_mint, "outputMint": output_mint,
                  "amount": str(int(amount_raw)), "taker": self.public_address}
        try:
            order_response = self.session.get(
                f"{JUPITER_BASE}/order", params=params,
                headers={"Accept": "application/json", "x-api-key": self.api_key}, timeout=self.timeout,
            )
            self._requests += 1
            order_response.raise_for_status()
            order = order_response.json()
            if not isinstance(order, dict):
                raise ValueError("ORDER_RESPONSE_NOT_OBJECT")
        except Exception as exc:
            raise ExecutionFailure(f"JUPITER_ORDER_FAILED:{type(exc).__name__}") from None
        transaction_b64 = order.get("transaction")
        request_id = order.get("requestId")
        if not transaction_b64 or not request_id:
            code = str(order.get("errorCode") or "NO_TRANSACTION")
            detail = str(order.get("errorMessage") or order.get("error") or "").replace("\n", " ")[:120]
            raise ExecutionFailure("JUPITER_ORDER_REJECTED:" + code + (":" + detail if detail else ""),
                                   context={"router": order.get("router")})
        signed = self._sign_transaction(str(transaction_b64))
        body = {"signedTransaction": signed, "requestId": request_id}
        try:
            # DO NOT retry this POST on a transport failure: Jupiter may already have
            # landed the transaction. An ambiguous result freezes the bot for reconcile.
            response = self.session.post(
                f"{JUPITER_BASE}/execute", json=body,
                headers={"Accept": "application/json", "Content-Type": "application/json",
                         "x-api-key": self.api_key}, timeout=self.timeout,
            )
            self._requests += 1
        except Exception as exc:
            raise ExecutionFailure(f"JUPITER_EXECUTE_RESULT_AMBIGUOUS:{type(exc).__name__}",
                                   ambiguous=True, context={"request_id": request_id,
                                                            "mint_in": input_mint, "mint_out": output_mint}) from None
        try:
            response.raise_for_status()
            result = response.json()
        except Exception as exc:
            ambiguous = int(getattr(response, "status_code", 0) or 0) >= 500
            raise ExecutionFailure(f"JUPITER_EXECUTE_RESPONSE_INVALID:{type(exc).__name__}",
                                   ambiguous=ambiguous,
                                   context={"request_id": request_id, "http_status": getattr(response, "status_code", None)}) from None
        if not isinstance(result, dict):
            raise ExecutionFailure("JUPITER_EXECUTE_RESPONSE_NOT_OBJECT", ambiguous=True,
                                   context={"request_id": request_id})
        if str(result.get("status", "")).lower() != "success" or int(result.get("code", 0) or 0) != 0:
            raise ExecutionFailure("JUPITER_EXECUTION_FAILED:" + str(result.get("error") or result.get("code") or result.get("status"))[:120],
                                   context={"request_id": request_id, "status": result.get("status"),
                                            "code": result.get("code")})
        input_amount = _positive_int(result.get("inputAmountResult")) or _positive_int(result.get("totalInputAmount"))
        output_amount = _positive_int(result.get("outputAmountResult")) or _positive_int(result.get("totalOutputAmount"))
        if not input_amount or not output_amount:
            raise ExecutionFailure("JUPITER_CONFIRMED_BUT_AMOUNT_UNVERIFIED", ambiguous=True,
                                   context={"request_id": request_id, "signature": result.get("signature")})
        signature = str(result.get("signature") or "")
        if not signature:
            raise ExecutionFailure("JUPITER_CONFIRMED_BUT_SIGNATURE_MISSING", ambiguous=True,
                                   context={"request_id": request_id})
        self._last_execution_ts = time.time()
        self._last_signature = signature
        self._last_error = None
        return {"input_raw": input_amount, "output_raw": output_amount,
                "signature": signature, "request_id": request_id, "router": order.get("router")}

    def execute_entry(self, mint: str, ref_price: float, qty: float) -> Optional[Fill]:
        """Buy a token using native SOL; qty is only used to calculate target USD spend."""
        with self._lock:
            self._require_ready()
            if not mint or mint == SOL_MINT or ref_price <= 0 or qty <= 0:
                raise ExecutionFailure("LIVE_ENTRY_INPUT_INVALID")
            sol_usd = self._sol_usd()
            desired_usd = min(float(qty) * float(ref_price), self.max_trade_usd,
                              float(self.cfg.max_position_notional))
            if desired_usd <= 0:
                raise ExecutionFailure("LIVE_ENTRY_NOTIONAL_INVALID")
            sol_amount = desired_usd / sol_usd
            raw_amount = int(sol_amount * LAMPORTS_PER_SOL)
            if raw_amount <= 0:
                raise ExecutionFailure("LIVE_ENTRY_ROUNDS_TO_ZERO")
            balance_sol = self._sol_balance()
            if balance_sol < sol_amount + self.min_sol_reserve:
                raise ExecutionFailure("INSUFFICIENT_SOL_AFTER_RESERVE",
                                       context={"required_sol_including_reserve": round(sol_amount + self.min_sol_reserve, 9),
                                                "available_sol": round(balance_sol, 9)})
            decimals = self._mint_decimals_for(mint)
            # Get a live order quote first, but do not sign it until verifying price
            # deviation versus the strategy's USD reference price.
            params = {"inputMint": SOL_MINT, "outputMint": mint,
                      "amount": str(raw_amount), "taker": self.public_address}
            try:
                response = self.session.get(
                    f"{JUPITER_BASE}/order", params=params,
                    headers={"Accept": "application/json", "x-api-key": self.api_key}, timeout=self.timeout,
                )
                self._requests += 1
                response.raise_for_status()
                order = response.json()
            except Exception as exc:
                raise ExecutionFailure(f"JUPITER_ORDER_FAILED:{type(exc).__name__}") from None
            if not isinstance(order, dict) or not order.get("transaction") or not order.get("requestId"):
                detail = str((order or {}).get("errorMessage") or (order or {}).get("error") or "NO_TRANSACTION")[:100] if isinstance(order, dict) else "INVALID_RESPONSE"
                raise ExecutionFailure("JUPITER_ORDER_REJECTED:" + detail)
            try:
                quoted_output_raw = int(order.get("outAmount") or 0)
            except (TypeError, ValueError):
                quoted_output_raw = 0
            quoted_qty = quoted_output_raw / (10 ** decimals) if quoted_output_raw > 0 else 0.0
            if quoted_qty <= 0:
                raise ExecutionFailure("JUPITER_QUOTE_OUTPUT_INVALID")
            estimated_fill_price = desired_usd / quoted_qty
            deviation = abs(estimated_fill_price / float(ref_price) - 1.0)
            if deviation > self.max_entry_deviation_pct:
                raise ExecutionFailure("LIVE_ENTRY_PRICE_DEVIATION_BLOCKED",
                                       context={"reference_price": float(ref_price),
                                                "quoted_price": estimated_fill_price,
                                                "deviation_pct": round(deviation * 100, 3),
                                                "limit_pct": round(self.max_entry_deviation_pct * 100, 3)})
            signed = self._sign_transaction(str(order["transaction"]))
            try:
                response = self.session.post(
                    f"{JUPITER_BASE}/execute",
                    json={"signedTransaction": signed, "requestId": order["requestId"]},
                    headers={"Accept": "application/json", "Content-Type": "application/json",
                             "x-api-key": self.api_key}, timeout=self.timeout,
                )
                self._requests += 1
            except Exception as exc:
                raise ExecutionFailure(f"JUPITER_ENTRY_RESULT_AMBIGUOUS:{type(exc).__name__}", ambiguous=True,
                                       context={"mint": mint, "request_id": order.get("requestId"),
                                                "requested_usd": desired_usd}) from None
            try:
                response.raise_for_status()
                result = response.json()
            except Exception as exc:
                raise ExecutionFailure(f"JUPITER_ENTRY_RESPONSE_INVALID:{type(exc).__name__}",
                                       ambiguous=int(getattr(response, "status_code", 0) or 0) >= 500,
                                       context={"mint": mint, "request_id": order.get("requestId"),
                                                "http_status": getattr(response, "status_code", None)}) from None
            if (not isinstance(result, dict) or str(result.get("status", "")).lower() != "success"
                    or int(result.get("code", 0) or 0) != 0):
                raise ExecutionFailure("JUPITER_ENTRY_FAILED:" + str((result or {}).get("error") or "UNKNOWN")[:100],
                                       context={"mint": mint, "request_id": order.get("requestId")})
            input_raw = _positive_int(result.get("inputAmountResult")) or _positive_int(result.get("totalInputAmount"))
            output_raw = _positive_int(result.get("outputAmountResult")) or _positive_int(result.get("totalOutputAmount"))
            signature = str(result.get("signature") or "")
            if not input_raw or not output_raw or not signature:
                raise ExecutionFailure("JUPITER_ENTRY_CONFIRMED_BUT_FILL_UNVERIFIED", ambiguous=True,
                                       context={"mint": mint, "request_id": order.get("requestId"),
                                                "signature": signature or None})
            actual_sol_spent = input_raw / LAMPORTS_PER_SOL
            actual_qty = output_raw / (10 ** decimals)
            if actual_qty <= 0:
                raise ExecutionFailure("JUPITER_ENTRY_FILL_QTY_INVALID", ambiguous=True,
                                       context={"mint": mint, "signature": signature})
            actual_cost_usd = actual_sol_spent * sol_usd
            fill_price = actual_cost_usd / actual_qty
            self._last_execution_ts = time.time()
            self._last_signature = signature
            self._last_error = None
            return Fill(price=fill_price, qty=actual_qty, fee=self.estimated_tx_fee_usd,
                        slippage=max(0.0, (fill_price - float(ref_price)) * actual_qty),
                        kind="LIVE_ENTRY", decimals=decimals)

    def execute_exit(self, mint: str, ref_price: float, qty: float, kind: str) -> Optional[Fill]:
        """Sell a precisely sized amount of a position token back to native SOL."""
        with self._lock:
            self._require_ready()
            if not mint or mint == SOL_MINT or ref_price <= 0 or qty <= 0:
                raise ExecutionFailure("LIVE_EXIT_INPUT_INVALID")
            sol_usd = self._sol_usd()
            decimals = self._mint_decimals_for(mint)
            amount_raw = int(round(float(qty) * (10 ** decimals)))
            if amount_raw <= 0:
                raise ExecutionFailure("LIVE_EXIT_ROUNDS_TO_ZERO", context={"mint": mint})
            available_raw = self._token_balance_raw(mint)
            if available_raw < amount_raw:
                raise ExecutionFailure("INSUFFICIENT_TOKEN_BALANCE_FOR_EXIT",
                                       context={"mint": mint, "needed_raw": amount_raw,
                                                "available_raw": available_raw})
            result = self._order_and_execute(mint, SOL_MINT, amount_raw)
            sold_qty = result["input_raw"] / (10 ** decimals)
            sol_received = result["output_raw"] / LAMPORTS_PER_SOL
            proceeds_usd = sol_received * sol_usd
            if sold_qty <= 0 or proceeds_usd <= 0:
                raise ExecutionFailure("LIVE_EXIT_FILL_INVALID", ambiguous=True,
                                       context={"mint": mint, "signature": result.get("signature")})
            fill_price = proceeds_usd / sold_qty
            return Fill(price=fill_price, qty=sold_qty, fee=self.estimated_tx_fee_usd,
                        slippage=max(0.0, (float(ref_price) - fill_price) * sold_qty),
                        kind=kind, decimals=decimals)


def _positive_int(value: Any) -> int | None:
    try:
        result = int(value)
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None


def _float_env(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value < minimum or value > maximum:
        return default
    return value
