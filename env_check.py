"""Print provider configuration status without exposing credential values."""
from __future__ import annotations

import os
from pathlib import Path

from config import load_config


def _status(value: str | None) -> str:
    return "CONFIGURED" if str(value or "").strip() else "NOT_CONFIGURED"


def main() -> int:
    cfg = load_config()  # explicitly loads project-root .env; does not contact providers
    env_path = Path(__file__).resolve().parent / ".env"
    print("GROK6 ENVIRONMENT CHECK (no secrets printed)")
    print(f"Project .env file: {'FOUND' if env_path.is_file() else 'MISSING'}")
    live_requested = bool(cfg.live_trading_enabled or str(cfg.mode).upper() == "LIVE")
    print(f"Requested mode: {cfg.mode}; live requested: {'YES' if live_requested else 'NO'}")
    key = os.getenv("GROK6_SOLANA_PRIVATE_KEY", "").strip()
    if key.upper() in {"PASTE_YOUR_BASE58_PRIVATE_KEY_HERE", "PASTE_PRIVATE_KEY_HERE", "YOUR_PRIVATE_KEY", "CHANGE_ME"}:
        key = ""
    print(f"GROK6_SOLANA_PRIVATE_KEY: {_status(key)} (value never printed; private key triggers LIVE request)")
    print(f"Live executor config: {'CHECK AT STARTUP' if key else 'PAPER DEFAULT'} (requires solders, Jupiter API key, and matching address if specified)")
    print(f"Kill switch: {'ENGAGED' if cfg.kill_switch else 'disengaged'}")
    print(f"Social radar: {'ENABLED' if cfg.social_radar_enabled else 'DISABLED'}")
    print(f"X_API_BEARER_TOKEN: {_status(cfg.social_x_bearer_token)}")
    print(f"TELEGRAM_BOT_TOKEN: {_status(cfg.social_telegram_bot_token)}")
    print(f"TELEGRAM_CHAT_IDS: {_status(cfg.social_telegram_chat_ids)}")
    pump_enabled = os.getenv("PUMPPORTAL_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
    print(f"PUMPPORTAL_ENABLED: {'ENABLED' if pump_enabled else 'DISABLED'}")
    print(f"PUMPPORTAL_API_KEY: {_status(os.getenv('PUMPPORTAL_API_KEY'))} (optional for new-token/migration discovery)")
    print(f"SOLANA_RPC_URL: {_status(os.getenv('SOLANA_RPC_URL'))} (otherwise public RPC default may be rate-limited)")
    print(f"RUGCHECK_API_KEY: {_status(os.getenv('RUGCHECK_API_KEY'))} (optional enrichment)")
    print(f"JUPITER_API_KEY: {_status(os.getenv('JUPITER_API_KEY'))} (required for configured Jupiter V2 quote checks)")
    phantom_address = os.getenv("GROK6_PHANTOM_PUBLIC_ADDRESS", "").strip()
    if phantom_address.upper() in {"PASTE_YOUR_PUBLIC_SOLANA_ADDRESS_HERE", "YOUR_SOLANA_PUBLIC_ADDRESS", "PASTE_YOUR_SOLANA_ADDRESS_HERE", "CHANGE_ME"}:
        phantom_address = ""
    print(f"GROK6_PHANTOM_PUBLIC_ADDRESS: {_status(phantom_address)} (optional public-address match; never a signing credential)")
    print(f"HELIUS_API_KEY: {_status(os.getenv('HELIUS_API_KEY'))} (optional wallet polling)")
    print("NOTE: CONFIGURED means only that a value is present; the running app validates the signer/address and logs LIVE readiness without printing secrets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
