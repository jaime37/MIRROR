"""
Extracts a probability signal from a MiroFish report (Markdown) for a given market question.
Uses the LLM to parse the report and estimate a YES probability.
"""

import json
from typing import Optional
from ...utils.llm_client import LLMClient
from ...utils.logger import get_logger

logger = get_logger("mirofish.polymarket.signal")

EXTRACTION_PROMPT = """You are a quantitative analyst. You are given:
1. A prediction market question from Polymarket.
2. A MiroFish simulation report (in Markdown) analyzing public opinion and event dynamics.

Your task: estimate the probability (0.0 to 1.0) that the market resolves YES, based ONLY on the report content.

Rules:
- If the report strongly supports YES → probability closer to 1.0
- If the report strongly supports NO → probability closer to 0.0
- If the report is ambiguous or not relevant → return null (cannot estimate)
- Be calibrated: use 0.55–0.75 for moderate confidence, 0.75–0.90 for high confidence
- Never return 0.0 or 1.0 exactly

Respond ONLY with valid JSON in this exact format:
{
  "probability": <float or null>,
  "confidence": "high" | "medium" | "low" | "irrelevant",
  "reasoning": "<one sentence explanation>",
  "relevant": <true or false>
}"""


class SignalExtractor:
    def __init__(self):
        self.llm = LLMClient()

    def extract(self, market_question: str, report_markdown: str) -> dict:
        """
        Analyzes a MiroFish report against a market question.

        Returns:
            {
                "probability": float | None,
                "confidence": str,
                "reasoning": str,
                "relevant": bool
            }
        """
        truncated_report = report_markdown[:8000]  # keep token usage bounded

        messages = [
            {"role": "system", "content": EXTRACTION_PROMPT},
            {
                "role": "user",
                "content": (
                    f"**Market question:** {market_question}\n\n"
                    f"**MiroFish report:**\n\n{truncated_report}"
                ),
            },
        ]

        try:
            result = self.llm.chat_json(messages, temperature=0.2)
            prob = result.get("probability")
            if prob is not None:
                result["probability"] = max(0.01, min(0.99, float(prob)))
            return result
        except Exception as e:
            logger.error(f"Signal extraction failed: {e}")
            return {
                "probability": None,
                "confidence": "irrelevant",
                "reasoning": f"Extraction error: {str(e)}",
                "relevant": False,
            }

    def has_edge(
        self,
        estimated_prob: float,
        market_price: float,
        min_edge: float = 0.05,
    ) -> dict:
        """
        Returns whether there is a tradeable edge and which side to bet.

        Args:
            estimated_prob: our estimated YES probability (0–1)
            market_price: current Polymarket YES price (0–1)
            min_edge: minimum required edge to consider a trade

        Returns:
            {"has_edge": bool, "side": "YES"|"NO"|None, "edge": float}
        """
        edge = estimated_prob - market_price
        if abs(edge) >= min_edge:
            return {
                "has_edge": True,
                "side": "YES" if edge > 0 else "NO",
                "edge": round(abs(edge), 4),
            }
        return {"has_edge": False, "side": None, "edge": round(abs(edge), 4)}
