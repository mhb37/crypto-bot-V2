import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Trading mode ──────────────────────────────────────────────────────────────
TRADING_MODE = os.getenv("TRADING_MODE", "paper")          # "paper" | "live"
INITIAL_PORTFOLIO = float(os.getenv("INITIAL_PORTFOLIO", "1000"))  # USD
POSITION_SIZE_PCT = float(os.getenv("POSITION_SIZE_PCT", "0.05"))  # 5% per trade
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.15"))          # -15%
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.50"))      # +50%
MAX_HOLD_HOURS = int(os.getenv("MAX_HOLD_HOURS", "48"))            # close after 48h

# ── Scanner settings ──────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))  # 5 min
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "10000"))
MIN_VOLUME_24H_USD = float(os.getenv("MIN_VOLUME_24H_USD", "5000"))
MIN_AGE_HOURS = int(os.getenv("MIN_AGE_HOURS", "24"))
MAX_AGE_HOURS = int(os.getenv("MAX_AGE_HOURS", "168"))             # 7 days max
MIN_PRICE_CHANGE_1H = float(os.getenv("MIN_PRICE_CHANGE_1H", "2"))   # +2% in 1h
MIN_PRICE_CHANGE_24H = float(os.getenv("MIN_PRICE_CHANGE_24H", "10")) # +10% in 24h

# ── AI/ML ────────────────────────────────────────────────────────────────────
MIN_SCORE_TO_TRADE = int(os.getenv("MIN_SCORE_TO_TRADE", "70"))   # 0-100
MODEL_PATH = os.getenv("MODEL_PATH", "models/scoring_model.pkl")
RETRAIN_INTERVAL_DAYS = int(os.getenv("RETRAIN_INTERVAL_DAYS", "7"))
MIN_TRADES_FOR_RETRAIN = int(os.getenv("MIN_TRADES_FOR_RETRAIN", "20"))

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "data/trading.db")

# ── Reporting ─────────────────────────────────────────────────────────────────
REPORT_HOUR_UTC = int(os.getenv("REPORT_HOUR_UTC", "8"))          # 08:00 UTC daily

# ── Live trading – Solana / Jupiter (disabled by default) ─────────────────────
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "")          # base58 encoded
JUPITER_SLIPPAGE_BPS = int(os.getenv("JUPITER_SLIPPAGE_BPS", "100"))  # 1%
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"

# ── Chain filter ──────────────────────────────────────────────────────────────
TARGET_CHAINS = [c.strip().lower() for c in os.getenv("TARGET_CHAINS", "solana,bsc,eth").split(",")]

# Seuils de liquidité par chain (ETH coûte cher en gas → exige plus de liquidité)
MIN_LIQUIDITY_PER_CHAIN = {
    "solana": float(os.getenv("MIN_LIQUIDITY_SOLANA", "10000")),
    "bsc":    float(os.getenv("MIN_LIQUIDITY_BSC",    "15000")),
    "eth":    float(os.getenv("MIN_LIQUIDITY_ETH",    "50000")),
}
