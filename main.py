import asyncio
import logging
import sys
from aiohttp import web
from bot.scanner import Scanner
from bot.risk_checker import RiskChecker
from bot.ai_analyst import AIAnalyst
from bot.trader import Trader
from bot.telegram_bot import TelegramBot
from bot.config import Config
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log"),
    ],
)
logger = logging.getLogger("main")

async def handle(request):
    return web.Response(text="CryptoBot is running!")

async def start_web():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    logger.info("Web server started on port 8080")

async def main():
    logger.info("CryptoBot starting up...")
    config = Config()
    config.validate()

    scanner  = Scanner(config)
    risk     = RiskChecker(config)
    ai       = AIAnalyst(config)
    trader   = Trader(config)
    telegram = TelegramBot(config, trader)

    scanner.set_pipeline(risk, ai, trader, telegram)

    await asyncio.gather(
        start_web(),
        telegram.start(),
        scanner.run(),
    )

if __name__ == "__main__":
    asyncio.run(main())
