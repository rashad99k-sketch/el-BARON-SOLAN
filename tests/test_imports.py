def test_all_modules_import():
    import config, storage, market_data, token_safety, strategy_engine
    import risk_engine, paper_execution, position_manager, agent_team
    import intelligence, discovery_service, dashboard, solana_app
    assert config.load_config() is not None


def test_intelligence_class_exists():
    from intelligence import Intelligence
    assert isinstance(Intelligence, type)
