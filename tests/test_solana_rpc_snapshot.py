"""Regression tests for RPC-only TokenSnapshot construction."""
import base64

import requests

from market_data import DataStatus
from solana_rpc import SolanaMintSafetyProvider, TOKEN_PROGRAM


class FakeResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        raw = bytearray(82)
        raw[44] = 6
        return {"result": {"context": {"slot": 123}, "value": {
            "owner": TOKEN_PROGRAM, "data": [base64.b64encode(bytes(raw)).decode(), "base64"]
        }}}


def test_rpc_snapshot_includes_explicit_unknown_honeypot_field(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **kw: FakeResponse())
    provider = SolanaMintSafetyProvider(rpc_url="https://rpc.example", timeout=1)
    snap = provider.snapshot("ExampleMint", "ExamplePool")

    assert snap is not None
    assert snap.mint == "ExampleMint"
    assert snap.mint_authority_disabled.status == DataStatus.VALID
    assert snap.mint_authority_disabled.value is True
    assert snap.freeze_authority_disabled.status == DataStatus.VALID
    assert snap.freeze_authority_disabled.value is True
    assert snap.decimals.status == DataStatus.VALID and snap.decimals.value == 6
    assert snap.honeypot_flag.status == DataStatus.MISSING
    assert "not provided" in snap.honeypot_flag.reason
    assert provider.health()["state"] == "CONNECTED"
