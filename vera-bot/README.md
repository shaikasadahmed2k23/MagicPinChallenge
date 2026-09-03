# Vera Challenge Bot

FastAPI service implementing the magicpin AI Challenge 5-endpoint judge contract.

## Endpoints
- `POST /v1/context` — receive category/merchant/customer/trigger context pushes (idempotent by version)
- `POST /v1/tick` — decide proactive actions for the given `available_triggers`
- `POST /v1/reply` — decide next move (`send` / `wait` / `end`) on an incoming reply
- `GET  /v1/healthz` — liveness check
- `GET  /v1/metadata` — team + model info
- `POST /v1/teardown` — clears all in-memory state (used between judge test runs)

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
