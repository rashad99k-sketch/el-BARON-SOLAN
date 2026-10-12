from launch_stream import PumpPortalLaunchStream, normalize_event


def test_normalize_pumpportal_event():
    mint = 'So11111111111111111111111111111111111111112'
    event = normalize_event({'mint': mint, 'symbol': 'TEST', 'name': 'Test Coin',
                             'traderPublicKey': 'CreatorPubKey', 'timestamp': 1700000000000}, 1700000001)
    assert event['mint'] == mint
    assert event['symbol'] == 'TEST'
    assert event['creator'] == 'CreatorPubKey'
    assert event['launch_ts'] == 1700000000
    assert event['source'] == 'pumpportal_new_token'


def test_invalid_launch_event_rejected():
    assert normalize_event({'symbol': 'NO_MINT'}) is None
    assert normalize_event(None) is None


def test_stream_buffer_deduplicates_and_is_bounded():
    stream = PumpPortalLaunchStream(api_key='', max_events=10)
    for i in range(12):
        mint = 'MintAddress_' + str(i).ljust(32, 'x')
        stream.ingest({'mint': mint, 'source': 'pumpportal_new_token', 'received_ts': 1 + i})
    events = stream.discover(20)
    assert len(events) == 10
    assert events[0]['received_ts'] == 12
    assert stream.health()['enabled'] is True
    assert stream.health()['api_key_configured'] is False


def test_unconfigured_solana_rpc_is_explicitly_disabled():
    from solana_rpc import SolanaMintSafetyProvider
    provider = SolanaMintSafetyProvider(rpc_url='')
    assert provider.snapshot('So11111111111111111111111111111111111111112') is None
    assert provider.health()['state'] == 'DISABLED'


def test_stream_can_be_explicitly_disabled():
    stream = PumpPortalLaunchStream(api_key='', enabled=False)
    assert stream.health()['state'] == 'DISABLED'
    assert stream.health()['enabled'] is False


def test_discovery_rotates_older_buffered_events():
    stream = PumpPortalLaunchStream(api_key='', max_events=20, enabled=False)
    for i in range(20):
        mint = 'MintAddress_' + str(i).ljust(32, 'x')
        stream.ingest({'mint': mint, 'source': 'pumpportal_new_token', 'received_ts': 100 + i})
    first = stream.discover(8)
    second = stream.discover(8)
    assert len(first) == len(second) == 8
    assert [x['mint'] for x in first[:6]] == [x['mint'] for x in second[:6]]
    assert first[-1]['mint'] != second[-1]['mint']


def test_stream_does_not_claim_data_received_before_first_message():
    stream = PumpPortalLaunchStream(api_key="", enabled=True, watchdog_seconds=60)
    stream._connected = True
    stream._connected_at_ts = __import__("time").time()
    health = stream.health()
    assert health["state"] == "CONNECTED_WAITING_FOR_FIRST_MESSAGE"
    assert health["events_received"] == 0
    assert health["last_message_age_seconds"] is None
