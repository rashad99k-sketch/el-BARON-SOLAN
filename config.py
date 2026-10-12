"""Validated configuration with PAPER defaults and fail-closed optional LIVE support."""
from __future__ import annotations
import os
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(ValueError):
    pass


def _env_str(name: str, default: str) -> str:
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ConfigError(f"Environment {name}={raw!r} is not a valid float") from e


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"Environment {name}={raw!r} is not a valid int") from e


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    # Runtime
    mode: str = "PAPER"
    live_trading_enabled: bool = False
    # Runtime-only execution readiness populated by Application after signer validation.
    solana_live_key_configured: bool = False
    live_execution_ready: bool = False
    live_execution_blocker: str = ""
    live_wallet_public_address: str = ""
    kill_switch: bool = False
    host: str = "127.0.0.1"
    port: int = 8080
    log_level: str = "INFO"

    # Storage
    db_path: str = "./grok6.db"

    # Portfolio / risk (per spec defaults)
    initial_equity: float = 20.0
    max_position_notional: float = 2.0
    max_total_open_exposure: float = 6.0
    max_total_open_positions: int = 6
    max_technical_positions: int = 5
    max_fast_launch_positions: int = 2
    risk_per_trade_pct: float = 0.005      # 0.5% of current equity
    max_aggregate_open_risk_pct: float = 0.02  # 2%
    daily_realized_loss_lock: float = -1.0     # USD

    # Execution costs
    taker_fee_bps: float = 30.0
    slippage_bps: float = 60.0
    assumed_price_impact_bps: float = 40.0
    max_sell_route_price_impact_pct: float = 5.0

    # Strategy thresholds
    min_liquidity_usd: float = 8000.0
    max_top_holder_pct: float = 0.35
    max_deployer_pct: float = 0.15
    min_net_reward_risk: float = 1.6
    max_entry_extension_pct: float = 0.06
    fast_launch_enabled: bool = True

    # Scanner
    scan_interval_seconds: int = 30
    provider_timeout_seconds: float = 8.0
    provider_max_retries: int = 3
    candidate_ttl_seconds: int = 900

    # V7 event-driven Fast Launch Sniper and continuation monitoring.
    sniper_candidate_ttl_seconds: int = 900
    sniper_max_entry_age_seconds: float = 45.0
    event_queue_size: int = 5000
    event_workers: int = 4
    retry_base_delay_seconds: float = 2.0
    retry_max_delay_seconds: float = 60.0
    retry_max_attempts: int = 8
    continuation_min_liquidity_usd: float = 20000.0
    continuation_interval_seconds: float = 12.0
    continuation_observation_cooldown_seconds: float = 20.0
    continuation_max_candidates_per_cycle: int = 8

    # Fast-launch winner management: TP1 banks half; TP2 arms a trailing runner.
    fast_launch_max_entry_extension_pct: float = 0.18
    fast_launch_max_5m_move_pct: float = 35.0
    fast_launch_stop_pct: float = 0.12
    fast_launch_tp1_return_pct: float = 0.30
    fast_launch_runner_activation_pct: float = 0.75
    fast_launch_runner_trail_drawdown_pct: float = 0.22
    fast_launch_runner_enabled: bool = True
    position_monitor_interval_seconds: float = 3.0
    position_price_cache_ttl_seconds: float = 2.0

    # Social trend radar (read-only; never an execution authority).
    social_radar_enabled: bool = True
    social_poll_interval_seconds: int = 60
    social_max_items: int = 500
    social_x_bearer_token: str = ""
    social_telegram_bot_token: str = ""
    social_telegram_chat_ids: str = ""

    # Timezone for daily boundary
    daily_boundary_tz: str = "UTC"

    def validate(self) -> "Config":
        self.mode = str(self.mode or "PAPER").upper()
        if self.mode not in {"PAPER", "WATCH_ONLY", "LIVE"}:
            raise ConfigError("mode must be PAPER, WATCH_ONLY, or LIVE (LIVE will be blocked).")
        # LIVE is a requested mode; ModeResolver authorizes it only when the live adapter validates.
        if self.initial_equity <= 0:
            raise ConfigError("initial_equity must be positive.")
        if self.max_position_notional <= 0 or self.max_position_notional > self.initial_equity:
            raise ConfigError("max_position_notional must be in (0, initial_equity].")
        if self.max_total_open_exposure < self.max_position_notional:
            raise ConfigError("max_total_open_exposure must be >= max_position_notional.")
        if self.max_total_open_positions < 1 or self.max_total_open_positions > 20:
            raise ConfigError("max_total_open_positions out of range.")
        if self.max_technical_positions + self.max_fast_launch_positions < self.max_total_open_positions:
            # Not fatal, just informational; keep strict caps.
            pass
        if not (0 < self.risk_per_trade_pct <= 0.05):
            raise ConfigError("risk_per_trade_pct out of sane range (0, 0.05].")
        if not (0 < self.max_aggregate_open_risk_pct <= 0.10):
            raise ConfigError("max_aggregate_open_risk_pct out of range.")
        if self.daily_realized_loss_lock >= 0:
            raise ConfigError("daily_realized_loss_lock must be negative.")
        if self.min_net_reward_risk <= 0:
            raise ConfigError("min_net_reward_risk must be positive.")
        try:
            ZoneInfo(self.daily_boundary_tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"Unknown daily_boundary_tz: {self.daily_boundary_tz}") from exc
        if self.port <= 0 or self.port > 65535:
            raise ConfigError("port out of range.")
        if self.event_workers < 1 or self.event_workers > 32:
            raise ConfigError("event_workers must be between 1 and 32.")
        if self.event_queue_size < 100:
            raise ConfigError("event_queue_size must be at least 100.")
        if self.sniper_max_entry_age_seconds <= 0:
            raise ConfigError("sniper_max_entry_age_seconds must be positive.")
        if self.retry_max_attempts < 1 or self.retry_max_attempts > 20:
            raise ConfigError("retry_max_attempts must be between 1 and 20.")
        if self.retry_base_delay_seconds < 0 or self.retry_max_delay_seconds < self.retry_base_delay_seconds:
            raise ConfigError("retry delay bounds are invalid.")
        if self.continuation_interval_seconds < 2 or self.continuation_observation_cooldown_seconds < 1:
            raise ConfigError("continuation intervals are too low.")
        if self.continuation_max_candidates_per_cycle < 1 or self.continuation_max_candidates_per_cycle > 100:
            raise ConfigError("continuation_max_candidates_per_cycle must be between 1 and 100.")
        if not (0.01 <= self.fast_launch_stop_pct <= 0.35):
            raise ConfigError("fast_launch_stop_pct must be between 1% and 35%.")
        if not (0.05 <= self.fast_launch_tp1_return_pct <= 3.0):
            raise ConfigError("fast_launch_tp1_return_pct out of range.")
        if not (self.fast_launch_tp1_return_pct < self.fast_launch_runner_activation_pct <= 10.0):
            raise ConfigError("runner activation must exceed TP1 and be <= 1000%.")
        if not (0.05 <= self.fast_launch_runner_trail_drawdown_pct <= 0.60):
            raise ConfigError("fast_launch_runner_trail_drawdown_pct must be between 5% and 60%.")
        if self.position_monitor_interval_seconds < 0.5 or self.position_price_cache_ttl_seconds < 0:
            raise ConfigError("position monitoring/cache interval is invalid.")
        return self

    def as_dict(self) -> dict:
        # Never expose signing material. This dataclass intentionally stores only
        # secret-presence/readiness metadata, never the private key itself.
        data = asdict(self)
        data["live_requested"] = bool(self.live_trading_enabled or str(self.mode).upper() == "LIVE")
        data["live_trading_enabled"] = bool(self.live_execution_ready)
        data["solana_live_key_configured"] = bool(self.solana_live_key_configured)
        data["live_execution_ready"] = bool(self.live_execution_ready)
        data["live_execution_blocker"] = str(self.live_execution_blocker or "")
        # A public wallet address is not a signing secret, but it is unnecessary in
        # generic settings payloads; expose only whether the signer was resolved.
        data["live_wallet_public_address"] = "RESOLVED" if self.live_wallet_public_address else ""
        data["kill_switch"] = bool(self.kill_switch)
        for key in ("social_x_bearer_token", "social_telegram_bot_token"):
            data[key] = "CONFIGURED" if data.get(key) else ""
        return data


