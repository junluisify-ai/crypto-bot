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
        return (
            f"AI Signal: {self.signal} ({self.confidence:.0f}% confidence)\n"
            f"Pattern: {self.pattern}\n"
            f"R/R: {self.risk_reward:.1f}x\n"
            f"Reasoning: {self.reasoning[:200]}"
        )

class AIAnalyst:
    GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"

    SYSTEM_PROMPT = """You are an expert crypto trading analyst specializing in meme coins and DEX tokens.
Analyse the token data and return ONLY a JSON object with no extra text:
{
  "signal": "BUY" or "SKIP" or "WATCH",
  "confidence": <0-100>,
  "pattern": "<pattern name>",
  "reasoning": "<2-3 sentences>",
  "target_price_multiplier": <float>,
  "stop_price_multiplier": <float>,
  "risk_reward": <float>
}
Rules:
- BUY only when confidence >= 65
- SKIP if pump already too large or signs of manipulation
- Be conservative. Protect capital above all else.
- Return ONLY the JSON object, nothing else."""

    def __init__(self, config):
        self.config = config
        self.trade_history: List[dict] = []

    async def analyse(self, candidate, risk_result) -> AIResult:
        if not self.config.GEMINI_API_KEY:
            return self._default_result(candidate)
        prompt = self._build_prompt(candidate, risk_result)
        raw    = await self._call_gemini(prompt)
        return self._parse_response(raw, candidate)

    def record_trade_outcome(self, symbol, entry, exit_price, pnl_pct):
        self.trade_history.append({
            "symbol": symbol, "entry": entry,
            "exit": exit_price, "pnl": f"{pnl_pct:+.1f}%"
        })
        if len(self.trade_history) > 50:
            self.trade_history = self.trade_history[-50:]

    def _build_prompt(self, candidate, risk_result):
        recent = json.dumps(self.trade_history[-10:]) if self.trade_history else "None yet."
        return f"""{self.SYSTEM_PROMPT}

Token: {candidate.symbol} on {candidate.chain}
Price: ${candidate.price_usd:.8f}
Change 5m: {candidate.price_change_5m:+.1f}%
Change 1h: {candidate.price_change_1h:+.1f}%
Change 24h: {candidate.price_change_24h:+.1f}%
Volume 5m: ${candidate.volume_5m:,.0f}
Liquidity: ${candidate.liquidity_usd:,.0f}
Market cap: ${candidate.market_cap:,.0f}
Buy pressure: {candidate.buy_pressure():.0f}%
Token age: {candidate.age_hours:.1f} hours
DEX: {candidate.dex_id}
Risk score: {risk_result.risk_score}/1000
LP locked: {risk_result.lp_locked_pct:.0f}%
Top10 wallets: {risk_result.top10_pct:.0f}%
Mintable: {risk_result.is_mintable}
Fake volume: {risk_result.fake_volume}
Blacklist: {risk_result.has_blacklist}
Recent trade history: {recent}

Return ONLY the JSON object."""

    async def _call_gemini(self, prompt: str) -> Optional[str]:
        url = f"{self.GEMINI_URL}?key={self.config.GEMINI_API_KEY}"
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 500,
            }
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        return data["candidates"][0]["content"]["parts"][0]["text"]
                    else:
                        error = await r.text()
                        logger.warning("Gemini API error: %s | %s", r.status, error[:200])
        except Exception as e:
            logger.error("Gemini call failed: %s", e)
        return None

    def _parse_response(self, raw: Optional[str], candidate) -> AIResult:
        if not raw:
            return self._default_result(candidate)
        try:
            clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            data  = json.loads(clean)
            tp    = float(data.get("target_price_multiplier", 1.4))
            sl    = float(data.get("stop_price_multiplier",   0.8))
            return AIResult(
                confidence   = float(data.get("confidence", 0)),
                signal       = data.get("signal", "SKIP"),
                reasoning    = data.get("reasoning", ""),
                target_price = candidate.price_usd * tp,
                stop_price   = candidate.price_usd * sl,
                risk_reward  = float(data.get("risk_reward", (tp-1)/(1-sl))),
                pattern      = data.get("pattern", "unknown"),
            )
        except Exception as e:
            logger.error("Failed to parse Gemini response: %s", e)
            return self._default_result(candidate)

    def _default_result(self, candidate) -> AIResult:
        confidence = min(50 + candidate.price_change_5m, 80)
        return AIResult(
            confidence   = confidence,
            signal       = "BUY" if confidence >= self.config.AI_MIN_CONFIDENCE else "SKIP",
            reasoning    = "AI unavailable - momentum signal used",
            target_price = candidate.price_usd * 1.40,
            stop_price   = candidate.price_usd * 0.80,
            risk_reward  = 2.0,
            pattern      = "momentum",
        )
