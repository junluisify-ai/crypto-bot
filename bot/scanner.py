import asyncio
import logging
import aiohttp
from typing import Optional, List
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("scanner")

@dataclass
class TokenCandidate:
    chain: str
    address: str
    symbol: str
    name: str
    price_usd: float
    price_change_5m: float
    price_change_1h: float
    price_change_24h: float
    volume_5m: float
    volume_1h: float
    liquidity_usd: float
    market_cap: float
    pair_address: str
    dex_id: str
    url: str
    buys_5m: int = 0
    sells_5m: int = 0
    buys_1h: int = 0
    sells_1h: int = 0
    age_hours: float = 0
    is_hype: bool = False
    discovered_at: datetime = field(default_factory=datetime.utcnow)

    def buy_pressure(self) -> float:
        total = self.buys_5m + self.sells_5m
        if total == 0:
            return 50.0
        return (self.buys_5m / total) * 100

    def is_early_entry(self) -> bool:
        if self.price_change_5m > 40:
            return False
        if self.age_hours > 24:
            return False
        return True

    def summary(self) -> str:
        bp = self.buy_pressure()
        hype = " 🔥 HYPE" if self.is_hype else ""
        return (
            f"{self.symbol} (SOLANA){hype}\n"
            f"Price: ${self.price_usd:.8f} | +{self.price_change_5m:.1f}% (5m)\n"
            f"Vol 5m: ${self.volume_5m:,.0f} | Liq: ${self.liquidity_usd:,.0f}\n"
            f"Buy pressure: {bp:.0f}% | Age: {self.age_hours:.1f}h"
        )

