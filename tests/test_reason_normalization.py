from token_safety import normalize_reason


def test_legacy_sell_route_reason_is_canonicalized_as_missing_evidence():
    assert normalize_reason("MISSING_SELL_ROUTE_EXISTS") == "MISSING_SELL_ROUTE_EVIDENCE"
    assert normalize_reason("MISSING_SELL_ROUTE_EVIDENCE") == "MISSING_SELL_ROUTE_EVIDENCE"
    assert normalize_reason("NO_SELL_ROUTE") == "NO_SELL_ROUTE"
    assert normalize_reason("UNRELATED") == "UNRELATED"
