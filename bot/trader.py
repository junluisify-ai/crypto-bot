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
    SLIPPAGE_BPS = 1000  # 10% slippage

    def __init__(self, config):
        self.config = config
        self.positions: Dict[str, Position] = {}
        self._monitor_task = None
        self.total_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0

    async def buy(self, candidate, ai_result) -> TradeResult:
        if len(self.positions) >= self.config.MAX_OPEN_POSITIONS:
            return TradeResult(success=False, error="Max open positions reached")

        # Wait for stable entry — avoid buying at peak
        stable = await self._wait_for_stable_entry(candidate)
        if not stable:
            return TradeResult(success=False, error="Price unstable - skipping entry")

        # Check buy pressure one more time before buying
        if candidate.buy_pressure() < 55:
            return TradeResult(success=False, error="Buy pressure too low before entry")

        size_usd = self.config.trade_size_for_chain(candidate.chain)
        logger.info("Buying %s for $%.2f on %s", candidate.symbol, size_usd, candidate.chain)

        if not self._has_key(candidate.chain):
            return TradeResult(success=False, error=f"No private key for {candidate.chain}")

        # Moonbag TPs: sell 50% at +100%, rest at +200%
        tp = candidate.price_usd * 2.0   # +100%
        sl = candidate.price_usd * 0.80  # -20%

        pos = Position(
            symbol=candidate.symbol,
            chain=candidate.chain,
            address=candidate.address,
            entry_price=candidate.price_usd,
            quantity=0,
            size_usd=size_usd,
            take_profit=ai_result.target_price or tp,
            stop_loss=ai_result.stop_price or sl,
            peak_price=candidate.price_usd,
            moonbag_sold=False,
            last_volume=candidate.volume_5m,
        )
        self.positions[candidate.address] = pos
        self.total_trades += 1

        if not self._monitor_task or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_positions())

        return TradeResult(success=True, price=candidate.price_usd, tx_hash="pending")

    async def _wait_for_stable_entry(self, candidate) -> bool:
        """
        Wait for price to stabilize before entering.
        Avoids buying at the very peak of a pump.
        """
        logger.info("Checking entry stability for %s...", candidate.symbol)
        prev_price = candidate.price_usd
        await asyncio.sleep(15)

        current = await self._get_current_price(candidate.address)
        if current is None:
            return True

        change = (current - prev_price) / prev_price * 100

        # If still pumping hard — wait more
        if change > 15:
            logger.info("%s still pumping (%.1f%%) — waiting for dip", candidate.symbol, change)
            await asyncio.sleep(20)
            current2 = await self._get_current_price(candidate.address)
            if current2 is None:
                return True
            # If dumping after pump — skip
            if current2 < current * 0.85:
                logger.info("%s dumped after pump — skipping", candidate.symbol)
                return False
            # If consolidated — good entry
            logger.info("%s consolidated — good entry!", candidate.symbol)
            return True

        # If dropping too fast — skip
        if change < -15:
            logger.info("%s dropping too fast (%.1f%%) — skipping", candidate.symbol, change)
            return False

        logger.info("%s price stable — entering!", candidate.symbol)
        return True

    def _has_key(self, chain):
        if chain == "solana":   return bool(self.config.SOLANA_PRIVATE_KEY)
        if chain == "ethereum": return bool(self.config.ETH_PRIVATE_KEY)
        if chain == "base":     return bool(self.config.ETH_PRIVATE_KEY)
        if chain == "bsc":      return bool(self.config.BSC_PRIVATE_KEY)
        return False

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

                    # FAST EXIT MODE — volume drops 50% suddenly
                    if volume and pos.last_volume > 0:
                        volume_drop = (pos.last_volume - volume) / pos.last_volume * 100
                        if volume_drop > 50 and pnl_pct > 0:
                            logger.info(
                                "FAST EXIT: %s volume dropped %.0f%% — exiting at +%.1f%%",
                                pos.symbol, volume_drop, pnl_pct
                            )
                            await self._telegram.send_fast_exit(pos, price, pnl_pct, volume_drop)
                            await self._close_position(pos, price, "fast_exit")
                            continue
                    if volume:
                        pos.last_volume = volume

                    # Update trailing stop
                    if self.config.TRAILING_STOP and price > pos.peak_price:
                        pos.peak_price = price
                        trail = price * (1 - self.config.TRAILING_STOP_PCT / 100)
                        pos.stop_loss = max(pos.stop_loss, trail)

                    # MOONBAG: Sell 50% at +100%
                    if pnl_pct >= 100 and not pos.moonbag_sold:
                        logger.info("MOONBAG: Selling 50%% of %s at +%.1f%%", pos.symbol, pnl_pct)
                        await self._sell_partial(pos, price, 50)
                        pos.moonbag_sold = True
                        pos.stop_loss = pos.entry_price  # Move SL to breakeven
                        await self._telegram.send_moonbag_triggered(pos, price, pnl_pct)
                        continue

                    # Sell remaining moonbag at +200%
                    if pos.moonbag_sold and pnl_pct >= 200:
                        logger.info("MOONBAG TP: Selling rest of %s at +%.1f%%", pos.symbol, pnl_pct)
                        await self._close_position(pos, price, "moonbag_tp")
                        continue

                    # Normal take profit
                    if not pos.moonbag_sold and price >= pos.take_profit:
                        logger.info("TP hit for %s @ $%.8f", pos.symbol, price)
                        await self._close_position(pos, price, "take_profit")
                        continue

                    # Stop loss
                    if price <= pos.stop_loss:
                        logger.info("SL hit for %s @ $%.8f", pos.symbol, price)
                        await self._close_position(pos, price, "stop_loss")

                except Exception as e:
                    logger.error("Monitor error for %s: %s", pos.symbol, e)

    async def _get_price_and_volume(self, address):
        """Get current price and 5m volume."""
        url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        data  = await r.json()
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
        self.total_pnl += sold_usd * (pnl / 100)
        if pnl > 0:
            self.winning_trades += 1
        logger.info(
            "Partial sell: %s | %.0f%% ($%.2f) | PnL: %+.1f%%",
            pos.symbol, pct, sold_usd, pnl
        )

    async def _close_position(self, pos, current_price, reason):
        pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100
        pnl_usd = pos.size_usd * (pnl_pct / 100)
        self.total_pnl += pnl_usd
        if pnl_pct > 0:
            self.winning_trades += 1
        logger.info(
            "Closing %s | Entry: $%.8f | Exit: $%.8f | PnL: %+.1f%% ($%+.2f) | Reason: %s",
            pos.symbol, pos.entry_price, current_price, pnl_pct, pnl_usd, reason
        )
        if pos.address in self.positions:
            del self.positions[pos.address]
        return pnl_pct

    def get_positions_summary(self) -> str:
        lines = ["📊 *Open Positions:*"]
        if not self.positions:
            lines.append("No open positions.")
        else:
            for pos in self.positions.values():
                moonbag = "🌙 Moonbag active" if pos.moonbag_sold else ""
                lines.append(
                    f"• *{pos.symbol}* ({pos.chain})\n"
                    f"  Entry: ${pos.entry_price:.8f}\n"
                    f"  TP: ${pos.take_profit:.8f} | SL: ${pos.stop_loss:.8f}\n"
                    f"  {moonbag}"
                )
        lines.append(f"\n📈 *Total PnL:* ${self.total_pnl:+.2f}")
        lines.append(f"🎯 *Win rate:* {self.winning_trades}/{self.total_trades} trades")
        return "\n".join(lines)