def load_config() -> Config:
    """Load configuration from environment/.env. Never opens network connections or threads."""
    try:
        from dotenv import load_dotenv
        # Anchor .env to the project directory, not the caller's current working directory.
        # Existing environment variables (e.g. Render secrets) retain precedence.
        env_path = Path(__file__).resolve().parent / ".env"
        if env_path.is_file():
            load_dotenv(dotenv_path=env_path, override=False)
    except ImportError:
        # Environment variables supplied by Render/containers work without python-dotenv.
        pass
    key_raw = os.environ.get("GROK6_SOLANA_PRIVATE_KEY", "").strip()
    key_placeholders = {"PASTE_YOUR_BASE58_PRIVATE_KEY_HERE", "PASTE_PRIVATE_KEY_HERE",
                        "YOUR_PRIVATE_KEY", "YOUR_SOLANA_PRIVATE_KEY", "CHANGE_ME"}
    key_present = bool(key_raw and key_raw.upper() not in key_placeholders)
    env_mode = _env_str("GROK6_MODE", "PAPER").upper()
    env_live_flag = _env_bool("GROK6_LIVE_TRADING", False)
    live_requested = key_present or env_live_flag or env_mode == "LIVE"
    effective_mode = "LIVE" if key_present else env_mode
    database_path = (_env_str("GROK6_LIVE_DB_PATH", "./grok6_live.db") if live_requested
                     else _env_str("GROK6_DB_PATH", "./grok6.db"))
    cfg = Config(
        mode=effective_mode,
        live_trading_enabled=(env_live_flag or key_present),
        solana_live_key_configured=key_present,
        kill_switch=_env_bool("GROK6_KILL_SWITCH", False),
        host=_env_str("GROK6_HOST", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"),
        port=_env_int("GROK6_PORT", _env_int("PORT", 8080)),
        log_level=_env_str("GROK6_LOG_LEVEL", "INFO"),
        db_path=database_path,
        initial_equity=_env_float("GROK6_INITIAL_EQUITY", 20.0),
        max_position_notional=_env_float("GROK6_MAX_POSITION_NOTIONAL", 2.0),
        max_total_open_exposure=_env_float("GROK6_MAX_TOTAL_OPEN_EXPOSURE", 6.0),
        max_total_open_positions=_env_int("GROK6_MAX_TOTAL_OPEN_POSITIONS", 6),
        max_technical_positions=_env_int("GROK6_MAX_TECHNICAL_POSITIONS", 5),
        max_fast_launch_positions=_env_int("GROK6_MAX_FAST_LAUNCH_POSITIONS", 2),
        risk_per_trade_pct=_env_float("GROK6_RISK_PER_TRADE_PCT", 0.005),
        max_aggregate_open_risk_pct=_env_float("GROK6_MAX_AGGREGATE_OPEN_RISK_PCT", 0.02),
        daily_realized_loss_lock=_env_float("GROK6_DAILY_REALIZED_LOSS_LOCK", -1.0),
        taker_fee_bps=_env_float("GROK6_TAKER_FEE_BPS", 30.0),
        slippage_bps=_env_float("GROK6_SLIPPAGE_BPS", 60.0),
        assumed_price_impact_bps=_env_float("GROK6_PRICE_IMPACT_BPS", 40.0),
        max_sell_route_price_impact_pct=_env_float("GROK6_MAX_SELL_ROUTE_PRICE_IMPACT_PCT", 5.0),
        min_liquidity_usd=_env_float("GROK6_MIN_LIQUIDITY_USD", 8000.0),
        max_top_holder_pct=_env_float("GROK6_MAX_TOP_HOLDER_PCT", 0.35),
        max_deployer_pct=_env_float("GROK6_MAX_DEPLOYER_PCT", 0.15),
        min_net_reward_risk=_env_float("GROK6_MIN_NET_REWARD_RISK", 1.6),
        max_entry_extension_pct=_env_float("GROK6_MAX_ENTRY_EXTENSION_PCT", 0.06),
        fast_launch_enabled=_env_bool("GROK6_FAST_LAUNCH_ENABLED", True),
        scan_interval_seconds=_env_int("GROK6_SCAN_INTERVAL_SECONDS", 30),
        provider_timeout_seconds=_env_float("GROK6_PROVIDER_TIMEOUT_SECONDS", 8.0),
        provider_max_retries=_env_int("GROK6_PROVIDER_MAX_RETRIES", 3),
        candidate_ttl_seconds=_env_int("GROK6_CANDIDATE_TTL_SECONDS", 900),
        sniper_candidate_ttl_seconds=_env_int("GROK6_SNIPER_CANDIDATE_TTL_SECONDS", 900),
        sniper_max_entry_age_seconds=_env_float("GROK6_SNIPER_MAX_ENTRY_AGE_SECONDS", 45.0),
        event_queue_size=_env_int("GROK6_EVENT_QUEUE_SIZE", 5000),
        event_workers=_env_int("GROK6_EVENT_WORKERS", 4),
        retry_base_delay_seconds=_env_float("GROK6_RETRY_BASE_DELAY_SECONDS", 2.0),
        retry_max_delay_seconds=_env_float("GROK6_RETRY_MAX_DELAY_SECONDS", 60.0),
        retry_max_attempts=_env_int("GROK6_RETRY_MAX_ATTEMPTS", 8),
        continuation_min_liquidity_usd=_env_float("GROK6_CONTINUATION_MIN_LIQUIDITY_USD", 20000.0),
        continuation_interval_seconds=_env_float("GROK6_CONTINUATION_INTERVAL_SECONDS", 12.0),
        continuation_observation_cooldown_seconds=_env_float("GROK6_CONTINUATION_OBSERVATION_COOLDOWN_SECONDS", 20.0),
        continuation_max_candidates_per_cycle=_env_int("GROK6_CONTINUATION_MAX_CANDIDATES_PER_CYCLE", 8),
        fast_launch_max_entry_extension_pct=_env_float("GROK6_FAST_LAUNCH_MAX_ENTRY_EXTENSION_PCT", 0.18),
        fast_launch_max_5m_move_pct=_env_float("GROK6_FAST_LAUNCH_MAX_5M_MOVE_PCT", 35.0),
        fast_launch_stop_pct=_env_float("GROK6_FAST_LAUNCH_STOP_PCT", 0.12),
        fast_launch_tp1_return_pct=_env_float("GROK6_FAST_LAUNCH_TP1_RETURN_PCT", 0.30),
        fast_launch_runner_activation_pct=_env_float("GROK6_FAST_LAUNCH_RUNNER_ACTIVATION_PCT", 0.75),
        fast_launch_runner_trail_drawdown_pct=_env_float("GROK6_FAST_LAUNCH_RUNNER_TRAIL_DRAWDOWN_PCT", 0.22),
        fast_launch_runner_enabled=_env_bool("GROK6_FAST_LAUNCH_RUNNER_ENABLED", True),
        position_monitor_interval_seconds=_env_float("GROK6_POSITION_MONITOR_INTERVAL_SECONDS", 3.0),
        position_price_cache_ttl_seconds=_env_float("GROK6_POSITION_PRICE_CACHE_TTL_SECONDS", 2.0),
        social_radar_enabled=_env_bool("GROK6_SOCIAL_RADAR_ENABLED", True),
        social_poll_interval_seconds=_env_int("GROK6_SOCIAL_POLL_INTERVAL_SECONDS", 60),
        social_max_items=_env_int("GROK6_SOCIAL_MAX_ITEMS", 500),
        social_x_bearer_token=_env_str("X_API_BEARER_TOKEN", ""),
        social_telegram_bot_token=_env_str("TELEGRAM_BOT_TOKEN", ""),
        social_telegram_chat_ids=_env_str("TELEGRAM_CHAT_IDS", ""),
        daily_boundary_tz=_env_str("GROK6_DAILY_TZ", "UTC"),
    )
    return cfg.validate()
