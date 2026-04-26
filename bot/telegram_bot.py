import asyncio
import logging
from typing import Optional
from datetime import datetime, timedelta

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
        self.config  = config
        self.trader  = trader
        self._app    = None
        self._bot    = None
        self._paused = False
        self._start_time = datetime.utcnow()

    async def start(self):
        if not TELEGRAM_AVAILABLE or not self.config.TELEGRAM_BOT_TOKEN:
            logger.warning("Telegram not configured")
            await asyncio.Event().wait()
            return
        self._app = ApplicationBuilder().token(self.config.TELEGRAM_BOT_TOKEN).build()
        self._app.add_handler(CommandHandler("start",     self._cmd_start))
        self._app.add_handler(CommandHandler("status",    self._cmd_status))
        self._app.add_handler(CommandHandler("positions", self._cmd_positions))
        self._app.add_handler(CommandHandler("pause",     self._cmd_pause))
        self._app.add_handler(CommandHandler("resume",    self._cmd_resume))
        self._app.add_handler(CommandHandler("help",      self._cmd_help))
        self._app.add_handler(CommandHandler("report",    self._cmd_report))
        self._bot = self._app.bot
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()
        await self.send_message(
            "🚀 *CryptoBot EXTREME is online!*\n\n"
            "🔥 New features active:\n"
            "✅ Entry timing filter\n"
            "✅ Buy pressure analysis\n"
            "✅ Fast exit mode\n"
            "✅ Moonbag strategy\n"
            "✅ Volume drop detection\n\n"
            "Watching: Solana | ETH | Base | BSC\n"
            "Type /help for commands."
        )
        logger.info("Telegram bot started")

        # Start weekly report scheduler
        asyncio.create_task(self._weekly_report_scheduler())

        await asyncio.Event().wait()

    async def stop(self):
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()

    # ── Alerts ────────────────────────────────────────────────

    async def send_opportunity(self, candidate, risk, ai):
        bp = candidate.buy_pressure()
        entry_quality = "🟢 EARLY ENTRY" if candidate.price_change_5m < 25 else "🟡 MID ENTRY"
        await self.send_message(
            f"🔥 *OPPORTUNITY FOUND*\n\n"
            f"*{candidate.symbol}* — {candidate.chain.upper()}\n"
            f"Price: `${candidate.price_usd:.8f}`\n"
            f"📈 +{candidate.price_change_5m:.1f}% (5m) | +{candidate.price_change_1h:.1f}% (1h)\n"
            f"💧 Liquidity: ${candidate.liquidity_usd:,.0f}\n"
            f"📊 Buy pressure: {bp:.0f}%\n"
            f"⏰ Token age: {candidate.age_hours:.1f} hours\n"
            f"🎯 Entry: {entry_quality}\n\n"
            f"🛡️ *Safety*\n{risk.summary()}\n\n"
            f"🤖 *AI Signal*\n{ai.summary()}\n\n"
            f"🔗 [DEX Screener]({candidate.url}) | [RugCheck]({risk.rugcheck_url})\n\n"
            f"⚡ Checking entry timing..."
        )

    async def send_trade_executed(self, candidate, trade):
        await self.send_message(
            f"✅ *TRADE EXECUTED*\n\n"
            f"Bought *{candidate.symbol}* on {candidate.chain.upper()}\n"
            f"Entry: `${candidate.price_usd:.8f}`\n"
            f"Amount: `${self.config.trade_size_for_chain(candidate.chain):.2f}`\n\n"
            f"🎯 TP1: +100% (sell 50% — moonbag)\n"
            f"🎯 TP2: +200% (sell remaining)\n"
            f"🛑 SL: -20%\n"
            f"⚡ Fast exit: ON (volume watch active)"
        )

    async def send_trade_failed(self, candidate, error):
        await self.send_message(
            f"❌ *TRADE SKIPPED*\n\n"
            f"Token: *{candidate.symbol}* ({candidate.chain.upper()})\n"
            f"Reason: `{error}`"
        )

    async def send_skipped(self, candidate, reason):
        await self.send_message(
            f"⏭️ *Skipped {candidate.symbol}* ({candidate.chain.upper()})\n"
            f"Reason: {reason}"
        )

    async def send_moonbag_triggered(self, pos, price, pnl_pct):
        await self.send_message(
            f"🌙 *MOONBAG TRIGGERED!*\n\n"
            f"*{pos.symbol}* ({pos.chain.upper()})\n"
            f"Sold 50% at +{pnl_pct:.0f}%\n"
            f"💰 Original investment recovered!\n"
            f"🚀 Remaining 50% riding FREE!\n"
            f"🛑 Stop loss moved to breakeven\n\n"
            f"You cannot lose on this trade now! 🎉"
        )

    async def send_fast_exit(self, pos, price, pnl_pct, volume_drop):
        await self.send_message(
            f"⚡ *FAST EXIT TRIGGERED!*\n\n"
            f"*{pos.symbol}* ({pos.chain.upper()})\n"
            f"Volume dropped {volume_drop:.0f}% suddenly!\n"
            f"Exiting at {pnl_pct:+.1f}% before dump\n\n"
            f"🛡️ Protected your profits!"
        )

    async def send_tp_hit(self, pos, price, pnl):
        await self.send_message(
            f"🎯 *TAKE PROFIT HIT!*\n\n"
            f"*{pos.symbol}* ({pos.chain.upper()})\n"
            f"PnL: `+{pnl:.1f}%` 💰\n"
            f"Total PnL: `${self.trader.total_pnl:+.2f}`"
        )

    async def send_sl_hit(self, pos, price, pnl):
        await self.send_message(
            f"🛑 *STOP LOSS HIT*\n\n"
            f"*{pos.symbol}* ({pos.chain.upper()})\n"
            f"PnL: `{pnl:.1f}%`\n"
            f"Protected from bigger loss! 🛡️"
        )

    # ── Commands ──────────────────────────────────────────────

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "👋 *CryptoBot EXTREME Active!*\n\n"
            "Your AI-powered 24/7 trading bot is running.\n"
            "Use /help for all commands.",
            parse_mode=ParseMode.MARKDOWN
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uptime = datetime.utcnow() - self._start_time
        hours  = int(uptime.total_seconds() / 3600)
        state  = "⏸️ PAUSED" if self._paused else "✅ RUNNING"
        await update.message.reply_text(
            f"📊 *Bot Status*\n\n"
            f"State: {state}\n"
            f"Uptime: {hours} hours\n"
            f"Open positions: {len(self.trader.positions)}/{self.config.MAX_OPEN_POSITIONS}\n"
            f"Total trades: {self.trader.total_trades}\n"
            f"Win rate: {self.trader.winning_trades}/{self.trader.total_trades}\n"
            f"Total PnL: ${self.trader.total_pnl:+.2f}\n\n"
            f"Chains: {', '.join(self.config.TARGET_CHAINS)}\n"
            f"Min liquidity: $50,000\n"
            f"Max pump to enter: 40%\n"
            f"Min buy pressure: 55%",
            parse_mode=ParseMode.MARKDOWN
        )

    async def _cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            self.trader.get_positions_summary(),
            parse_mode=ParseMode.MARKDOWN
        )

    async def _cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self._paused = True
        await update.message.reply_text("⏸️ Bot paused. Use /resume to restart.")

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self._paused = False
        await update.message.reply_text("▶️ Bot resumed! Scanning for opportunities...")

    async def _cmd_report(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            await self._build_report(),
            parse_mode=ParseMode.MARKDOWN
        )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "📖 *Commands*\n\n"
            "/status — Bot state and performance\n"
            "/positions — Open trades\n"
            "/report — Full profit report\n"
            "/pause — Stop scanning\n"
            "/resume — Resume scanning\n"
            "/help — This message\n\n"
            "🔔 *Auto alerts:*\n"
            "• Opportunity found\n"
            "• Trade executed\n"
            "• Moonbag triggered 🌙\n"
            "• Fast exit triggered ⚡\n"
            "• Take profit hit 🎯\n"
            "• Stop loss hit 🛑\n"
            "• Weekly profit report 📊",
            parse_mode=ParseMode.MARKDOWN
        )

    # ── Weekly Report ─────────────────────────────────────────

    async def _weekly_report_scheduler(self):
        """Send weekly profit report every Monday at 8AM UTC."""
        while True:
            now = datetime.utcnow()
            # Calculate next Monday 8AM
            days_until_monday = (7 - now.weekday()) % 7 or 7
            next_monday = now + timedelta(days=days_until_monday)
            next_monday = next_monday.replace(hour=8, minute=0, second=0, microsecond=0)
            wait_seconds = (next_monday - now).total_seconds()
            await asyncio.sleep(wait_seconds)
            await self.send_message(await self._build_report())

    async def _build_report(self) -> str:
        win_rate = (
            f"{(self.trader.winning_trades/self.trader.total_trades*100):.0f}%"
            if self.trader.total_trades > 0 else "N/A"
        )
        return (
            f"📊 *Weekly Performance Report*\n\n"
            f"Total trades: {self.trader.total_trades}\n"
            f"Winning trades: {self.trader.winning_trades}\n"
            f"Win rate: {win_rate}\n"
            f"Total PnL: `${self.trader.total_pnl:+.2f}`\n"
            f"Open positions: {len(self.trader.positions)}\n\n"
            f"🔥 Bot is running 24/7 — keep stacking! 🇳🇬🚀"
        )

    # ── Helper ────────────────────────────────────────────────

    async def send_message(self, text):
        if not TELEGRAM_AVAILABLE or not self._bot:
            logger.info("ALERT: %s", text[:100].replace("*","").replace("`",""))
            return
        try:
            await self._bot.send_message(
                chat_id=self.config.TELEGRAM_CHAT_ID,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.error("Telegram send error: %s", e)
