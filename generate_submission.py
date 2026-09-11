"""
Generate submission.jsonl (challenge-brief.md §7.2) by running vera-bot's
compose() over all 30 canonical test pairs in expanded/test_pairs.json.

Run this from the repo root, with vera-bot/.env populated (GEMINI_API_KEY,
GROQ_API_KEY) and network access to Gemini + Groq:

    python generate_submission.py

Writes vera-bot/submission.jsonl (one JSON line per test pair, in test_id
order), alongside bot.py and README.md as the brief expects.
"""
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent
BOT_PATH = ROOT / "vera-bot" / "bot.py"
OUT_PATH = ROOT / "vera-bot" / "submission.jsonl"


def _load_bot():
    """Import vera-bot/bot.py as a standalone module (loads .env via
    python-dotenv the same way `uvicorn bot:app` does, since bot.py calls
    load_dotenv() at import time)."""
    sys.path.insert(0, str(BOT_PATH.parent))
    spec = importlib.util.spec_from_file_location("bot", BOT_PATH)
    bot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bot)
    return bot


def _load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _resolve_pair(bot, pair: dict) -> tuple[dict, dict, dict, dict | None]:
    """Load (category, merchant, trigger, customer) dicts for one test pair,
    exactly the four inputs compose() expects."""
    merchant = _load_json(ROOT / "expanded" / "merchants" / f"{pair['merchant_id']}.json")
    trigger = _load_json(ROOT / "expanded" / "triggers" / f"{pair['trigger_id']}.json")
    category = _load_json(ROOT / "expanded" / "categories" / f"{merchant['category_slug']}.json")
    customer = None
    if pair.get("customer_id"):
        customer = _load_json(ROOT / "expanded" / "customers" / f"{pair['customer_id']}.json")
    return category, merchant, trigger, customer


async def _compose_one(bot, pair: dict) -> dict:
    category, merchant, trigger, customer = _resolve_pair(bot, pair)
    try:
        result = await bot.compose(category, merchant, trigger, customer)
    except Exception as e:  # noqa: BLE001
        print(f"  [ERROR] {pair['test_id']}: {e}", file=sys.stderr)
        raise
    return {
        "test_id": pair["test_id"],
        "body": result.get("body", ""),
        "cta": result.get("cta", "open_ended"),
        "send_as": result.get("send_as", "vera"),
        "suppression_key": result.get("suppression_key", ""),
        "rationale": result.get("rationale", ""),
    }


async def main():
    bot = _load_bot()
    if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GROQ_API_KEY"):
        print("ERROR: neither GEMINI_API_KEY nor GROQ_API_KEY is set. "
              "Make sure vera-bot/.env is populated.", file=sys.stderr)
        sys.exit(1)

    pairs = _load_json(ROOT / "expanded" / "test_pairs.json")["pairs"]
    print(f"Loaded {len(pairs)} test pairs. Composing (this calls the LLM for each)...")

    # Small concurrency cap (not all 30 at once) to stay polite to free-tier
    # rate limits — call_llm() itself is already bounded by LLM_CONCURRENCY.
    sem = asyncio.Semaphore(5)

    async def bounded(pair):
        async with sem:
            return await _compose_one(bot, pair)

    results = await asyncio.gather(*(bounded(p) for p in pairs))

    # test_pairs.json is already in T01..T30 order; keep that order in output.
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"Wrote {len(results)} lines to {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
