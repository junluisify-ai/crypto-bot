import asyncio
import logging
import aiohttp
from dataclasses import dataclass

logger = logging.getLogger("risk")

@dataclass
class RiskResult:
    is_safe: bool
    risk_score: int
    reason: str
    lp_locked_pct: float
    top10_pct: float
    is_mintable: bool
    is_freezable: bool
    has_blacklist: bool
    fake_volume: bool
    tier: str
    rugcheck_url: str

    def badge(self):
        if self.risk_score < 200: return "LOW RISK"
        if self.risk_score < 400: return "MEDIUM RISK"
        return "HIGH RISK"

    def summary(self):
        flags = []
        if self.is_mintable:   flags.append("mintable")
        if self.is_freezable:  flags.append("freezable")
        if self.has_blacklist: flags.append("blacklist")
        if self.fake_volume:   flags.append("fake volume")
        return (
            f"{self.badge()} (score: {self.risk_score}/1000)\n"
            f"LP locked: {self.lp_locked_pct:.0f}% | Top10: {self.top10_pct:.0f}%\n"
            f"Flags: {', '.join(flags) if flags else 'none'}\n"
            f"Tier: {self.tier}"
        )

class RiskChecker:
    def __init__(self, config):
        self.config = config
        self._cache = {}

    async def check(self, candidate) -> RiskResult:
        if candidate.address in self._cache:
            return self._cache[candidate.address]
        async with aiohttp.ClientSession() as session:
            rug_data = await self._rugcheck(session, candidate)
        result = self._score(candidate, rug_data)
        self._cache[candidate.address] = result
        return result

    async def _rugcheck(self, session, candidate):
        url = f"https://api.rugcheck.xyz/v1/tokens/{candidate.address}/report"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    return await r.json()
        except Exception as e:
            logger.debug("RugCheck error: %s", e)
        return None

    def _score(self, candidate, rug_data) -> RiskResult:
        risk_score = 0
        lp_locked_pct = 0.0
        top10_pct = 0.0
        is_mintable = False
        is_freezable = False
        has_blacklist = False
        fake_volume = False
        reason = "OK"

        if rug_data:
            risk_score = int(rug_data.get("score", 0) or 0)
            names = [r.get("name", "").lower() for r in rug_data.get("risks", [])]
            is_mintable   = any("mint"      in n for n in names)
            is_freezable  = any("freeze"    in n for n in names)
            has_blacklist = any("blacklist" in n for n in names)
            markets = rug_data.get("markets", [{}])
            if markets:
                lp_locked_pct = float(markets[0].get("lp", {}).get("lpLockedPct", 0) or 0)
            top_holders = rug_data.get("topHolders", [])
            if top_holders:
                top10_pct = sum(float(h.get("pct", 0) or 0) for h in top_holders[:10])
            if candidate.volume_1h > candidate.liquidity_usd * 3:
                fake_volume = True

        # ── HARD KILL SWITCHES ─────────────────────────────────
        # These block the trade completely regardless of anything else

        if fake_volume:
            return RiskResult(
                is_safe=False, risk_score=1000, reason="KILL: Fake volume detected",
                lp_locked_pct=lp_locked_pct, top10_pct=top10_pct,
                is_mintable=is_mintable, is_freezable=is_freezable,
                has_blacklist=has_blacklist, fake_volume=True,
                tier="REJECTED", rugcheck_url=f"https://rugcheck.xyz/tokens/{candidate.address}"
            )

        if has_blacklist:
            return RiskResult(
                is_safe=False, risk_score=1000, reason="KILL: Blacklist function detected",
                lp_locked_pct=lp_locked_pct, top10_pct=top10_pct,
                is_mintable=is_mintable, is_freezable=is_freezable,
                has_blacklist=True, fake_volume=False,
                tier="REJECTED", rugcheck_url=f"https://rugcheck.xyz/tokens/{candidate.address}"
            )

        if top10_pct > 85:
            return RiskResult(
                is_safe=False, risk_score=1000, reason=f"KILL: Top 10 wallets hold {top10_pct:.0f}%",
                lp_locked_pct=lp_locked_pct, top10_pct=top10_pct,
                is_mintable=is_mintable, is_freezable=is_freezable,
                has_blacklist=has_blacklist, fake_volume=False,
                tier="REJECTED", rugcheck_url=f"https://rugcheck.xyz/tokens/{candidate.address}"
            )

        # ── SCORE ADJUSTMENTS ──────────────────────────────────
        if is_mintable:  risk_score += 100
        if is_freezable: risk_score += 80
        risk_score = min(risk_score, 1000)

        # ── TIER ASSIGNMENT ────────────────────────────────────
        # Tier A: full $3 trade
        if (
            risk_score <= self.config.MAX_RUGCHECK_RISK_SCORE
            and lp_locked_pct >= self.config.TIER_A_MIN_LP_LOCKED
            and candidate.liquidity_usd >= self.config.TIER_A_MIN_LIQUIDITY
            and not is_mintable
            and not is_freezable
        ):
            tier = "A"
            is_safe = True

        # Tier B: half size $1.50 trade
        elif (
            risk_score <= self.config.MAX_RUGCHECK_RISK_SCORE
            and candidate.liquidity_usd >= self.config.TIER_B_MIN_LIQUIDITY
            and not is_mintable
        ):
            tier = "B"
            is_safe = True

        # Tier C: alert only, no trade
        elif risk_score <= self.config.MAX_RUGCHECK_RISK_SCORE:
            tier = "C"
            is_safe = False
            reason = "Tier C — alert only, below trading threshold"

        else:
            tier = "REJECTED"
            is_safe = False
            reason = f"Risk score too high ({risk_score}/1000)"

        return RiskResult(
            is_safe=is_safe, risk_score=risk_score, reason=reason,
            lp_locked_pct=lp_locked_pct, top10_pct=top10_pct,
            is_mintable=is_mintable, is_freezable=is_freezable,
            has_blacklist=has_blacklist, fake_volume=fake_volume,
            tier=tier, rugcheck_url=f"https://rugcheck.xyz/tokens/{candidate.address}"
        )
