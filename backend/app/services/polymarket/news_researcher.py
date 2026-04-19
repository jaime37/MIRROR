"""
Researches a Polymarket question by searching the web for recent news and context.
Uses DuckDuckGo (free, no API key) as primary source.
Optionally uses Tavily if TAVILY_API_KEY is set in .env.
Falls back to LLM's own knowledge if search yields nothing useful.
Handles 429 rate-limit errors with exponential backoff.
"""

import json
import os
import re
import time
from openai import RateLimitError
from ...utils.llm_client import LLMClient
from ...utils.logger import get_logger

logger = get_logger("mirofish.polymarket.researcher")

# ── Prompts ────────────────────────────────────────────────────────────────────

ANALYSIS_PROMPT = """You are a prediction market analyst. Today is April 2025.

You are given a Polymarket YES/NO question and some web search results.

Your task:
- Estimate the probability (0.01–0.99) that the question resolves YES
- Use the news snippets as primary evidence
- If snippets are sparse, STILL give your best estimate using general knowledge, base rates, and common sense
- Only return relevant=false if the question is about a completely unknown private person or a hyper-local event you genuinely know nothing about

Confidence rules:
- "high": strong recent evidence directly answering the question
- "medium": some relevant evidence or strong prior knowledge
- "low": little evidence but can estimate from base rates or general knowledge
- "irrelevant": truly cannot form any estimate (very rare)

Respond ONLY with valid JSON — no markdown, no extra text:
{
  "probability": <float 0.01-0.99>,
  "confidence": "high" | "medium" | "low" | "irrelevant",
  "reasoning": "<2-3 sentences citing specific evidence or reasoning>",
  "relevant": <true or false>,
  "key_facts": ["<fact 1>", "<fact 2>", "<fact 3>"]
}"""

FALLBACK_PROMPT = """You are a prediction market analyst. Today is April 2025.

You are given a Polymarket YES/NO question. No web search results are available.

Use your training knowledge to estimate the probability this resolves YES.
Consider: base rates, historical patterns, current known trends, common sense.
Only return relevant=false if you have absolutely no way to form any estimate.

Respond ONLY with valid JSON — no markdown, no extra text:
{
  "probability": <float 0.01-0.99>,
  "confidence": "high" | "medium" | "low" | "irrelevant",
  "reasoning": "<2-3 sentences explaining your reasoning>",
  "relevant": <true or false>,
  "key_facts": ["<fact 1>", "<fact 2>", "<fact 3>"]
}"""


