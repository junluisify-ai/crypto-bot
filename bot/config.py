import os
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv
load_dotenv()

@dataclass
class Config:
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID:   str = os.getenv("TELEGRAM_CHAT_ID",   "")
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    SOLANA_PRIVATE_KEY: str = os.getenv("SOLANA_PRIVATE_KEY", "")
    SOLANA_RPC: str = "https://api.mainnet-beta.solana.com"
    PAPER_TRADING: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"
    PAPER_STARTING_BALANCE: float = 50.0
    TARGET_CHAINS: List[str] = field(default_factory=lambda: ["solana"])
    SCAN_INTERVAL_SECONDS: int   = 30
    MIN_PRICE_CHANGE_PCT:  float = 10.0
    MIN_VOLUME_USD_5M:     float = 20000
    MIN_LIQUIDITY_USD:     float = 35000
    MAX_TOKENS_PER_SCAN:   int   = 20
    MAX_RUGCHECK_RISK_SCORE: int   = 400
    MIN_LP_LOCKED_PCT:       float = 0.0
    BLACKLISTED_TOKENS: List[str] = field(default_factory=list)
    TRADE_SIZE_USD_SOLANA: float = float(os.getenv("TRADE_SIZE_SOL", "3"))
    FEE_BUFFER_USD:        float = 5.0
    MAX_OPEN_POSITIONS: int   = 3
    TAKE_PROFIT_PCT:    float = 40.0
    STOP_LOSS_PCT:      float = 20.0
    TRAILING_STOP:      bool  = True
    TRAILING_STOP_PCT:  float = 10.0
    TIER_A_MIN_LIQUIDITY:   float = 80000
    TIER_A_MAX_FLAGS:       int   = 0
    TIER_A_MIN_LP_LOCKED:   float = 50.0
    TIER_B_MIN_LIQUIDITY:   float = 50000
    TIER_B_MAX_FLAGS:       int   = 1
    TIER_B_MIN_LP_LOCKED:   float = 0.0
    AI_MIN_CONFIDENCE: float = 65.0
    AI_MODEL:          str   = "gemini-1.5-flash"
    HYPE_MIN_PRICE_CHANGE_1H: float = 50.0
    HYPE_MIN_LIQUIDITY:       float = 50000
    HYPE_MAX_RISK_SCORE:      int   = 300

    def validate(self):
        import logging
        log = logging.getLogger("config")
        if not self.TELEGRAM_BOT_TOKEN:
            log.warning("TELEGRAM_BOT_TOKEN not set")
        if not self.GEMINI_API_KEY:
            log.warning("GEMINI_API_KEY not set — will use momentum fallback")
        if not self.SOLANA_PRIVATE_KEY and not self.PAPER_TRADING:
            log.warning("SOLANA_PRIVATE_KEY not set — live trading will fail")
        if self.PAPER_TRADING:
            log.info("*** PAPER TRADING MODE ACTIVE — no real money used ***")
        mode = "PAPER" if self.PAPER_TRADING else "LIVE"
        log.info("Mode: %s | Chain: Solana only | Trade size: $%.2f", mode, self.TRADE_SIZE_USD_SOLANA)

    def trade_size_for_chain(self, chain: str) -> float:
        return self.TRADE_SIZE_USD_SOLANA if chain == "solana" else 0.0

    def tier_trade_size(self, tier: str) -> float:
        if tier == "A":
            return self.TRADE_SIZE_USD_SOLANA
        if tier == "B":
            return self.TRADE_SIZE_USD_SOLANA * 0.5
        return 0.0
