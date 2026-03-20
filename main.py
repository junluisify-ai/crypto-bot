import asyncio
import logging
import sys
from bot.scanner import Scanner
from bot.risk_checker import RiskChecker
from bot.ai_analyst import AIAnalyst
from bot.trader import Trader
from bot.telegram_bot import TelegramBot
from bot.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log"),
    ],
)
logger = logging.getLogger("main")

async def main():
    logger.info("CryptoBot starting up...")
    config   = Config()
    config.validate()
    scanner  = Scanner(config)
    risk     = RiskChecker(config)
    ai       = AIAnalyst(config)
    trader   = Trader(config)
    telegram = TelegramBot(config, trader)
    scanner.set_pipeline(risk, ai, trader, telegram)
    await asyncio.gather(telegram.start(), scanner.run())

if __name__ == "__main__":
    asyncio.run(main())
