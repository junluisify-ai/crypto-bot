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
    BIRDEYE_TRENDING  = "https://public-api.birdeye.so/defi/token_trending"
    BIRDEYE_NEW       = "https://public-api.birdeye.so/defi/v2/tokens/new_listing"

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

    @property
    def _birdeye_headers(self):
        return {
            "X-API-KEY": self.config.BIRDEYE_API_KEY,
            "x-chain": "solana"
        }

    async def run(self):
        self.running = True
        logger.info("Scanner started — Solana | DexScreener + Birdeye")
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
        logger.info("Scanning Solana — DexScreener + Birdeye trending + Birdeye new...")

        results = await asyncio.gather(
            self._get_dexscreener_trending(),
            self._get_birdeye_trending(),
            self._get_birdeye_new(),
            return_exceptions=True
        )

        candidates = []
        for r in results:
            if isinstance(r, list):
                candidates.extend(r)

        seen_now = {}
        for c in candidates:
            if c.address and c.address not in seen_now:
                seen_now[c.address] = c

        fresh = [c for addr, c in seen_now.items() if addr not in self._seen]
        logger.info("Found %d new Solana candidates", len(fresh))

        top = sorted(fresh, key=lambda x: (x.is_hype, x.price_change_5m), reverse=True)

        for candidate in top[:self.config.MAX_TOKENS_PER_SCAN]:
            self._seen.add(candidate.address)
            asyncio.create_task(self._process_candidate(candidate))

        if len(self._seen) > 10000:
            self._seen = set(list(self._seen)[-5000:])

    async def _get_dexscreener_trending(self):
        try:
            async with self._session.get(
                self.TRENDING_ENDPOINT,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status != 200:
                    return []
                data = await r.json()
                addresses = [t.get("tokenAddress", "") for t in (data or [])[:30]]
                return await self._fetch_dex_token_details(addresses)
        except Exception as e:
            logger.debug("DexScreener trending error: %s", e)
            return []

    async def _get_birdeye_trending(self):
        """Birdeye trending tokens — much better quality than DexScreener."""
        if not self.config.BIRDEYE_API_KEY:
            return []
        try:
            async with self._session.get(
                self.BIRDEYE_TRENDING,
                headers=self._birdeye_headers,
                params={"sort_by": "rank", "sort_type": "asc", "offset": 0, "limit": 20},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status != 200:
                    logger.warning("Birdeye trending error: %s", r.status)
                    return []
                data = await r.json()
                items = data.get("data", {}).get("items", [])
                logger.info("Birdeye trending returned %d tokens", len(items))
                candidates = []
                for item in items:
                    c = self._birdeye_to_candidate(item)
                    if c and self._passes_filters(c):
                        candidates.append(c)
                return candidates
        except Exception as e:
            logger.debug("Birdeye trending error: %s", e)
            return []

    async def _get_birdeye_new(self):
        """Birdeye new token listings — catch early movers."""
        if not self.config.BIRDEYE_API_KEY:
            return []
        try:
            async with self._session.get(
                self.BIRDEYE_NEW,
                headers=self._birdeye_headers,
                params={"limit": 20, "meme_platform_enabled": True},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status != 200:
                    logger.warning("Birdeye new listing error: %s", r.status)
                    return []
                data = await r.json()
                items = data.get("data", {}).get("items", [])
                logger.info("Birdeye new listings returned %d tokens", len(items))
                candidates = []
                for item in items:
                    c = self._birdeye_to_candidate(item)
                    if c and self._passes_filters(c):
                        candidates.append(c)
                return candidates
        except Exception as e:
            logger.debug("Birdeye new listing error: %s", e)
            return []

    def _birdeye_to_candidate(self, item):
        try:
            address = item.get("address", "")
            if not address:
                return None

            price_change_1h = float(item.get("priceChange1hPercent", 0) or 0)
            liquidity_usd   = float(item.get("liquidity", 0) or 0)
            volume_5m       = float(item.get("v5mUSD", 0) or 0)
            volume_1h       = float(item.get("v1hUSD", 0) or 0)
            price_change_5m = float(item.get("priceChange5mPercent", 0) or 0)

            # Calculate age
            listed_at = item.get("listingTime", 0) or 0
            age_hours = 0
            if listed_at:
                age_hours = (datetime.utcnow().timestamp() - listed_at) / 3600

            is_hype = (
                price_change_1h >= self.config.HYPE_MIN_PRICE_CHANGE_1H
                and liquidity_usd >= self.config.HYPE_MIN_LIQUIDITY
            )

            symbol = item.get("symbol", "???")
            url    = f"https://dexscreener.com/solana/{address}"

            return TokenCandidate(
                chain="solana",
                address=address,
                symbol=symbol,
                name=item.get("name", "Unknown"),
                price_usd=float(item.get("price", 0) or 0),
                price_change_5m=price_change_5m,
                price_change_1h=price_change_1h,
                price_change_24h=float(item.get("priceChange24hPercent", 0) or 0),
                volume_5m=volume_5m,
                volume_1h=volume_1h,
                liquidity_usd=liquidity_usd,
                market_cap=float(item.get("mc", 0) or 0),
                pair_address=address,
                dex_id="birdeye",
                url=url,
                buys_5m=int(item.get("buy5m", 0) or 0),
                sells_5m=int(item.get("sell5m", 0) or 0),
                buys_1h=int(item.get("buy1h", 0) or 0),
                sells_1h=int(item.get("sell1h", 0) or 0),
                age_hours=age_hours,
                is_hype=is_hype,
            )
        except Exception as e:
            logger.debug("Birdeye parse error: %s", e)
            return None

    async def _fetch_dex_token_details(self, addresses):
        if not addresses:
            return []
        results = []
        for i in range(0, len(addresses), 30):
            batch = [a for a in addresses[i:i+30] if a]
            if not batch:
                continue
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
                        if pair.get("chainId", "").lower() != "solana":
                            continue
                        c = self._dex_pair_to_candidate(pair)
                        if c and self._passes_filters(c):
                            results.append(c)
            except Exception as e:
                logger.debug("Token details error: %s", e)
        return results

    def _dex_pair_to_candidate(self, pair):
        try:
            chain = pair.get("chainId", "").lower()
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
        if not c or not c.address:
            return False
        if c.address in self.config.BLACKLISTED_TOKENS:
            return False
        if c.price_change_5m < self.config.MIN_PRICE_CHANGE_PCT:
            return False
        if c.price_change_5m > 60:
            return False
        if c.volume_5m < self.config.MIN_VOLUME_USD_5M:
            return False
        if c.liquidity_usd < self.config.MIN_LIQUIDITY_USD:
            return False
        if c.price_usd <= 0:
            return False
        if c.buy_pressure() < 50:
            return False
        return True

    async def _process_candidate(self, candidate):
        logger.info(
            "Processing: %s | +%.1f%% (5m) | +%.1f%% (1h) | BP: %.0f%% | Age: %.1fh | Hype: %s",
            candidate.symbol, candidate.price_change_5m, candidate.price_change_1h,
            candidate.buy_pressure(), candidate.age_hours, candidate.is_hype
        )

        risk_result = await self._risk.check(candidate)

        if candidate.is_hype:
            await self._telegram.send_hype_alert(candidate, risk_result)

        if not risk_result.is_safe:
            logger.info("SKIPPED %s: %s", candidate.symbol, risk_result.reason)
            await self._telegram.send_skipped(candidate, risk_result.reason)
            return

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
