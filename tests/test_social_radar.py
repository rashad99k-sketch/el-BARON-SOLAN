from types import SimpleNamespace
from social_radar import SocialTrendRadar

class FakeResponse:
    def __init__(self, body, status=200): self.body, self.status_code = body, status
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(f"HTTP {self.status_code}")
    def json(self): return self.body

class FakeSession:
    def __init__(self): self.calls=[]
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "api.x.com" in url:
            return FakeResponse({"data":[{"id":"123","text":"Watching $BONK and So11111111111111111111111111111111111111112","created_at":"2026-10-11T10:00:00Z","public_metrics":{"like_count":2}}]})
        return FakeResponse({"ok":True,"result":[{"update_id":1,"channel_post":{"message_id":7,"date":1791712800,"chat":{"id":-100123,"username":"testchannel"},"text":"$WIF trend"}}]})

def cfg(**kwargs):
    defaults=dict(social_radar_enabled=True,social_poll_interval_seconds=60,social_max_items=100,
                  social_x_bearer_token="test-bearer",social_telegram_bot_token="test-telegram",
                  social_telegram_chat_ids="-100123",provider_timeout_seconds=1)
    defaults.update(kwargs); return SimpleNamespace(**defaults)

def test_social_radar_deduplicates_and_extracts_contract_and_ticker():
    session=FakeSession(); radar=SocialTrendRadar(cfg(),session=session)
    assert radar.poll_x()==1
    assert radar.poll_x()==0
    snapshot=radar.snapshot()
    assert len(snapshot["items"])==1
    assert snapshot["items"][0]["mint"]=="So11111111111111111111111111111111111111112"
    assert "BONK" in snapshot["items"][0]["tickers"]
    assert snapshot["items"][0]["source"]=="X"

def test_telegram_filters_configured_chats_and_health():
    session=FakeSession(); radar=SocialTrendRadar(cfg(social_x_bearer_token=""),session=session)
    assert radar.poll_telegram()==1
    assert radar.snapshot()["items"][0]["source"]=="TELEGRAM"
    assert radar.health()["live_execution_enabled"] is False

def test_social_radar_missing_credentials_is_not_configured():
    radar=SocialTrendRadar(cfg(social_x_bearer_token="",social_telegram_bot_token=""),session=FakeSession())
    assert radar.poll_once()["state"]=="NOT_CONFIGURED"
    assert radar.health()["state"]=="NOT_CONFIGURED"



def test_explicitly_empty_config_does_not_fall_back_to_process_credentials(monkeypatch):
    # A loaded developer .env must not make a test/config object with blank values secretly
    # activate X or Telegram, which previously made this test order-dependent.
    monkeypatch.setenv("X_API_BEARER_TOKEN", "environment-token-should-not-be-used")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "environment-token-should-not-be-used")
    session = FakeSession()
    radar = SocialTrendRadar(
        cfg(social_x_bearer_token="", social_telegram_bot_token=""), session=session
    )
    result = radar.poll_once()
    assert result["state"] == "NOT_CONFIGURED"
    assert radar.health()["state"] == "NOT_CONFIGURED"
    assert session.calls == []

def test_telegram_chat_allowlist_blocks_unselected_chat():
    class OtherChatSession:
        def get(self,url,**kwargs):
            return FakeResponse({"ok":True,"result":[{"update_id":1,"channel_post":{"message_id":1,"date":1,"chat":{"id":-99,"username":"other"},"text":"$SCAM"}}]})
    radar=SocialTrendRadar(cfg(),session=OtherChatSession())
    assert radar.poll_telegram()==0
    assert radar.snapshot()["items"]==[]


def test_social_candidate_evaluation_is_advisory_and_requires_matching_evidence():
    radar = SocialTrendRadar(cfg(), session=FakeSession())
    radar._remember("X", "Fresh launch $BONK So1anaMintAddress111111111111111111111111", source_id="a")
    result = radar.evaluate_candidate("So1anaMintAddress111111111111111111111111", "BONK")
    assert result["decision_role"] == "ADVISORY_ONLY"
    assert result["live_execution_enabled"] is False
    assert result["exact_mint_verified_by_social"] is True
    assert result["state"] == "OBSERVED"
    missing = radar.evaluate_candidate("differentMintAddress11111111111111111111111", "OTHER")
    assert missing["state"] == "NO_MATCH"
    assert missing["score"] == 0
