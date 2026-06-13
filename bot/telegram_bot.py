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
        self.config      = config
        self.trader      = trader
        self._app        = None
        self._bot        = None
        self._paused     = False
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
        self._app.add_handler(CommandHandler("log",       self._cmd_log))
        self._app.add_handler(CommandHandler("pause",     self._cmd_pause))
        self._app.add_handler(CommandHandler("resume",    self._cmd_resume))
        self._app.add_handler(CommandHandler("help",      self._cmd_help))
        self._app.add_handler(CommandHandler("report",    self._cmd_report))
        self._bot = self._app.bot
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()
        mode = "📄 PAPER TRADING MODE" if self.config.PAPER_TRADING else "💰 LIVE TRADING MODE"
        await self.send_message(
            f"🚀 *CryptoBot EXTREME is online!*\n\n"
            f"⚙️ Mode: *{mode}*\n"
            f"⛓️ Chain: *Solana only*\n"
            f"💵 Trade size: *${self.config.TRADE_SIZE_USD_SOLANA:.2f}* (Tier A)\n"
            f"💵 Trade size: *${self.config.tier_trade_size('B'):.2f}* (Tier B)\n"
            f"🔒 Fee buffer: *${self.config.FEE_BUFFER_USD:.2f}*\n\n"
            f"✅ Active features:\n"
            f"• Tiered entry system (A/B/C)\n"
            f"• Hard kill switches\n"
            f"• Hype token tracker 🔥\n"
            f"• Paper P&L tracking\n"
            f"• Moonbag strategy\n"
            f"• Fast exit on volume drop\n\n"
            f"Type /help for commands."
        )
        logger.info("Telegram bot started")
        asyncio.create_task(self._weekly_report_scheduler())
        await asyncio.Event().wait()

    async def stop(self):
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()

    # ── Alerts ────────────────────────────────────────────────

    async def send_hype_alert(self, candidate, risk):
        tier_emoji = {"A": "🟢", "B": "🟡", "C": "🔴", "REJECTED": "⛔"}.get(risk.tier, "⚪")
        await self.send_message(
            f"🔥 *HYPE TOKEN DETECTED*\n\n"
            f"*{candidate.symbol}* — SOLANA\n"
            f"Price: `${candidate.price_usd:.8f}`\n"
            f"📈 +{candidate.price_change_5m:.1f}% (5m) | +{candidate.price_change_1h:.1f}% (1h)\n"
            f"💧 Liquidity: ${candidate.liquidity_usd:,.0f}\n"
            f"📊 Buy pressure: {candidate.buy_pressure():.0f}%\n"
            f"⏰ Age: {candidate.age_hours:.1f}h\n\n"
            f"🛡️ *Safety:* {risk.badge()} ({risk.risk_score}/1000)\n"
            f"Tier: {tier_emoji} *{risk.tier}*\n\n"
            f"🔗 [DEX Screener]({candidate.url}) | [RugCheck]({risk.rugcheck_url})"
        )

    async def send_opportunity(self, candidate, risk, ai):
        bp = candidate.buy_pressure()
        entry_quality = "🟢 EARLY ENTRY" if candidate.price_change_5m < 25 else "🟡 MID ENTRY"
        tier_emoji = {"A": "🟢 FULL $3", "B": "🟡 HALF $1.50", "C": "🔴 ALERT ONLY"}.get(risk.tier, "⚪")
        mode = "📄 PAPER" if self.config.PAPER_TRADING else "💰 LIVE"
        await self.send_message(
            f"🔥 *OPPORTUNITY FOUND* ({mode})\n\n"
            f"*{candidate.symbol}* — SOLANA\n"
            f"Price: `${candidate.price_usd:.8f}`\n"
            f"📈 +{candidate.price_change_5m:.1f}% (5m) | +{candidate.price_change_1h:.1f}% (1h)\n"
            f"💧 Liquidity: ${candidate.liquidity_usd:,.0f}\n"
            f"📊 Buy pressure: {bp:.0f}%\n"
            f"⏰ Token age: {candidate.age_hours:.1f} hours\n"
            f"🎯 Entry: {entry_quality}\n\n"
            f"🛡️ *Safety*\n{risk.summary()}\n\n"
            f"💰 *Trade Size:* {tier_emoji}\n\n"
            f"🤖 *AI Signal*\n{ai.summary()}\n\n"
            f"🔗 [DEX Screener]({candidate.url}) | [RugCheck]({risk.rugcheck_url})\n\n"
            f"⚡ Checking entry timing..."
        )

    async def send_trade_executed(self, candidate, trade, tier="A"):
        size = self.config.tier_trade_size(tier)
        mode = "📄 PAPER TRADE" if self.config.PAPER_TRADING else "✅ LIVE TRADE"
        balance_line = ""
        if self.config.PAPER_TRADING:
            balance_line = f"📄 Paper Balance: `${self.trader.paper_balance:.2f}`\n"
        await self.send_message(
            f"{mode} *EXECUTED*\n\n"
            f"Bought *{candidate.symbol}* on SOLANA\n"
            f"Entry: `${candidate.price_usd:.8f}`\n"
            f"Amount: `${size:.2f}` (Tier {tier})\n"
            f"{balance_line}\n"
            f"🎯 TP1: +100% (sell 50% — moonbag)\n"
            f"🎯 TP2: +200% (sell remaining)\n"
            f"🛑 SL: -20%\n"
            f"⚡ Fast exit: ON"
        )

    async def send_trade_failed(self, candidate, error):
        await self.send_message(
            f"❌ *TRADE SKIPPED*\n\n"
            f"Token: *{candidate.symbol}* (SOLANA)\n"
            f"Reason: `{error}`"
        )

    async def send_skipped(self, candidate, reason):
        await self.send_message(
            f"⏭️ *Skipped {candidate.symbol}* (SOLANA)\n"
            f"Reason: {reason}"
        )

    async def send_moonbag_triggered(self, pos, price, pnl_pct):
        mode = "📄 PAPER" if self.config.PAPER_TRADING else "💰 LIVE"
        balance_line = f"\n📄 Paper Balance: `${self.trader.paper_balance:.2f}`" if self.config.PAPER_TRADING else ""
        await self.send_message(
            f"🌙 *MOONBAG TRIGGERED!* ({mode})\n\n"
            f"*{pos.symbol}* (SOLANA)\n"
            f"Sold 50% at +{pnl_pct:.0f}%\n"
            f"💰 Original investment recovered!\n"
            f"🚀 Remaining 50% riding FREE!\n"
            f"🛑 Stop loss moved to breakeven"
            f"{balance_line}"
        )

    async def send_fast_exit(self, pos, price, pnl_pct, volume_drop):
        mode = "📄 PAPER" if self.config.PAPER_TRADING else "💰 LIVE"
        await self.send_message(
            f"⚡ *FAST EXIT TRIGGERED!* ({mode})\n\n"
            f"*{pos.symbol}* (SOLANA)\n"
            f"Volume dropped {volume_drop:.0f}% suddenly!\n"
            f"Exiting at {pnl_pct:+.1f}%\n\n"
            f"🛡️ Protected your profits!"
        )

    async def send_tp_hit(self, pos, price, pnl):
        mode = "📄 PAPER" if self.config.PAPER_TRADING else "💰 LIVE"
        balance_line = f"\n📄 Paper Balance: `${self.trader.paper_balance:.2f}`" if self.config.PAPER_TRADING else ""
        await self.send_message(
            f"🎯 *TAKE PROFIT HIT!* ({mode})\n\n"
            f"*{pos.symbol}* (SOLANA)\n"
            f"PnL: `+{pnl:.1f}%` 💰\n"
            f"Total PnL: `${self.trader.total_pnl:+.2f}`"
            f"{balance_line}"
        )

    async def send_sl_hit(self, pos, price, pnl):
        mode = "📄 PAPER" if self.config.PAPER_TRADING else "💰 LIVE"
        balance_line = f"\n📄 Paper Balance: `${self.trader.paper_balance:.2f}`" if self.config.PAPER_TRADING else ""
        await self.send_message(
            f"🛑 *STOP LOSS HIT* ({mode})\n\n"
            f"*{pos.symbol}* (SOLANA)\n"
            f"PnL: `{pnl:.1f}%`\n"
            f"Protected from bigger loss! 🛡️"
            f"{balance_line}"
        )

    # ── Commands ──────────────────────────────────────────────

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        mode = "📄 PAPER MODE" if self.config.PAPER_TRADING else "💰 LIVE MODE"
        await update.message.reply_text(
            f"👋 *CryptoBot EXTREME Active!*\n\n"
            f"Mode: *{mode}*\n"
            f"Chain: *Solana only*\n\n"
            f"Use /help for all commands.",
            parse_mode=ParseMode.MARKDOWN
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uptime = datetime.utcnow() - self._start_time
        hours  = int(uptime.total_seconds() / 3600)
        state  = "⏸️ PAUSED" if self._paused else "✅ RUNNING"
        mode   = "📄 PAPER" if self.config.PAPER_TRADING else "💰 LIVE"
        balance_line = f"Paper Balance: ${self.trader.paper_balance:.2f}\n" if self.config.PAPER_TRADING else ""
        win_rate = f"{self.trader.winning_trades}/{self.trader.total_trades}" if self.trader.total_trades > 0 else "0/0"
        await update.message.reply_text(
            f"📊 *Bot Status*\n\n"
            f"State: {state}\n"
            f"Mode: {mode}\n"
            f"Uptime: {hours}h\n"
            f"Chain: Solana only\n"
            f"{balance_line}"
            f"Open positions: {len(self.trader.positions)}/{self.config.MAX_OPEN_POSITIONS}\n"
            f"Total trades: {self.trader.total_trades}\n"
            f"Win rate: {win_rate}\n"
            f"Total PnL: ${self.trader.total_pnl:+.2f}\n\n"
            f"Trade sizes:\n"
            f"• Tier A: ${self.config.TRADE_SIZE_USD_SOLANA:.2f}\n"
            f"• Tier B: ${self.config.tier_trade_size('B'):.2f}\n"
            f"• Tier C: Alert only",
            parse_mode=ParseMode.MARKDOWN
        )

    async def _cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            self.trader.get_positions_summary(),
            parse_mode=ParseMode.MARKDOWN
        )

    async def _cmd_log(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            self.trader.get_trade_log_summary(),
            parse_mode=ParseMode.MARKDOWN
        )

    async def _cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self._paused = True
        await update.message.reply_text("⏸️ Bot paused. Use /resume to restart.")

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self._paused = False
        await update.message.reply_text("▶️ Bot resumed! Scanning Solana...")

    async def _cmd_report(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            await self._build_report(),
            parse_mode=ParseMode.MARKDOWN
        )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        mode = "📄 PAPER MODE" if self.config.PAPER_TRADING else "💰 LIVE MODE"
        await update.message.reply_text(
            f"📖 *Commands* ({mode})\n\n"
            f"/status — Bot state and performance\n"
            f"/positions — Open trades\n"
            f"/log — Recent trade history\n"
            f"/report — Full profit report\n"
            f"/pause — Stop scanning\n"
            f"/resume — Resume scanning\n"
            f"/help — This message\n\n"
            f"🔔 *Auto alerts:*\n"
            f"• 🔥 Hype token detected\n"
            f"• Opportunity found\n"
            f"• Trade executed\n"
            f"• 🌙 Moonbag triggered\n"
            f"• ⚡ Fast exit triggered\n"
            f"• 🎯 Take profit hit\n"
            f"• 🛑 Stop loss hit\n"
            f"• 📊 Weekly report",
            parse_mode=ParseMode.MARKDOWN
        )

    # ── Weekly Report ─────────────────────────────────────────

    async def _weekly_report_scheduler(self):
        while True:
            now = datetime.utcnow()
            days_until_monday = (7 - now.weekday()) % 7 or 7
            next_monday = now + timedelta(days=days_until_monday)
            next_monday = next_monday.replace(hour=8, minute=0, second=0, microsecond=0)
            wait_seconds = (next_monday - now).total_seconds()
            await asyncio.sleep(wait_seconds)
            await self.send_message(await self._build_report())

    async def _build_report(self) -> str:
        mode = "📄 PAPER MODE" if self.config.PAPER_TRADING else "💰 LIVE MODE"
        win_rate = (
            f"{(self.trader.winning_trades / self.trader.total_trades * 100):.0f}%"
            if self.trader.total_trades > 0 else "N/A"
        )
        balance_line = f"Paper Balance: `${self.trader.paper_balance:.2f}`\n" if self.config.PAPER_TRADING else ""
        return (
            f"📊 *Weekly Performance Report*\n"
            f"Mode: {mode}\n\n"
            f"Total trades: {self.trader.total_trades}\n"
            f"Winning trades: {self.trader.winning_trades}\n"
            f"Losing trades: {self.trader.losing_trades}\n"
            f"Win rate: {win_rate}\n"
            f"Total PnL: `${self.trader.total_pnl:+.2f}`\n"
            f"{balance_line}"
            f"Open positions: {len(self.trader.positions)}\n\n"
            f"🔥 Solana only — keep stacking! 🇳🇬🚀"
        )

    # ── Helper ────────────────────────────────────────────────

    async def send_message(self, text):
        if not TELEGRAM_AVAILABLE or not self._bot:
            logger.info("ALERT: %s", text[:100].replace("*", "").replace("`", ""))
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
