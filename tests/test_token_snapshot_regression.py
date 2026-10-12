from market_data import DataStatus, TokenSnapshot


def test_minimal_construction_does_not_raise():
    snap = TokenSnapshot(mint="M")
    assert snap.honeypot_flag.status == DataStatus.MISSING
    assert snap.rugged_flag.status == DataStatus.MISSING
    assert snap.mint_authority_disabled.status == DataStatus.MISSING


def test_missing_is_not_false_or_zero():
    snap = TokenSnapshot(mint="M")
    assert snap.honeypot_flag.value is None
    assert snap.honeypot_flag.status != DataStatus.VALID
    assert snap.price_usd.value is None
    assert snap.price_usd.status == DataStatus.MISSING


def test_partial_provider_snapshot_constructs_safely():
    snap = TokenSnapshot(mint="M", symbol="TST")
    assert snap.symbol == "TST"
    assert snap.liquidity_usd.status == DataStatus.MISSING


def test_positional_mint_and_pool_call_remains_supported():
    snap = TokenSnapshot("M", "POOL")
    assert snap.mint == "M"
    assert snap.pool_id == "POOL"
