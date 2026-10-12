from health_model import classify_core, classify_data_completeness


def test_optional_wallet_provider_does_not_degrade_core_readiness():
    providers = {
        "dexscreener": {"state": "HEALTHY"},
        "solana_rpc": {"state": "CONNECTED"},
        "pumpportal_launch_stream": {"state": "CONNECTED"},
        "helius": {"state": "NOT_CONFIGURED"},
        "jupiter": {"state": "NOT_CONFIGURED"},
        "rugcheck": {"state": "NOT_CONFIGURED"},
    }
    health = classify_core(providers, paper_readiness={"state": "CONFIGURED_NOT_TOKEN_VERIFIED", "blockers": []})
    assert health.state == "READY"


def test_missing_required_source_marks_not_ready():
    health = classify_core({
        "dexscreener": {"state": "DEGRADED"},
        "solana_rpc": {"state": "CONNECTED"},
        "pumpportal_launch_stream": {"state": "CONNECTED"},
    }, paper_readiness={"state": "CONFIGURED_NOT_TOKEN_VERIFIED", "blockers": []})
    assert health.state == "NOT_READY"
    assert any(x.startswith("dexscreener:") for x in health.blocking)


def test_jupiter_blocker_is_visible_not_hidden_as_optional():
    health = classify_core({
        "dexscreener": {"state": "HEALTHY"},
        "solana_rpc": {"state": "CONNECTED"},
        "pumpportal_launch_stream": {"state": "CONNECTED"},
    }, paper_readiness={"state": "BLOCKED", "blockers": ["JUPITER_API_KEY_NOT_CONFIGURED"]})
    assert health.state == "NOT_READY"
    assert "JUPITER_API_KEY_NOT_CONFIGURED" in health.blocking


def test_data_completeness_excludes_final_rejections():
    result = classify_data_completeness([
        {"mint": "A", "payload": {"decision": "NEEDS_REFRESH", "safety": {"missing_fields": ["mint_authority_disabled"]}}},
        {"mint": "B", "payload": {"decision": "REJECTED", "safety": {"missing_fields": []}}},
    ])
    assert result["n"] == 1
    assert result["complete"] == 0
    assert result["pct_complete"] == 0
