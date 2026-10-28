# CONFIG VERSION 2
import os

MODE = "paper"
REAL_TRADING_ENABLED = False

# Risk
PAPER_AMOUNT_USD = float(os.getenv("PAPER_AMOUNT_PER_POSITION_USD", "8"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "6"))
MAX_DAILY_LOSS_USD = float(os.getenv("MAX_DAILY_LOSS_USD", "24"))
MIN_HOLD_SECONDS = int(os.getenv("MIN_HOLD_SECONDS", "90"))

# Market filters
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "8000"))
MIN_VOLUME_24H_USD = float(os.getenv("MIN_VOLUME_24H_USD", "30000"))
MIN_BUYS_SELLS_RATIO = float(os.getenv("MIN_BUYS_SELLS_RATIO", "1.1"))
MIN_TRADES_24H = int(os.getenv("MIN_TRADES_24H", "20"))

# Rug protection: keep fail-closed checks enabled.
MAX_RUGCHECK_SCORE = 35
MAX_TOP10_HOLDER_PCT = float(os.getenv("MAX_TOP10_HOLDER_PCT", "40"))
MIN_LP_LOCKED_PCT = float(os.getenv("MIN_LP_LOCKED_PCT", "50"))
REQUIRE_MINT_AUTHORITY_REVOKED = True
REQUIRE_FREEZE_AUTHORITY_REVOKED = True
REQUIRE_ZERO_INSIDER_NETWORKS = False

# Exit
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "60"))
PARTIAL_TAKE_PROFIT_PCT = float(os.getenv("PARTIAL_TAKE_PROFIT_PCT", "25"))
PARTIAL_SELL_FRACTION = float(os.getenv("PARTIAL_SELL_FRACTION", "0.5"))
MAX_HOLD_SECONDS = int(os.getenv("MAX_HOLD_SECONDS", "1200"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "-28"))
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "22"))

# Jupiter
JUPITER_QUOTE_URL = os.getenv("JUPITER_QUOTE_URL", "https://api.jup.ag/swap/v1/quote").rstrip("/")
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "").strip()
JUPITER_SLIPPAGE_BPS = int(os.getenv("JUPITER_SLIPPAGE_BPS", "60"))
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Grok / safety-screening budget.
# Evaluate more of the already market-filtered candidates without weakening safety gates.
GROK_API_BASE = os.getenv("XAI_API_BASE", "https://api.x.ai/v1").rstrip("/")
GROK_MODEL = os.getenv("XAI_MODEL", "grok-3-mini")
GROK_TIMEOUT_SECONDS = int(os.getenv("AI_REQUEST_TIMEOUT_SECONDS", "60"))
GROK_CONFIDENCE_THRESHOLD = 0.65
MAX_GROK_CHECKS_PER_SCAN = max(1, min(10, int(os.getenv("MAX_GROK_CHECKS_PER_SCAN", "10"))))
COPYFOMO_GATE_ENABLED = True
