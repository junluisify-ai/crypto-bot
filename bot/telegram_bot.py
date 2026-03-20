import asyncio
import logging
from typing import Optional

logger = logging.getLogger("telegram")

try:
    from telegram import Update
    from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
    from telegram.constants import ParseMode
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    logger.warning("python-telegram-bot not installed")

class TelegramBot:
    def __init__(self, config, trader):
        self.config=config; self.trader=trader
        self._app=None; self._bot=None; self._paused=False

    async def start(self):
        if not TELEGRAM_AVAILABLE or not self.config.TELEGRAM_BOT_TOKEN:
            logger.warning("Telegram not configured - running without alerts")
            await asyncio.Event().wait()
            return
        self._app=ApplicationBuilder().token(self.config.TELEGRAM_BOT_TOKEN).build()
        self._app.add_handler(CommandHandler("start",    self._cmd_start))
        self._app.add_handler(CommandHandler("status",   self._cmd_status))
        self._app.add_handler(CommandHandler("positions",self._cmd_positions))
        self._app.add_handler(CommandHandler("pause",    self._cmd_pause))
        self._app.add_handler(CommandHandler("resume",   self._cmd_resume))
        self._app.add_handler(CommandHandler("help",     self._cmd_help))
        self._bot=self._app.bot
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()
        await self.send_message("🚀 *CryptoBot is online!*\nWatching: Solana | ETH | Base | BSC\nType /help for commands.")
        logger.info("Telegram bot started")
        await asyncio.Event().wait()

    async def stop(self):
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()

    async def send_opportunity(self, candidate, risk, ai):
        await self.send_message(
            f"🔥 *OPPORTUNITY FOUND*\n\n*{candidate.symbol}* — {candidate.chain.upper()}\n"
            f"Price: `${candidate.price_usd:.8f}`\n"
            f"📈 +{candidate.price_change_5m:.1f}% (5m)\n"
            f"💧 Liquidity: ${candidate.liquidity_usd:,.0f}\n\n"
            f"🛡️ *Safety*\n{risk.summary()}\n\n"
            f"🤖 *AI*\n{ai.summary()}\n\n"
            f"🔗 [DEX Screener]({candidate.url}) | [RugCheck]({risk.rugcheck_url})\n\n"
            f"⚡ Executing trade now...")

    async def send_trade_executed(self, candidate, trade):
        await self.send_message(
            f"✅ *TRADE EXECUTED*\n\n"
            f"Bought *{candidate.symbol}* on {candidate.chain.upper()}\n"
            f"Entry: `${candidate.price_usd:.8f}`\n"
            f"Amount: `${self.config.trade_size_for_chain(candidate.chain):.2f}`\n"
            f"TP: +{self.config.TAKE_PROFIT_PCT:.0f}% | SL: -{self.config.STOP_LOSS_PCT:.0f}%")

    async def send_trade_failed(self, candidate, error):
        await self.send_message(f"❌ *TRADE FAILED*\n{candidate.symbol}: `{error}`")

    async def send_skipped(self, candidate, reason):
        await self.send_message(f"⏭️ *Skipped {candidate.symbol}* ({candidate.chain.upper()})\n{reason}")

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("👋 *CryptoBot Active!*\nUse /help for commands.",parse_mode=ParseMode.MARKDOWN)

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        state="⏸️ PAUSED" if self._paused else "✅ RUNNING"
        await update.message.reply_text(
            f"📊 *Bot Status*\nState: {state}\n"
            f"Positions: {len(self.trader.positions)}/{self.config.MAX_OPEN_POSITIONS}\n"
            f"Chains: {', '.join(self.config.TARGET_CHAINS)}",
            parse_mode=ParseMode.MARKDOWN)

    async def _cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(self.trader.get_positions_summary(),parse_mode=ParseMode.MARKDOWN)

    async def _cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self._paused=True
        await update.message.reply_text("⏸️ Bot paused. Use /resume to restart.")

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self._paused=False
        await update.message.reply_text("▶️ Bot resumed!")

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "📖 *Commands*\n/status — Bot state\n/positions — Open trades\n"
            "/pause — Stop scanning\n/resume — Resume\n/help — This message",
            parse_mode=ParseMode.MARKDOWN)

    async def send_message(self, text):
        if not TELEGRAM_AVAILABLE or not self._bot:
            logger.info("ALERT: %s", text[:100].replace("*","").replace("`",""))
            return
        try:
            await self._bot.send_message(
                chat_id=self.config.TELEGRAM_CHAT_ID,text=text,
                parse_mode=ParseMode.MARKDOWN,disable_web_page_preview=True)
        except Exception as e:
            logger.error("Telegram send error: %s",e)
