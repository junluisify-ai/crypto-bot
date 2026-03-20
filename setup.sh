#!/bin/bash
cat > requirements.txt << 'EOF'
aiohttp>=3.9.0
python-telegram-bot>=20.7
web3>=6.15.0
base58>=2.1.1
PyNaCl>=1.5.0
python-dotenv>=1.0.0
EOF

mkdir -p bot
touch bot/__init__.py

cat > bot/config.py << 'EOF'
import os
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv
load_dotenv()

@dataclass
class Config:
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID:   str = os.getenv("TELEGRAM_CHAT_ID",   "")
    DEEPSEEK_API_KEY:   str = os.getenv("DEEPSEEK_API_KEY",   "")
    DEEPSEEK_BASE_URL:  str = "https://api.deepseek.com"
    SOLANA_PRIVATE_KEY: str = os.getenv("SOLANA_PRIVATE_KEY", "")
    ETH_PRIVATE_KEY:    str = os.getenv("ETH_PRIVATE_KEY",    "")
    BSC_PRIVATE_KEY:    str = os.getenv("BSC_PRIVATE_KEY",    "")
    SOLANA_RPC:  str = "https://api.mainnet-beta.solana.com"
    ETH_RPC:     str = "https://eth.llamarpc.com"
    BASE_RPC:    str = "https://mainnet.base.org"
    BSC_RPC:     str = "https://bsc-dataseed.binance.org"
    TARGET_CHAINS: List[str] = field(default_factory=lambda: ["solana","ethereum","base","bsc"])
    SCAN_INTERVAL_SECONDS:  int   = 30
    MIN_PRICE_CHANGE_PCT:   float = 15.0
    MIN_VOLUME_USD_5M:      float = 50000
    MIN_LIQUIDITY_USD:      float = 30000
    MAX_TOKENS_PER_SCAN:    int   = 20
    MAX_RUGCHECK_RISK_SCORE: int  = 500
    MIN_LP_LOCKED_PCT:       float = 50.0
    BLACKLISTED_TOKENS: List[str] = field(default_factory=list)
    TRADE_SIZE_USD_SOLANA:   float = float(os.getenv("TRADE_SIZE_SOL",  "20"))
    TRADE_SIZE_USD_ETH:      float = float(os.getenv("TRADE_SIZE_ETH",  "20"))
    TRADE_SIZE_USD_BASE:     float = float(os.getenv("TRADE_SIZE_BASE", "20"))
    TRADE_SIZE_USD_BSC:      float = float(os.getenv("TRADE_SIZE_BSC",  "20"))
    MAX_OPEN_POSITIONS: int = 5
    TAKE_PROFIT_PCT:  float = 40.0
    STOP_LOSS_PCT:    float = 20.0
    TRAILING_STOP:    bool  = True
    TRAILING_STOP_PCT: float = 10.0
    AI_MIN_CONFIDENCE: float = 70.0
    AI_MODEL:          str   = "deepseek-chat"

    def validate(self):
        import logging
        log = logging.getLogger("config")
        if not self.TELEGRAM_BOT_TOKEN: log.warning("TELEGRAM_BOT_TOKEN not set")
        if not self.DEEPSEEK_API_KEY:   log.warning("DEEPSEEK_API_KEY not set")

    def trade_size_for_chain(self, chain):
        return {"solana":self.TRADE_SIZE_USD_SOLANA,"ethereum":self.TRADE_SIZE_USD_ETH,"base":self.TRADE_SIZE_USD_BASE,"bsc":self.TRADE_SIZE_USD_BSC}.get(chain,20.0)
EOF

echo "Files created successfully!"
