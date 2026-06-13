import asyncio
import logging
import aiohttp
from dataclasses import dataclass, field
from typing import Dict, Optional
from datetime import datetime

logger = logging.getLogger("trader")

@dataclass
class Position:
    symbol: str
    chain: str
    address: str
    entry_price: float
    quantity: float
    size_usd: float
    take_profit: float
    stop_loss: float
    peak_price: float
    tier: str = "A"
    moonbag_sold: bool = False
    last_volume: float = 0
    opened_at: datetime = field(default_factory=datetime.utcnow)
    tx_hash: str = ""

@dataclass
class TradeResult:
    success: bool
    tx_hash: str = ""
    amount_in: float = 0
    amount_out: float = 0
    price: float = 0
    error: str = ""

class Trader:
    def __init__(self, config):
        self.config = config
        self.positions: Dict[str, Position] = {}
        self._monitor_task = None
        self._telegram = None

        # Stats
        self.total_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0

        # Paper trading balance
        self.paper_balance = config.PAPER_STARTING_BALANCE if config.PAPER_TRADING else 0.0
        self.paper_reserved_fees = config.FEE_BUFFER_USD
        self.trade_log = []

    def set_telegram(self, telegram):
        self._telegram = telegram

    async def buy(self, candidate, ai_result, risk_result) -> TradeResult:
        if len(self.positions) >= self.config.MAX_OPEN_POSITIONS:
            return TradeResult(success=False, error="Max open positions reached")

        # Get trade size based on tier
        tier = risk_result.tier if hasattr(risk_result, "tier") else "A"
        size_usd = self.config.tier_trade_size(tier)
        if size_usd <= 0:
            return TradeResult(success=False, error=f"Tier {tier} — no trade")

        # Check paper balance
        if self.config.PAPER_TRADING:
            available = self.paper_balance - self.paper_reserved_fees
            if available < size_usd:
                return TradeResult(success=False, error=f"Insufficient paper balance (${available:.2f} available)")
        else:
            if not self.config.SOLANA_PRIVATE_KEY:
                return TradeResult(success=False, error="No private key for solana")

        # Wait for stable entry
        stable = await self._wait_for_stable_entry(candidate)
        if not stable:
            return TradeResult(success=False, error="Price unstable — skipping entry")

        if candidate.buy_pressure() < 55:
            return TradeResult(success=False, error="Buy pressure too low before entry")

        tp = candidate.price_usd * 2.0
        sl = candidate.price_usd * 0.80

        pos = Position(
            symbol=candidate.symbol,
            chain=candidate.chain,
            address=candidate.address,
            entry_price=candidate.price_usd,
            quantity=size_usd / candidate.price_usd if candidate.price_usd > 0 else 0,
            size_usd=size_usd,
            take_profit=ai_result.target_price or tp,
            stop_loss=ai_result.stop_price or sl,
            peak_price=candidate.price_usd,
            tier=tier,
            moonbag_sold=False,
            last_volume=candidate.volume_5m,
        )

        if self.config.PAPER_TRADING:
            self.paper_balance -= size_usd
            logger.info(
                "[PAPER] Bought %s for $%.2f | Balance: $%.2f",
                candidate.symbol, size_usd, self.paper_balance
            )
        else:
            logger.info("Buying %s for $%.2f on Solana", candidate.symbol, size_usd)

        self.positions[candidate.address] = pos
        self.total_trades += 1

        self.trade_log.append({
            "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            "symbol": candidate.symbol,
            "action": "BUY",
            "price": candidate.price_usd,
            "size_usd": size_usd,
            "tier": tier,
            "paper": self.config.PAPER_TRADING,
        })

        if not self._monitor_task or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_positions())

        mode = "PAPER" if self.config.PAPER_TRADING else "LIVE"
        return TradeResult(success=True, price=candidate.price_usd, tx_hash=f"{mode}-pending")

    async def _wait_for_stable_entry(self, candidate) -> bool:
        logger.info("Checking entry stability for %s...", candidate.symbol)
        prev_price = candidate.price_usd
        await asyncio.sleep(15)
        current = await self._get_current_price(candidate.address)
        if current is None:
            return True
        change = (current - prev_price) / prev_price * 100
        if change > 15:
            logger.info("%s still pumping (%.1f%%) — waiting", candidate.symbol, change)
            await asyncio.sleep(20)
            current2 = await self._get_current_price(candidate.address)
            if current2 is None:
                return True
            if current2 < current * 0.85:
                logger.info("%s dumped after pump — skipping", candidate.symbol)
                return False
            return True
        if change < -15:
            logger.info("%s dropping too fast — skipping", candidate.symbol)
            return False
        return True

    async def _monitor_positions(self):
        logger.info("Position monitor running (%d open)", len(self.positions))
        while self.positions:
            await asyncio.sleep(15)
            for address, pos in list(self.positions.items()):
                try:
                    price, volume = await self._get_price_and_volume(address)
                    if price is None:
                        continue

                    pnl_pct = ((price - pos.entry_price) / pos.entry_price) * 100

                    # Fast exit on volume drop
                    if volume and pos.last_volume > 0:
                        volume_drop = (pos.last_volume - volume) / pos.last_volume * 100
                        if volume_drop > 50 and pnl_pct > 0:
                            logger.info("FAST EXIT: %s volume dropped %.0f%%", pos.symbol, volume_drop)
                            if self._telegram:
                                await self._telegram.send_fast_exit(pos, price, pnl_pct, volume_drop)
                            await self._close_position(pos, price, "fast_exit")
                            continue
                    if volume:
                        pos.last_volume = volume

                    # Trailing stop
                    if self.config.TRAILING_STOP and price > pos.peak_price:
                        pos.peak_price = price
                        trail = price * (1 - self.config.TRAILING_STOP_PCT / 100)
                        pos.stop_loss = max(pos.stop_loss, trail)

                    # Moonbag: sell 50% at +100%
                    if pnl_pct >= 100 and not pos.moonbag_sold:
                        logger.info("MOONBAG: Selling 50%% of %s at +%.1f%%", pos.symbol, pnl_pct)
                        await self._sell_partial(pos, price, 50)
                        pos.moonbag_sold = True
                        pos.stop_loss = pos.entry_price
                        if self._telegram:
                            await self._telegram.send_moonbag_triggered(pos, price, pnl_pct)
                        continue

                    # Sell moonbag at +200%
                    if pos.moonbag_sold and pnl_pct >= 200:
                        await self._close_position(pos, price, "moonbag_tp")
                        continue

                    # Normal TP
                    if not pos.moonbag_sold and price >= pos.take_profit:
                        await self._close_position(pos, price, "take_profit")
                        continue

                    # Stop loss
                    if price <= pos.stop_loss:
                        await self._close_position(pos, price, "stop_loss")

                except Exception as e:
                    logger.error("Monitor error for %s: %s", pos.symbol, e)

    async def _get_price_and_volume(self, address):
        url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        data = await r.json()
                        pairs = data.get("pairs") or []
                        if pairs:
                            price  = float(pairs[0].get("priceUsd", 0) or 0)
                            volume = float(pairs[0].get("volume", {}).get("m5", 0) or 0)
                            return price, volume
        except Exception:
            pass
        return None, None

    async def _get_current_price(self, address) -> Optional[float]:
        price, _ = await self._get_price_and_volume(address)
        return price

    async def _sell_partial(self, pos, current_price, pct):
        sold_usd = pos.size_usd * (pct / 100)
        pnl = ((current_price - pos.entry_price) / pos.entry_price) * 100
        profit = sold_usd * (pnl / 100)
        self.total_pnl += profit
        if self.config.PAPER_TRADING:
            self.paper_balance += sold_usd + profit
        if pnl > 0:
            self.winning_trades += 1
        self.trade_log.append({
            "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            "symbol": pos.symbol,
            "action": "SELL 50%",
            "price": current_price,
            "pnl_pct": pnl,
            "pnl_usd": profit,
            "paper": self.config.PAPER_TRADING,
        })
        logger.info("Partial sell: %s | 50%% | PnL: %+.1f%%", pos.symbol, pnl)

    async def _close_position(self, pos, current_price, reason):
        pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100
        pnl_usd = pos.size_usd * (pnl_pct / 100)
        self.total_pnl += pnl_usd
        if self.config.PAPER_TRADING:
            self.paper_balance += pos.size_usd + pnl_usd
        if pnl_pct > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1
        self.trade_log.append({
            "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            "symbol": pos.symbol,
            "action": f"CLOSE ({reason})",
            "price": current_price,
            "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd,
            "paper": self.config.PAPER_TRADING,
        })
        logger.info(
            "Closing %s | PnL: %+.1f%% ($%+.2f) | Reason: %s",
            pos.symbol, pnl_pct, pnl_usd, reason
        )
        if pos.address in self.positions:
            del self.positions[pos.address]
        if self._telegram:
            if pnl_pct > 0:
                await self._telegram.send_tp_hit(pos, current_price, pnl_pct)
            else:
                await self._telegram.send_sl_hit(pos, current_price, pnl_pct)
        return pnl_pct

    def get_positions_summary(self) -> str:
        mode = "📄 PAPER MODE" if self.config.PAPER_TRADING else "💰 LIVE MODE"
        lines = [f"📊 *Open Positions* ({mode}):"]
        if not self.positions:
            lines.append("No open positions.")
        else:
            for pos in self.positions.values():
                moonbag = "🌙 Moonbag active" if pos.moonbag_sold else ""
                lines.append(
                    f"• *{pos.symbol}* | Tier {pos.tier}\n"
                    f"  Entry: ${pos.entry_price:.8f}\n"
                    f"  TP: ${pos.take_profit:.8f} | SL: ${pos.stop_loss:.8f}\n"
                    f"  {moonbag}"
                )
        if self.config.PAPER_TRADING:
            lines.append(f"\n💵 *Paper Balance:* ${self.paper_balance:.2f}")
            lines.append(f"🔒 *Fee Buffer:* ${self.paper_reserved_fees:.2f}")
        lines.append(f"\n📈 *Total PnL:* ${self.total_pnl:+.2f}")
        lines.append(f"🎯 *Win rate:* {self.winning_trades}/{self.total_trades} trades")
        return "\n".join(lines)

    def get_trade_log_summary(self) -> str:
        if not self.trade_log:
            return "No trades recorded yet."
        lines = ["📋 *Recent Trades:*\n"]
        for t in self.trade_log[-10:]:
            pnl = f" | PnL: {t['pnl_pct']:+.1f}%" if "pnl_pct" in t else ""
            lines.append(f"• {t['time']} | {t['symbol']} | {t['action']}{pnl}")
        return "\n".join(lines)