class NewsResearcher:
    def __init__(self):
        self.llm = LLMClient()
        self._tavily_key = os.environ.get("TAVILY_API_KEY")

    # ── Public API ─────────────────────────────────────────────────────────────

    def research(self, question: str, max_results: int = 6) -> dict:
        """
        Searches the web for news about a market question and returns an
        LLM-generated probability estimate. Falls back to LLM knowledge if
        search results are empty or unhelpful.
        """
        query = self._build_query(question)
        snippets = self._search(query, max_results)
        sources = [s.get("url", "") for s in snippets]

        if snippets:
            news_text = self._format_snippets(snippets)
            result = self._call_with_retry(self._analyze_with_news, question, news_text)
        else:
            logger.warning(f"No search results — using LLM knowledge: {question[:60]}")
            result = self._call_with_retry(self._analyze_no_news, question)

        # If LLM says irrelevant despite having news, try pure-knowledge fallback
        if not result.get("relevant") and snippets:
            logger.info(f"Retrying with fallback prompt: {question[:60]}")
            fallback = self._call_with_retry(self._analyze_no_news, question)
            if fallback.get("relevant"):
                result = fallback

        result["sources"] = sources
        result["search_query"] = query
        return result

    # ── Retry wrapper ──────────────────────────────────────────────────────────

    def _call_with_retry(self, fn, *args, max_retries: int = 4) -> dict:
        """
        Calls fn(*args) and retries on 429 RateLimitError with exponential backoff.
        Waits: 20s → 40s → 80s → 160s between attempts.
        """
        delay = 20
        for attempt in range(max_retries):
            try:
                return fn(*args)
            except RateLimitError as e:
                if attempt == max_retries - 1:
                    logger.error(f"Rate limit persists after {max_retries} retries: {e}")
                    return self._error_result(str(e))
                logger.warning(f"Rate limit hit — waiting {delay}s before retry {attempt + 2}/{max_retries}…")
                time.sleep(delay)
                delay = min(delay * 2, 120)
            except Exception as e:
                logger.error(f"LLM call failed: {e}")
                return self._error_result(str(e))
        return self._error_result("Max retries exceeded")

    # ── Search backends ────────────────────────────────────────────────────────

    def _search(self, query: str, max_results: int) -> list[dict]:
        if self._tavily_key:
            try:
                return self._search_tavily(query, max_results)
            except Exception as e:
                logger.warning(f"Tavily failed, falling back to DuckDuckGo: {e}")
        return self._search_ddg(query, max_results)

    def _search_ddg(self, query: str, max_results: int) -> list[dict]:
        from ddgs import DDGS
        results = []
        try:
            with DDGS() as ddgs:
                for r in ddgs.news(query, max_results=max_results, timelimit="m"):
                    results.append({
                        "title": r.get("title", ""),
                        "body": r.get("body", ""),
                        "url": r.get("url", ""),
                        "date": r.get("date", ""),
                    })
        except Exception:
            pass

        if not results:
            try:
                with DDGS() as ddgs:
                    for r in ddgs.text(query, max_results=max_results):
                        results.append({
                            "title": r.get("title", ""),
                            "body": r.get("body", ""),
                            "url": r.get("href", ""),
                            "date": "",
                        })
            except Exception as e:
                logger.error(f"DuckDuckGo search failed: {e}")

        time.sleep(0.5)
        return results

    def _search_tavily(self, query: str, max_results: int) -> list[dict]:
        import requests
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": self._tavily_key,
                "query": query,
                "search_depth": "basic",
                "max_results": max_results,
                "include_answer": False,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return [
            {
                "title": r.get("title", ""),
                "body": r.get("content", ""),
                "url": r.get("url", ""),
                "date": r.get("published_date", ""),
            }
            for r in resp.json().get("results", [])
        ]

    # ── LLM analysis ──────────────────────────────────────────────────────────

    def _analyze_with_news(self, question: str, news_text: str) -> dict:
        messages = [
            {"role": "system", "content": ANALYSIS_PROMPT},
            {
                "role": "user",
                "content": f"**Market question:** {question}\n\n**Search results:**\n\n{news_text}",
            },
        ]
        return self._llm_json(messages)

    def _analyze_no_news(self, question: str) -> dict:
        messages = [
            {"role": "system", "content": FALLBACK_PROMPT},
            {"role": "user", "content": f"**Market question:** {question}"},
        ]
        return self._llm_json(messages)

    def _llm_json(self, messages: list) -> dict:
        # Use plain chat() instead of chat_json() — many free OpenRouter models
        # don't support response_format: json_object and return a 400 error.
        # We extract JSON from the raw text response ourselves.
        raw = self.llm.chat(messages, temperature=0.2, max_tokens=500)
        result = self._parse_json(raw)
        prob = result.get("probability")
        if prob is not None:
            result["probability"] = max(0.01, min(0.99, float(prob)))
        result.setdefault("relevant", result.get("probability") is not None)
        result.setdefault("confidence", "low")
        result.setdefault("reasoning", "")
        result.setdefault("key_facts", [])
        return result

    def _parse_json(self, text: str) -> dict:
        """Extract the first JSON object from a free-form LLM response."""
        # Strip markdown code fences
        text = re.sub(r"```(?:json)?\s*", "", text).strip()
        text = text.replace("```", "").strip()
        # Try direct parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Find first {...} block
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        logger.warning(f"Could not parse JSON from response: {text[:200]}")
        return {}

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_query(self, question: str) -> str:
        q = question[:100].strip()
        return f"{q} 2025"

    def _format_snippets(self, snippets: list[dict]) -> str:
        lines = []
        for i, s in enumerate(snippets, 1):
            date = f" [{s['date']}]" if s.get("date") else ""
            lines.append(f"[{i}]{date} {s['title']}\n{s['body'][:400]}\n")
        return "\n".join(lines)

    def _error_result(self, msg: str) -> dict:
        return {
            "probability": None,
            "confidence": "irrelevant",
            "reasoning": f"Error: {msg}",
            "relevant": False,
            "key_facts": [],
        }