class Scanner:
    TRENDING_ENDPOINT = "https://api.dexscreener.com/token-profiles/latest/v1"
    TOKENS_ENDPOINT   = "https://api.dexscreener.com/latest/dex/tokens"

    def __init__(self, config):
        self.config    = config
        self.running   = False
        self._session  = None
        self._seen     = set()
        self._risk     = None
        self._ai       = None
        self._trader   = None
        self._telegram = None

    def set_pipeline(self, risk, ai, trader, telegram):
        self._risk     = risk
        self._ai       = ai
        self._trader   = trader
        self._telegram = telegram
        if hasattr(trader, 'set_telegram'):
            trader.set_telegram(telegram)

    async def run(self):
        self.running = True
        logger.info("Scanner started — Solana only")
        async with aiohttp.ClientSession() as session:
            self._session = session
            while self.running:
                try:
                    await self._scan_cycle()
                except Exception as e:
                    logger.error("Scan cycle error: %s", e)
                await asyncio.sleep(self.config.SCAN_INTERVAL_SECONDS)

    async def stop(self):
        self.running = False

    async def _scan_cycle(self):
        logger.info("Scanning Solana for opportunities...")
        candidates = await self._get_trending()
        fresh = [c for c in candidates if c.address not in self._seen]
        logger.info("Found %d new Solana candidates", len(fresh))

        # Sort: hype tokens first, then by 5m price change
        top = sorted(fresh, key=lambda x: (x.is_hype, x.price_change_5m), reverse=True)

        for candidate in top[:self.config.MAX_TOKENS_PER_SCAN]:
            self._seen.add(candidate.address)
            asyncio.create_task(self._process_candidate(candidate))

        if len(self._seen) > 10000:
            self._seen = set(list(self._seen)[-5000:])

    async def _get_trending(self):
        try:
            async with self._session.get(
                self.TRENDING_ENDPOINT,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status != 200:
                    return []
                data = await r.json()
                addresses = [t.get("tokenAddress", "") for t in (data or [])[:30]]
                return await self._fetch_token_details(addresses)
        except Exception as e:
            logger.debug("Trending fetch error: %s", e)
            return []

    async def _fetch_token_details(self, addresses):
        if not addresses:
            return []
        results = []
        for i in range(0, len(addresses), 30):
            batch = addresses[i:i+30]
            url = f"{self.TOKENS_ENDPOINT}/{','.join(batch)}"
            try:
                async with self._session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    if r.status != 200:
                        continue
                    data = await r.json()
                    for pair in (data.get("pairs") or []):
                        c = self._pair_to_candidate(pair)
                        if c and self._passes_filters(c):
                            results.append(c)
            except Exception as e:
                logger.debug("Token details error: %s", e)
        return results

    def _pair_to_candidate(self, pair):
        try:
            chain = pair.get("chainId", "").lower()
            # Solana only
            if chain != "solana":
                return None
            pc   = pair.get("priceChange", {})
            vol  = pair.get("volume", {})
            liq  = pair.get("liquidity", {})
            bt   = pair.get("baseToken", {})
            txns = pair.get("txns", {})
            m5   = txns.get("m5", {})
            h1   = txns.get("h1", {})

            created_at = pair.get("pairCreatedAt", 0)
            age_hours = 0
            if created_at:
                age_ms    = datetime.utcnow().timestamp() * 1000 - created_at
                age_hours = age_ms / (1000 * 3600)

            price_change_1h = float(pc.get("h1", 0) or 0)
            liquidity_usd   = float(liq.get("usd", 0) or 0)

            # Flag hype tokens — big 1h move + decent liquidity
            is_hype = (
                price_change_1h >= self.config.HYPE_MIN_PRICE_CHANGE_1H
                and liquidity_usd >= self.config.HYPE_MIN_LIQUIDITY
            )

            return TokenCandidate(
                chain=chain,
                address=bt.get("address", ""),
                symbol=bt.get("symbol", "???"),
                name=bt.get("name", "Unknown"),
                price_usd=float(pair.get("priceUsd", 0) or 0),
                price_change_5m=float(pc.get("m5", 0) or 0),
                price_change_1h=price_change_1h,
                price_change_24h=float(pc.get("h24", 0) or 0),
                volume_5m=float(vol.get("m5", 0) or 0),
                volume_1h=float(vol.get("h1", 0) or 0),
                liquidity_usd=liquidity_usd,
                market_cap=float(pair.get("marketCap", 0) or 0),
                pair_address=pair.get("pairAddress", ""),
                dex_id=pair.get("dexId", ""),
                url=pair.get("url", ""),
                buys_5m=int(m5.get("buys", 0) or 0),
                sells_5m=int(m5.get("sells", 0) or 0),
                buys_1h=int(h1.get("buys", 0) or 0),
                sells_1h=int(h1.get("sells", 0) or 0),
                age_hours=age_hours,
                is_hype=is_hype,
            )
        except Exception:
            return None

    def _passes_filters(self, c) -> bool:
        if c.address in self.config.BLACKLISTED_TOKENS:
            return False
        if c.price_change_5m < self.config.MIN_PRICE_CHANGE_PCT:
            return False
        if c.price_change_5m > 50:
            logger.debug("Skipping %s — already pumped %.1f%%", c.symbol, c.price_change_5m)
            return False
        if c.volume_5m < self.config.MIN_VOLUME_USD_5M:
            return False
        if c.liquidity_usd < self.config.MIN_LIQUIDITY_USD:
            return False
        if c.price_usd <= 0:
            return False
        if c.buy_pressure() < 52:
            logger.debug("Skipping %s — low buy pressure %.1f%%", c.symbol, c.buy_pressure())
            return False
        return True

    async def _process_candidate(self, candidate):
        logger.info(
            "Processing: %s | +%.1f%% (5m) | +%.1f%% (1h) | BP: %.0f%% | Age: %.1fh | Hype: %s",
            candidate.symbol, candidate.price_change_5m, candidate.price_change_1h,
            candidate.buy_pressure(), candidate.age_hours, candidate.is_hype
        )

        # Risk check
        risk_result = await self._risk.check(candidate)

        # Hype token alert — send regardless of trade decision
        if candidate.is_hype:
            await self._telegram.send_hype_alert(candidate, risk_result)

        if not risk_result.is_safe:
            logger.info("SKIPPED %s: %s", candidate.symbol, risk_result.reason)
            await self._telegram.send_skipped(candidate, risk_result.reason)
            return

        # AI analysis
        ai_result = await self._ai.analyse(candidate, risk_result)
        if ai_result.confidence < self.config.AI_MIN_CONFIDENCE:
            logger.info("AI skipped %s (%.1f%% confidence)", candidate.symbol, ai_result.confidence)
            return

        logger.info(
            "APPROVED %s | Tier %s | %.1f%% confidence",
            candidate.symbol, risk_result.tier, ai_result.confidence
        )
        await self._telegram.send_opportunity(candidate, risk_result, ai_result)
        trade = await self._trader.buy(candidate, ai_result, risk_result)
        if trade and trade.success:
            await self._telegram.send_trade_executed(candidate, trade, risk_result.tier)
        else:
            await self._telegram.send_trade_failed(candidate, trade.error if trade else "unknown")
