from __future__ import annotations

from config import Config, load_config
from solana_live_execution import SolanaLiveExecutor, configured_private_key


def test_private_key_placeholder_does_not_trigger_live(monkeypatch):
    monkeypatch.setenv("GROK6_SOLANA_PRIVATE_KEY", "PASTE_YOUR_BASE58_PRIVATE_KEY_HERE")
    assert configured_private_key() == ""
    executor = SolanaLiveExecutor(Config().validate())
    assert executor.ready is False
    assert executor.health()["state"] == "NOT_CONFIGURED"


def test_real_key_presence_requests_live_and_uses_separate_database(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK6_SOLANA_PRIVATE_KEY", "test-private-key-placeholder-for-config-only")
    monkeypatch.setenv("GROK6_MODE", "PAPER")
    monkeypatch.setenv("GROK6_LIVE_TRADING", "false")
    monkeypatch.setenv("GROK6_LIVE_DB_PATH", str(tmp_path / "live.db"))
    cfg = load_config()
    assert cfg.mode == "LIVE"
    assert cfg.live_trading_enabled is True
    assert cfg.solana_live_key_configured is True
    assert cfg.db_path == str(tmp_path / "live.db")
    assert "test-private-key-placeholder-for-config-only" not in repr(cfg.as_dict())


def test_live_executor_uses_order_then_execute_and_requires_confirmed_fill(monkeypatch):
    from types import SimpleNamespace
    from solana_live_execution import ExecutionFailure, SolanaLiveExecutor

    class Response:
        def __init__(self, payload, status_code=200):
            self.payload = payload
            self.status_code = status_code
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("http error")
        def json(self):
            return self.payload

    class Session:
        def __init__(self):
            self.get_calls = []
            self.post_calls = []
        def get(self, url, **kwargs):
            self.get_calls.append((url, kwargs))
            return Response({"transaction": "tx-base64", "requestId": "req-1", "router": "metis"})
        def post(self, url, **kwargs):
            self.post_calls.append((url, kwargs))
            return Response({"status": "Success", "code": 0,
                             "signature": "sig-1", "inputAmountResult": "100",
                             "outputAmountResult": "200"})

    session = Session()
    executor = SolanaLiveExecutor.__new__(SolanaLiveExecutor)
    executor.cfg = Config()
    executor.timeout = 2
    executor.session = session
    executor.api_key = "test-api-key"
    executor.public_address = "test-wallet"
    executor.keypair = object()
    executor.ready = True
    executor.reason = "READY"
    executor._secret_present = True
    executor._lock = __import__("threading").RLock()
    executor._requests = 0
    executor._last_execution_ts = None
    executor._last_signature = None
    executor._last_error = None
    executor._mint_decimals = {}
    executor._sol_price_cache = None
    executor._sign_transaction = lambda transaction: "signed-transaction"

    result = executor._order_and_execute("mint-in", "mint-out", 100)
    assert result["input_raw"] == 100
    assert result["output_raw"] == 200
    assert result["signature"] == "sig-1"
    assert len(session.get_calls) == 1
    assert len(session.post_calls) == 1
    assert session.post_calls[0][1]["json"] == {
        "signedTransaction": "signed-transaction", "requestId": "req-1"
    }


def test_jupiter_execute_timeout_is_ambiguous_and_never_retried():
    from solana_live_execution import ExecutionFailure, SolanaLiveExecutor

    class Response:
        def raise_for_status(self):
            return None
        def json(self):
            return {"transaction": "tx-base64", "requestId": "req-ambiguous"}

    class Session:
        def __init__(self):
            self.posts = 0
        def get(self, *args, **kwargs):
            return Response()
        def post(self, *args, **kwargs):
            self.posts += 1
            raise TimeoutError("simulated timeout")

    executor = SolanaLiveExecutor.__new__(SolanaLiveExecutor)
    executor.ready = True
    executor.keypair = object()
    executor.public_address = "test-wallet"
    executor.api_key = "test-api-key"
    executor.timeout = 2
    executor.session = Session()
    executor._requests = 0
    executor._sign_transaction = lambda transaction: "signed-transaction"
    try:
        executor._order_and_execute("mint-in", "mint-out", 100)
        assert False, "expected ambiguous execution failure"
    except ExecutionFailure as exc:
        assert exc.ambiguous is True
        assert "AMBIGUOUS" in exc.reason
    assert executor.session.posts == 1


def test_live_startup_reconciliation_blocks_when_onchain_balance_is_short(cfg, storage):
    from mode_resolver import ModeResolver, OperatingMode
    from paper_execution import PaperExecutor
    from position_manager import PositionManager

    # Create a persisted position first, then simulate the LIVE recovery path.
    paper_pm = PositionManager(cfg, storage, PaperExecutor(cfg))
    position = paper_pm.open_position("test-mint", "TEST", "TEST", "technical",
                                      1.0, 0.9, 1.1, 1.2, 10.0, "test", {})
    assert position is not None
    meta = dict(position.get("meta") or {})
    meta["execution_mode"] = "LIVE"
    storage.update_position(position["id"], meta_json=meta)

    class FakeLiveExecutor:
        mode = "LIVE"
        ready = True
        def _mint_decimals_for(self, mint):
            return 6
        def _token_balance_raw(self, mint):
            return 9_000_000  # database claims 10 tokens

    cfg.mode = "LIVE"
    cfg.live_trading_enabled = True
    cfg.solana_live_key_configured = True
    cfg.live_execution_ready = True
    resolver = ModeResolver(cfg, storage)
    live_pm = PositionManager(cfg, storage, FakeLiveExecutor(), mode_resolver=resolver)
    live_pm.reconcile()

    current = storage.get_position(position["id"])
    assert current["meta"]["live_reconciliation_required"] is True
    assert resolver.current() == OperatingMode.SAFE_STOP
    assert resolver.can_open_position()[0] is False


def test_live_startup_reconciliation_allows_matching_onchain_balance(cfg, storage):
    from mode_resolver import ModeResolver, OperatingMode
    from paper_execution import PaperExecutor
    from position_manager import PositionManager

    paper_pm = PositionManager(cfg, storage, PaperExecutor(cfg))
    position = paper_pm.open_position("test-mint-good", "TEST", "TEST", "technical",
                                      1.0, 0.9, 1.1, 1.2, 10.0, "test", {})
    assert position is not None
    meta = dict(position.get("meta") or {})
    meta["execution_mode"] = "LIVE"
    storage.update_position(position["id"], meta_json=meta)

    class FakeLiveExecutor:
        mode = "LIVE"
        ready = True
        def _mint_decimals_for(self, mint):
            return 6
        def _token_balance_raw(self, mint):
            return 10_000_000

    cfg.mode = "LIVE"
    cfg.live_trading_enabled = True
    cfg.solana_live_key_configured = True
    cfg.live_execution_ready = True
    resolver = ModeResolver(cfg, storage)
    live_pm = PositionManager(cfg, storage, FakeLiveExecutor(), mode_resolver=resolver)
    live_pm.reconcile()
    assert resolver.current() == OperatingMode.LIVE
    assert storage.get_position(position["id"])["meta"].get("live_reconciliation_required") is None
