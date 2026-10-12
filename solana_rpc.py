"""Conservative Solana classic SPL mint-authority verifier.

Token-2022 and non-standard mint layouts are reported UNSUPPORTED, never guessed.
This adapter does not compute holder-owner concentration or honeypot status.
"""
from __future__ import annotations
import base64, itertools, logging, os, time
from typing import Any, Dict, List, Optional
from market_data import DataResult, DataStatus, TokenSnapshot, _miss

log = logging.getLogger('grok6.solana_rpc')
TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
TOKEN_2022_PROGRAM = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
_ids = itertools.count(1)


def _result(value, source='solana_rpc', reason=None):
    now = time.time()
    return DataResult(status=DataStatus.VALID, value=value, source=source, ts=now,
                      age_seconds=0, reason=reason, completeness='full')


class SolanaMintSafetyProvider:
    name = 'solana_rpc_mint_safety'
    def __init__(self, rpc_url: Optional[str] = None, timeout: float = 6.0):
        if rpc_url is not None:
            raw_url = rpc_url
        else:
            configured_rpc = os.getenv('SOLANA_RPC_URL', '').strip()
            helius_key = os.getenv('HELIUS_API_KEY', '').strip()
            raw_url = (configured_rpc
                       or (f'https://mainnet.helius-rpc.com/?api-key={helius_key}' if helius_key
                           else 'https://api.mainnet-beta.solana.com'))
        self.rpc_url = raw_url.strip()
        self.timeout = timeout
        self._last_error = None
        self._last_success = None

    def discover(self, limit=30):
        return []

    def snapshot(self, mint: str, pool_id=None):
        if not self.rpc_url: return None
        try:
            import requests
            response = requests.post(self.rpc_url, json={'jsonrpc':'2.0','id':next(_ids),
                'method':'getAccountInfo','params':[mint, {'encoding':'base64','commitment':'confirmed'}]}, timeout=self.timeout)
            response.raise_for_status()
            body = response.json()
            if body.get('error'): raise RuntimeError(str(body['error']))
            value = (body.get('result') or {}).get('value')
            if not value:
                self._last_error = 'Mint account not found'
                return None
            owner = value.get('owner')
            missing = _miss('not provided by mint account RPC')
            decimals_result = missing
            if owner == TOKEN_2022_PROGRAM:
                mint_auth = DataResult(DataStatus.UNSUPPORTED, reason='TOKEN_2022_LAYOUT_NOT_DECODED', source=self.name, completeness='none')
                freeze_auth = DataResult(DataStatus.UNSUPPORTED, reason='TOKEN_2022_LAYOUT_NOT_DECODED', source=self.name, completeness='none')
            elif owner != TOKEN_PROGRAM:
                mint_auth = DataResult(DataStatus.UNSUPPORTED, reason='UNRECOGNIZED_TOKEN_PROGRAM', source=self.name, completeness='none')
                freeze_auth = DataResult(DataStatus.UNSUPPORTED, reason='UNRECOGNIZED_TOKEN_PROGRAM', source=self.name, completeness='none')
            else:
                raw = base64.b64decode(value['data'][0])
                if len(raw) != 82:
                    mint_auth = DataResult(DataStatus.UNSUPPORTED, reason='UNEXPECTED_CLASSIC_MINT_LAYOUT', source=self.name, completeness='none')
                    freeze_auth = DataResult(DataStatus.UNSUPPORTED, reason='UNEXPECTED_CLASSIC_MINT_LAYOUT', source=self.name, completeness='none')
                else:
                    mint_opt = int.from_bytes(raw[0:4], 'little')
                    freeze_opt = int.from_bytes(raw[46:50], 'little')
                    mint_auth = _result(mint_opt == 0, self.name, 'mint authority absent' if mint_opt == 0 else 'mint authority present')
                    freeze_auth = _result(freeze_opt == 0, self.name, 'freeze authority absent' if freeze_opt == 0 else 'freeze authority present')
                    decimals_result = _result(int(raw[44]), self.name, 'classic SPL mint decimals')
            self._last_success = time.time(); self._last_error = None
            return TokenSnapshot(mint=mint, pool_id=pool_id, symbol=None, name=None,
                price_usd=missing, liquidity_usd=missing, volume_5m_usd=missing, volume_1h_usd=missing,
                price_change_5m_pct=missing, price_change_1h_pct=missing, txns_5m=missing,
                buys_5m=missing, sells_5m=missing, unique_buyers_5m=missing, unique_sellers_5m=missing,
                ohlcv=missing, holders_top10_pct=missing, deployer_pct=missing,
                mint_authority_disabled=mint_auth, freeze_authority_disabled=freeze_auth,
                # RPC proves mint/freeze authority state only; it cannot prove honeypot
                # status. Pass an explicit UNKNOWN/MISSING value so the snapshot stays
                # schema-complete without inventing a negative honeypot result.
                honeypot_flag=missing,
                decimals=decimals_result)
        except Exception as exc:
            self._last_error = f'{type(exc).__name__}: {exc}'
            log.warning('Solana RPC mint check failed: %s', self._last_error)
            return None

    def health(self):
        return {'state':'DISABLED' if not self.rpc_url else ('DEGRADED' if self._last_error else ('CONNECTED' if self._last_success else 'NOT_CHECKED')),
                'configured':bool(self.rpc_url), 'last_success_ts':self._last_success, 'last_error':self._last_error}
