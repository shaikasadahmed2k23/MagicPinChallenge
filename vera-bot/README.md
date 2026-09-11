# Vera Challenge Bot

FastAPI service implementing the magicpin AI Challenge 5-endpoint judge contract.

## Approach, tradeoffs, and what would have helped most

**Approach.** `compose()` builds one JSON prompt from the four context layers
(category voice/taboos, merchant identity/performance/offers/history, trigger
kind/payload/urgency, optional customer) and asks the LLM for a grounded,
category-voiced, single-CTA message at `temperature=0`. A deterministic
post-check (`_grounded_in_context`) verifies every number in the output
literally appears in the source context, with one corrective retry before
falling back to a minimal rule-based template — the bot should never invent a
stat, and never go silent even if both LLM providers are down.
`compose_reply()` (`/v1/reply`) layers three deterministic signals *before*
the LLM decides: an auto-reply streak counter (merchant-level, not just
conversation-level, since a real auto-responder doesn't care how we thread
conversations), a canned-boilerplate regex match, and an explicit-intent
regex match — so the bot doesn't rely on the LLM to notice an obvious pattern
under time pressure, and can short-circuit to `end` without even calling an
LLM once an auto-reply is confirmed.

**Tradeoffs.**
- *Reliability over single-provider "best" quality*: Gemini and Groq are
  raced concurrently per call (not sequential fallback) because Gemini's
  free-tier capacity has been visibly inconsistent (slow 503s under "high
  demand") during development — a race bounds worst-case latency to whichever
  provider actually responds, comfortably inside the judge's 30s budget,
  at the cost of always spending a Groq call even when Gemini would've been
  fine.
- *Determinism over LLM flexibility* for auto-reply/intent detection: regex
  pre-checks are less nuanced than pure LLM judgment on edge cases, but they
  are fast, free, and can't be talked out of the pattern the way a prompted
  instruction sometimes can under adversarial or repetitive input.
- *Grounding retry over always-first-answer*: the anti-hallucination retry
  roughly doubles that one call's latency when it fires, but a hallucinated
  number is a worse failure than a slower reply, per the challenge's own
  stated priorities — bounded by a per-trigger budget in `/v1/tick` so one
  retry can't sink an entire batch.
- *Rule-based fallback over erroring out*: if both LLM providers are
  unavailable, the bot still returns a safe, generic (ungrounded-but-honest)
  message rather than a 5xx — worse content quality, but never a broken
  endpoint.

**What additional context would have helped most.** Real reply/engagement
data per compulsion lever (curiosity vs. loss-aversion vs. social-proof) by
category, so message strategy could be tuned from evidence instead of the
static example set; more worked examples for the thinner categories
(pharmacies, salons) to calibrate voice as confidently as for
dentists/restaurants; and earlier visibility into exactly what the
post-submission context injection looks like (digest/performance/trigger
shape), to validate the grounding logic against it before submission rather
than only against the 30 canonical pairs.

## Endpoints
- `POST /v1/context` — receive category/merchant/customer/trigger context pushes (idempotent by version)
- `POST /v1/tick` — decide proactive actions for the given `available_triggers`
- `POST /v1/reply` — decide next move (`send` / `wait` / `end`) on an incoming reply
- `GET  /v1/healthz` — liveness check
- `GET  /v1/metadata` — team + model info
- `POST /v1/teardown` — clears all in-memory state (used between judge test runs)

## Generating submission.jsonl

The 30 canonical test pairs live in `../expanded/test_pairs.json`. From the
repo root:

```bash
python generate_submission.py
```

This resolves each pair's category/merchant/trigger/customer context from
`expanded/`, calls `compose()` directly (no HTTP hop), and writes
`vera-bot/submission.jsonl` — one JSON line per pair, in `T01..T30` order.

## Local run

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

# set your keys (PowerShell):
$env:GEMINI_API_KEY="your_key_here"
$env:GROQ_API_KEY="your_key_here"

uvicorn bot:app --reload --port 8000
```

Visit `http://127.0.0.1:8000/docs` for interactive API docs.

## Testing with judge_simulator.py

In the `MagicPinChallenge` repo, set `BOT_URL` (or whatever var the file uses) to
`http://127.0.0.1:8000`, set the judge's own `LLM_API_KEY` (this is a *separate*
key — the judge itself uses an LLM to score your bot's messages, unrelated to
the keys your bot uses to generate them), then run:

```bash
python judge_simulator.py
```

## Deploying to Render

1. Push this folder to a GitHub repo (or add to the existing `MagicPinChallenge` repo in a `vera-bot/` subfolder).
2. On Render: New → Web Service → connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn bot:app --host 0.0.0.0 --port $PORT`
5. Add environment variables: `GEMINI_API_KEY`, `GROQ_API_KEY`, and optionally
   `TEAM_NAME`, `TEAM_MEMBERS`, `CONTACT_EMAIL`.
6. Deploy. Your public URL (e.g. `https://vera-challenge-bot.onrender.com`) is
   what you submit in the challenge form.

> Free-tier Render services sleep after inactivity — the first request after a
> cold start may exceed the 30s judge timeout. If that's a risk near submission
> time, consider a paid instance or a keep-alive ping, or upgrade briefly.

## How composition works

`bot.py`'s `compose()` builds a single JSON prompt from the four context layers
(category voice/offers, merchant identity/performance/offers/history, trigger
kind/payload/urgency, optional customer) and calls Gemini at `temperature=0`
for determinism, falling back to Groq if Gemini errors or rate-limits, and
finally to a minimal rule-based template if both providers are unavailable —
so the bot never exceeds the 30s budget or crashes even under LLM outage.

`compose_reply()` handles `/v1/reply`: detects repeated/auto-reply messages
and backs off (`end` after repeated auto-replies), detects explicit intent and
routes straight to action, and otherwise proposes the next low-friction
message grounded only in the conversation already established.
