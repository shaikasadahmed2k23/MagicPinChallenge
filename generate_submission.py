"""
Generate submission.jsonl (challenge-brief.md §7.2) by running vera-bot's
compose() over all 30 canonical test pairs in expanded/test_pairs.json.

Run this from the repo root, with vera-bot/.env populated (GEMINI_API_KEY,
GROQ_API_KEY) and network access to Gemini + Groq:

    python generate_submission.py

Writes vera-bot/submission.jsonl (one JSON line per test pair, in test_id
order), alongside bot.py and README.md as the brief expects.

NOTE on pacing: compose() races Gemini + Groq concurrently (by design, for
the live bot's per-call latency budget), so every pair burns a call against
BOTH providers' quotas. Free-tier Groq's TPM (tokens/minute) budget is easy
to blow through if 30 pairs fire back-to-back or concurrently — this script
runs pairs strictly sequentially with a pacing delay between them, and
retries (with backoff) any pair that falls back to the generic template
instead of silently banking that lower-quality line into the submission.
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

# Groq free-tier TPM budget has been ~8000 tokens/min in testing, and a
# single compose() prompt has been costing ~2700-3800 tokens — pace calls
# conservatively so we don't fire faster than that budget refills.
PACING_SECONDS = 22.0
FALLBACK_RATIONALE = "Fallback path — LLM providers unavailable, used minimal grounded template."
MAX_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 35.0


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
    """Compose one pair, retrying (with backoff) if both providers were
    rate-limited/unavailable and compose() fell back to the generic
    template — a batch script isn't latency-constrained the way the live
    bot's /v1/reply and /v1/tick are, so it's worth waiting out a rate
    limit rather than banking a low-quality line into the submission."""
    category, merchant, trigger, customer = _resolve_pair(bot, pair)
    result = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = await bot.compose(category, merchant, trigger, customer)
        if result.get("rationale") != FALLBACK_RATIONALE:
            break
        if attempt < MAX_ATTEMPTS:
            print(f"  [{pair['test_id']}] fell back to template (attempt {attempt}/{MAX_ATTEMPTS}), "
                  f"waiting {RETRY_BACKOFF_SECONDS:.0f}s before retry...", file=sys.stderr)
            await asyncio.sleep(RETRY_BACKOFF_SECONDS)
        else:
            print(f"  [{pair['test_id']}] WARNING: still fell back after {MAX_ATTEMPTS} attempts "
                  f"— keeping the fallback line, revisit this one manually.", file=sys.stderr)
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
    print(f"Loaded {len(pairs)} test pairs. Composing sequentially "
          f"(~{PACING_SECONDS:.0f}s apart to respect provider rate limits)...")

    results = []
    fallback_count = 0
    for i, pair in enumerate(pairs, 1):
        print(f"[{i}/{len(pairs)}] {pair['test_id']}...")
        r = await _compose_one(bot, pair)
        if r["rationale"] == FALLBACK_RATIONALE:
            fallback_count += 1
        results.append(r)
        if i < len(pairs):
            await asyncio.sleep(PACING_SECONDS)

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"Wrote {len(results)} lines to {OUT_PATH}")
    if fallback_count:
        print(f"WARNING: {fallback_count}/{len(results)} lines still used the generic fallback "
              f"template after retries — check the log above for which test_ids, and consider "
              f"re-running just those once your provider quota has recovered.", file=sys.stderr)
    else:
        print("All lines came from real LLM composition — none fell back to the generic template.")


if __name__ == "__main__":
    asyncio.run(main())
