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
    rugcheck_url: str

    def badge(self):
        if self.risk_score < 200: return "LOW RISK"
        if self.risk_score < 500: return "MEDIUM RISK"
        return "HIGH RISK"

    def summary(self):
        flags = []
        if self.is_mintable:   flags.append("mintable")
        if self.is_freezable:  flags.append("freezable")
        if self.has_blacklist: flags.append("blacklist")
        if self.fake_volume:   flags.append("fake volume")
        return (f"{self.badge()} (score: {self.risk_score}/1000)\n"
                f"LP locked: {self.lp_locked_pct:.0f}% | Top10: {self.top10_pct:.0f}%\n"
                f"Flags: {', '.join(flags) if flags else 'none'}")

class RiskChecker:
    def __init__(self, config):
        self.config = config
        self._cache = {}

    async def check(self, candidate) -> RiskResult:
        if candidate.address in self._cache:
            return self._cache[candidate.address]
        async with aiohttp.ClientSession() as session:
            rug_data = await self._rugcheck(session, candidate)
            extra    = await self._honeypot(session, candidate)
        result = self._score(candidate, rug_data, extra)
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

    async def _honeypot(self, session, candidate):
        if candidate.chain not in ("ethereum","base","bsc"):
            return None
        ids = {"ethereum":1,"base":8453,"bsc":56}
        try:
            async with session.get(
                "https://api.honeypot.is/v2/IsHoneypot",
                params={"address":candidate.address,"chainID":ids[candidate.chain]},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status == 200:
                    return await r.json()
        except Exception as e:
            logger.debug("Honeypot error: %s", e)
        return None

    def _score(self, candidate, rug_data, extra) -> RiskResult:
        risk_score=0; lp_locked_pct=0.0; top10_pct=0.0
        is_mintable=False; is_freezable=False; has_blacklist=False
        fake_volume=False; reason="OK"
        if rug_data:
            risk_score=int(rug_data.get("score",0) or 0)
            names=[r.get("name","").lower() for r in rug_data.get("risks",[])]
            is_mintable=any("mint" in n for n in names)
            is_freezable=any("freeze" in n for n in names)
            has_blacklist=any("blacklist" in n for n in names)
            markets=rug_data.get("markets",[{}])
            if markets: lp_locked_pct=float(markets[0].get("lp",{}).get("lpLockedPct",0) or 0)
            top_holders=rug_data.get("topHolders",[])
            if top_holders: top10_pct=sum(float(h.get("pct",0) or 0) for h in top_holders[:10])
            if candidate.volume_1h > candidate.liquidity_usd * 3:
                fake_volume=True; risk_score+=200
        if extra:
            if extra.get("isHoneypot"): risk_score+=500; reason="Honeypot detected"
            if float(extra.get("sellTax",0) or 0) > 20: risk_score+=300; reason=f"High sell tax"
        if lp_locked_pct < self.config.MIN_LP_LOCKED_PCT: risk_score+=150
        if top10_pct > 80: risk_score+=200
        if is_mintable: risk_score+=100
        risk_score=min(risk_score,1000)
        is_safe=risk_score<=self.config.MAX_RUGCHECK_RISK_SCORE
        if not is_safe and reason=="OK": reason=f"Risk score too high ({risk_score}/1000)"
        return RiskResult(is_safe=is_safe,risk_score=risk_score,reason=reason,
            lp_locked_pct=lp_locked_pct,top10_pct=top10_pct,is_mintable=is_mintable,
            is_freezable=is_freezable,has_blacklist=has_blacklist,fake_volume=fake_volume,
            rugcheck_url=f"https://rugcheck.xyz/tokens/{candidate.address}")
