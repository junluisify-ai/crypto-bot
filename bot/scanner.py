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
    discovered_at: datetime = field(default_factory=datetime.utcnow)

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
        self._risk=risk; self._ai=ai; self._trader=trader; self._telegram=telegram

    async def run(self):
        self.running = True
        logger.info("Scanner started - watching %s", self.config.TARGET_CHAINS)
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
        logger.info("Scanning for opportunities...")
        candidates = await self._get_trending()
        fresh = [c for c in candidates if c.address not in self._seen]
        logger.info("Found %d new candidates", len(fresh))
        top = sorted(fresh, key=lambda x: x.price_change_5m, reverse=True)
        for candidate in top[:self.config.MAX_TOKENS_PER_SCAN]:
            self._seen.add(candidate.address)
            asyncio.create_task(self._process_candidate(candidate))
        if len(self._seen) > 10000:
            self._seen = set(list(self._seen)[-5000:])

    async def _get_trending(self):
        try:
            async with self._session.get(self.TRENDING_ENDPOINT, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return []
                data = await r.json()
                addresses = [t.get("tokenAddress","") for t in (data or [])[:30]]
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
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
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
            chain = pair.get("chainId","").lower()
            if chain not in self.config.TARGET_CHAINS:
                return None
            pc=pair.get("priceChange",{}); vol=pair.get("volume",{})
            liq=pair.get("liquidity",{}); bt=pair.get("baseToken",{})
            return TokenCandidate(
                chain=chain, address=bt.get("address",""),
                symbol=bt.get("symbol","???"), name=bt.get("name","Unknown"),
                price_usd=float(pair.get("priceUsd",0) or 0),
                price_change_5m=float(pc.get("m5",0) or 0),
                price_change_1h=float(pc.get("h1",0) or 0),
                price_change_24h=float(pc.get("h24",0) or 0),
                volume_5m=float(vol.get("m5",0) or 0),
                volume_1h=float(vol.get("h1",0) or 0),
                liquidity_usd=float(liq.get("usd",0) or 0),
                market_cap=float(pair.get("marketCap",0) or 0),
                pair_address=pair.get("pairAddress",""),
                dex_id=pair.get("dexId",""), url=pair.get("url",""),
            )
        except Exception:
            return None

    def _passes_filters(self, c):
        if c.address in self.config.BLACKLISTED_TOKENS: return False
        if c.price_change_5m < self.config.MIN_PRICE_CHANGE_PCT: return False
        if c.volume_5m < self.config.MIN_VOLUME_USD_5M: return False
        if c.liquidity_usd < self.config.MIN_LIQUIDITY_USD: return False
        if c.price_usd <= 0: return False
        return True

    async def _process_candidate(self, candidate):
        logger.info("Processing: %s on %s", candidate.symbol, candidate.chain)
        risk_result = await self._risk.check(candidate)
        if not risk_result.is_safe:
            logger.info("SKIPPED %s: %s", candidate.symbol, risk_result.reason)
            await self._telegram.send_skipped(candidate, risk_result.reason)
            return
        ai_result = await self._ai.analyse(candidate, risk_result)
        if ai_result.confidence < self.config.AI_MIN_CONFIDENCE:
            logger.info("AI skipped %s (%.1f%%)", candidate.symbol, ai_result.confidence)
            return
        logger.info("APPROVED %s - %.1f%% confidence", candidate.symbol, ai_result.confidence)
        await self._telegram.send_opportunity(candidate, risk_result, ai_result)
        trade = await self._trader.buy(candidate, ai_result)
        if trade and trade.success:
            await self._telegram.send_trade_executed(candidate, trade)
        else:
            await self._telegram.send_trade_failed(candidate, trade.error if trade else "unknown")
