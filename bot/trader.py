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

    async def buy(self, candidate, ai_result) -> TradeResult:
        if len(self.positions) >= self.config.MAX_OPEN_POSITIONS:
            return TradeResult(success=False, error="Max open positions reached")
        size_usd = self.config.trade_size_for_chain(candidate.chain)
        logger.info("Buying %s for $%.2f on %s", candidate.symbol, size_usd, candidate.chain)
        if not self._has_key(candidate.chain):
            return TradeResult(success=False, error=f"No private key for {candidate.chain}")
        tp = candidate.price_usd * (1 + self.config.TAKE_PROFIT_PCT / 100)
        sl = candidate.price_usd * (1 - self.config.STOP_LOSS_PCT  / 100)
        pos = Position(
            symbol=candidate.symbol, chain=candidate.chain,
            address=candidate.address, entry_price=candidate.price_usd,
            quantity=0, size_usd=size_usd,
            take_profit=ai_result.target_price or tp,
            stop_loss=ai_result.stop_price or sl,
            peak_price=candidate.price_usd,
        )
        self.positions[candidate.address] = pos
        if not self._monitor_task or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_positions())
        return TradeResult(success=True, price=candidate.price_usd, tx_hash="simulated_tx")

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
                    price = await self._get_current_price(address)
                    if price is None:
                        continue
                    if self.config.TRAILING_STOP and price > pos.peak_price:
                        pos.peak_price = price
                        trail = price * (1 - self.config.TRAILING_STOP_PCT / 100)
                        pos.stop_loss = max(pos.stop_loss, trail)
                    if price >= pos.take_profit:
                        logger.info("TP hit for %s @ $%.8f", pos.symbol, price)
                        await self._close_position(pos, price, "take_profit")
                    elif price <= pos.stop_loss:
                        logger.info("SL hit for %s @ $%.8f", pos.symbol, price)
                        await self._close_position(pos, price, "stop_loss")
                except Exception as e:
                    logger.error("Monitor error for %s: %s", pos.symbol, e)

    async def _get_current_price(self, address) -> Optional[float]:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        data = await r.json()
                        pairs = data.get("pairs") or []
                        if pairs:
                            return float(pairs[0].get("priceUsd", 0) or 0)
        except Exception:
            pass
        return None

    async def _close_position(self, pos, current_price, reason):
        pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100
        logger.info("Closing %s | PnL: %+.1f%% | Reason: %s", pos.symbol, pnl_pct, reason)
        del self.positions[pos.address]
        return pnl_pct

    def get_positions_summary(self):
        if not self.positions:
            return "No open positions."
        lines = ["📊 Open Positions:"]
        for pos in self.positions.values():
            lines.append(f"• {pos.symbol} ({pos.chain}) | Entry: ${pos.entry_price:.6f} | TP: ${pos.take_profit:.6f} | SL: ${pos.stop_loss:.6f}")
        return "\n".join(lines)
