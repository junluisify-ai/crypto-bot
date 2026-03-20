import logging
import aiohttp
import json
from dataclasses import dataclass
from typing import Optional, List

logger = logging.getLogger("ai")

@dataclass
class AIResult:
    confidence: float
    signal: str
    reasoning: str
    target_price: float
    stop_price: float
    risk_reward: float
    pattern: str

    def summary(self):
        return (f"AI Signal: {self.signal} ({self.confidence:.0f}% confidence)\n"
                f"Pattern: {self.pattern}\n"
                f"R/R: {self.risk_reward:.1f}x\n"
                f"Reasoning: {self.reasoning[:200]}")

class AIAnalyst:
    SYSTEM_PROMPT = """You are an expert crypto trading analyst.
Analyse the token and return ONLY a JSON object:
{"signal":"BUY"or"SKIP"or"WATCH","confidence":<0-100>,"pattern":"<name>","reasoning":"<2-3 sentences>","target_price_multiplier":<float>,"stop_price_multiplier":<float>,"risk_reward":<float>}
Be conservative. Return ONLY the JSON."""

    def __init__(self, config):
        self.config = config
        self.trade_history: List[dict] = []

    async def analyse(self, candidate, risk_result) -> AIResult:
        if not self.config.DEEPSEEK_API_KEY:
            return self._default_result(candidate)
        prompt = self._build_prompt(candidate, risk_result)
        raw    = await self._call_deepseek(prompt)
        return self._parse_response(raw, candidate)

    def _build_prompt(self, candidate, risk_result):
        return f"""Token: {candidate.symbol} on {candidate.chain}
Price: ${candidate.price_usd:.8f}
Change 5m:{candidate.price_change_5m:+.1f}% 1h:{candidate.price_change_1h:+.1f}% 24h:{candidate.price_change_24h:+.1f}%
Volume 5m:${candidate.volume_5m:,.0f} Liquidity:${candidate.liquidity_usd:,.0f}
Risk:{risk_result.risk_score}/1000 LP:{risk_result.lp_locked_pct:.0f}% Top10:{risk_result.top10_pct:.0f}%
Mintable:{risk_result.is_mintable} FakeVol:{risk_result.fake_volume}
Return ONLY the JSON."""

    async def _call_deepseek(self, prompt) -> Optional[str]:
        headers={"Authorization":f"Bearer {self.config.DEEPSEEK_API_KEY}","Content-Type":"application/json"}
        payload={"model":self.config.AI_MODEL,"max_tokens":500,"temperature":0.2,
                 "messages":[{"role":"system","content":self.SYSTEM_PROMPT},{"role":"user","content":prompt}]}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.config.DEEPSEEK_BASE_URL}/chat/completions",
                    json=payload,headers=headers,timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status==200:
                        data=await r.json()
                        return data["choices"][0]["message"]["content"]
                    logger.warning("DeepSeek error: %s",r.status)
        except Exception as e:
            logger.error("DeepSeek failed: %s",e)
        return None

    def _parse_response(self, raw, candidate) -> AIResult:
        if not raw:
            return self._default_result(candidate)
        try:
            clean=raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            data=json.loads(clean)
            tp=float(data.get("target_price_multiplier",1.4))
            sl=float(data.get("stop_price_multiplier",0.8))
            return AIResult(confidence=float(data.get("confidence",0)),
                signal=data.get("signal","SKIP"),reasoning=data.get("reasoning",""),
                target_price=candidate.price_usd*tp,stop_price=candidate.price_usd*sl,
                risk_reward=float(data.get("risk_reward",(tp-1)/(1-sl))),
                pattern=data.get("pattern","unknown"))
        except Exception as e:
            logger.error("Parse error: %s",e)
            return self._default_result(candidate)

    def _default_result(self, candidate) -> AIResult:
        confidence=min(50+candidate.price_change_5m,80)
        return AIResult(confidence=confidence,
            signal="BUY" if confidence>=self.config.AI_MIN_CONFIDENCE else "SKIP",
            reasoning="AI unavailable - momentum signal used",
            target_price=candidate.price_usd*1.40,stop_price=candidate.price_usd*0.80,
            risk_reward=2.0,pattern="momentum")
