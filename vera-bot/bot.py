"""
magicpin AI Challenge — Vera bot
=================================
FastAPI service implementing the 5-endpoint judge contract:
  POST /v1/context   - receive category/merchant/customer/trigger pushes
  POST /v1/tick       - proactively decide what to send
  POST /v1/reply      - respond to a merchant/customer reply
  GET  /v1/healthz    - liveness
  GET  /v1/metadata   - team identity

Composer: deterministic prompt -> LLM (temperature=0) -> validated JSON output.
Primary provider: Gemini. Fallback: Groq (used if Gemini errors/rate-limits).

Env vars required:
  GEMINI_API_KEY
  GROQ_API_KEY   (fallback)
  TEAM_NAME, TEAM_MEMBERS, CONTACT_EMAIL (optional, used in /v1/metadata)
"""

import os
import re
import json
import time
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv
    load_dotenv()  # loads GEMINI_API_KEY / GROQ_API_KEY from a local .env file if present
except ImportError:
    pass  # dotenv optional — fine in production where env vars are set by the host (e.g. Render)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vera-bot")

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

TEAM_NAME = os.environ.get("TEAM_NAME", "Asad Solo")
TEAM_MEMBERS = os.environ.get("TEAM_MEMBERS", "Shaik Asad Ahmed")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "")
BOT_VERSION = "1.0.0"

START_TS = time.time()

# tick calls can carry several active triggers at once; the judge gives the
# whole /v1/tick call a 30s budget, so we fan compose() calls out concurrently
# instead of awaiting them one at a time. Cap concurrency so we don't blow
# through free-tier LLM rate limits.
LLM_CONCURRENCY = int(os.environ.get("LLM_CONCURRENCY", "6"))
_llm_semaphore = asyncio.Semaphore(LLM_CONCURRENCY)

AUTO_REPLY_END_THRESHOLD = 2  # 3rd verbatim-identical incoming message => end

# --------------------------------------------------------------------------
# In-memory stores
# --------------------------------------------------------------------------
# contexts[(scope, context_id)] = {"version": int, "payload": dict}
contexts: dict[tuple[str, str], dict[str, Any]] = {}

# conversations[conversation_id] = {
#   "merchant_id": str, "customer_id": str|None, "send_as": str,
#   "turns": [ {"from": "vera"/"merchant"/"customer", "body": str, "ts": str} ],
#   "sent_bodies": set[str],   # anti-repetition
#   "auto_reply_streak": int,  # consecutive identical merchant replies
# }
conversations: dict[str, dict[str, Any]] = {}

app = FastAPI(title="Vera Challenge Bot")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_ctx(scope: str, context_id: str) -> Optional[dict]:
    entry = contexts.get((scope, context_id))
    return entry["payload"] if entry else None


# --------------------------------------------------------------------------
# LLM providers (Gemini primary, Groq fallback) — both OpenAI-compatible-ish
# --------------------------------------------------------------------------

class LLMError(Exception):
    pass


async def _call_gemini(system_prompt: str, user_prompt: str) -> str:
    if not GEMINI_API_KEY:
        raise LLMError("GEMINI_API_KEY not set")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json=body)
    if r.status_code != 200:
        raise LLMError(f"gemini {r.status_code}: {r.text[:300]}")
    data = r.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise LLMError(f"gemini malformed response: {e} / {data}")


async def _call_groq(system_prompt: str, user_prompt: str) -> str:
    if not GROQ_API_KEY:
        raise LLMError("GROQ_API_KEY not set")
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        # Groq sits behind Cloudflare, which can reject requests carrying a
        # generic/library default User-Agent as bot traffic (HTTP 403,
        # Cloudflare error 1010). A normal browser-style UA avoids that.
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    body = {
        "model": GROQ_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, headers=headers, json=body)
    if r.status_code != 200:
        raise LLMError(f"groq {r.status_code}: {r.text[:300]}")
    data = r.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise LLMError(f"groq malformed response: {e} / {data}")


async def call_llm(system_prompt: str, user_prompt: str) -> dict:
    """Try Gemini first, fall back to Groq. Returns parsed JSON dict.
    Bounded by a semaphore so a burst of concurrent tick triggers can't blow
    past free-tier LLM rate limits or the judge's 30s call budget."""
    async with _llm_semaphore:
        last_err = None
        for fn, name in ((_call_gemini, "gemini"), (_call_groq, "groq")):
            try:
                raw = await fn(system_prompt, user_prompt)
                cleaned = re.sub(r"^```json\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
                return json.loads(cleaned)
            except Exception as e:  # noqa: BLE001
                log.warning("LLM provider %s failed: %s", name, e)
                last_err = e
                continue
        raise LLMError(f"all providers failed: {last_err}")


# --------------------------------------------------------------------------
# Prompt construction (the actual "composer")
# --------------------------------------------------------------------------

COMPOSER_SYSTEM_PROMPT = """You are Vera, magicpin's WhatsApp AI assistant for merchant growth. \
You compose ONE outbound message given four context layers: category, merchant, trigger, and \
optionally customer. You must follow these hard rules:

1. Anchor on a concrete, verifiable fact from the given context (a number, date, headline, or \
peer stat). Never invent facts, offers, research citations, or competitor names not present in \
the context.
2. Match the category voice exactly (tone, vocabulary, taboos). Clinical categories (dentists, \
doctors) get peer/clinical tone, never promotional hype.
3. Personalize using the merchant's actual performance numbers, offers, and conversation history. \
Honor the merchant's language preference — Hindi-English code-mix ("hi-en mix") is encouraged \
when the merchant's languages include "hi".
4. Use exactly ONE primary call-to-action. For action-worthy triggers, prefer a low-friction \
binary ask (e.g. reply YES / a simple choice). For pure-information triggers, no forced CTA is \
also acceptable ("none").
5. Use at least one compulsion lever: specificity, loss aversion, social proof, effort \
externalization ("I've drafted X, just say go"), curiosity, reciprocity, asking the merchant a \
direct question, or a single binary commitment.
6. Keep it concise. No long preambles, no re-introducing yourself if this isn't the first message \
in the conversation history. The call-to-action must land in the last sentence.
7. If send_as is "merchant_on_behalf" (a customer-facing message), never use language implying \
guarantees or cures, and always reflect the customer's actual relationship/state/preferences.
8. Never repeat a message body verbatim that was already sent in this conversation (see \
`previous_bodies_in_conversation` in the input).

Return ONLY a JSON object with exactly these keys:
{
  "body": "<the WhatsApp message text>",
  "cta": "<'binary' | 'open_ended' | 'none'>",
  "send_as": "<'vera' | 'merchant_on_behalf'>",
  "suppression_key": "<a short dedup key for this message class>",
  "rationale": "<one or two sentences: why this message, why now, what it should achieve>"
}
No markdown fences, no extra text, just the JSON object.
"""

REPLY_SYSTEM_PROMPT = """You are Vera, continuing an in-progress WhatsApp conversation with a \
merchant or their customer. You are given the full conversation so far and the latest incoming \
message. Decide the next move.

Rules:
1. Detect auto-replies: if the incoming message looks like a generic canned reply (e.g. "Thank \
you for contacting us, our team will get back to you") even without an exact repeat yet, note it — \
but the caller already handles the hard "3 near-identical repeats = end" case deterministically \
before calling you, so you only need to use judgment for near-duplicate or clearly-templated single \
occurrences. Two fields help you: `auto_reply_streak` (how many normalized-identical repeats have \
occurred so far, including this one) and `canned_pattern_match` (true if this message's wording \
matches common auto-responder boilerplate even on its first occurrence). If `canned_pattern_match` \
is true, lean toward "wait" with a longer wait_seconds rather than "send" — don't keep messaging \
into what looks like an unattended inbox.
2. Detect explicit intent ("yes", "I want to join", "go ahead", "let's do it") and route straight \
to action — do not re-ask qualifying questions. The input's `intent_detected` field is a \
deterministic pre-check for this. If `intent_detected` is true, it is non-negotiable: your reply \
must NOT contain a question mark, must NOT re-ask or re-confirm anything the merchant/customer just \
agreed to, and must instead state the concrete next step in one decisive sentence (what happens \
now, or the one piece of info you genuinely still need to execute — never "are you sure" or "would \
you like to proceed").
3. If the other party asks for time / says "later" / "not now", choose "wait" with a reasonable \
wait_seconds (900-3600).
4. If they say "not interested" / "stop" / clearly decline, choose "end" gracefully, no hard sell.
5. If the message is abusive, hostile, or contains profanity: do not mirror the tone, do not \
apologize excessively, and do not end the conversation just because of hostility. Stay polite and \
professional, briefly acknowledge, and steer back to the original purpose with "send".
6. If the message asks something clearly outside Vera's mission (unrelated to this merchant's \
magicpin growth/marketing — e.g. asking for help with GST filing, personal favors, general trivia): \
politely decline that specific ask in one short clause and redirect back to the original topic in \
the same message. Do not attempt to actually help with the off-topic request.
7. Otherwise choose "send" with the next best low-friction message — single CTA, grounded in \
context already established in this conversation, no invented facts.
8. Never repeat a body already sent in this conversation.

Return ONLY a JSON object with exactly these keys:
{
  "action": "<'send' | 'wait' | 'end'>",
  "body": "<only if action is 'send' — the message text>",
  "cta": "<'binary' | 'open_ended' | 'none' — only if action is 'send'>",
  "wait_seconds": <integer — only if action is 'wait'>,
  "rationale": "<one sentence explaining the decision>"
}
Omit keys that don't apply to the chosen action. No markdown fences, just the JSON object.
"""


_WS_RE = re.compile(r"\s+")
_TRAILING_PUNCT_RE = re.compile(r"[!.?,]+$")


def _normalize_for_dedup(text: str) -> str:
    """Normalize a message for auto-reply/duplicate comparison: case-fold,
    collapse whitespace, drop trailing punctuation. Two canned replies that
    differ only by a stray space, a trailing period, or casing should still
    count as the 'same' message for streak purposes — exact byte-equality
    was too strict and let real auto-replies slip through undetected."""
    t = text.strip().lower()
    t = _WS_RE.sub(" ", t)
    t = _TRAILING_PUNCT_RE.sub("", t)
    return t


_CANNED_REPLY_PATTERNS = [
    r"thank(?:s| you) for (?:contacting|reaching out)",
    r"(?:our )?team will (?:get back|revert|respond)",
    r"we(?:'| ha)ve received your (?:message|query|request)",
    r"currently (?:unavailable|away|out of office)",
    r"this is an automated (?:reply|response|message)",
    r"will (?:get back to you|revert) (?:shortly|soon|within)",
]
_CANNED_REPLY_RE = re.compile("|".join(_CANNED_REPLY_PATTERNS), re.IGNORECASE)


def _looks_canned(text: str) -> bool:
    """Heuristic single-occurrence canned-reply detector (auto-responder
    boilerplate) — fires on the FIRST occurrence, independent of the
    repeat-streak check below, so the LLM gets a signal even before a
    message has repeated at all."""
    return bool(_CANNED_REPLY_RE.search(text))


_INTENT_PATTERNS = [
    r"\byes\b", r"\byeah\b", r"\byep\b", r"\bsure\b", r"\bok(?:ay)?\b",
    r"\bgo ahead\b", r"\blet'?s do (?:it|this)\b", r"\bi'?m in\b",
    r"\bcount me in\b", r"\bsign me up\b", r"\bi want to join\b",
    r"\bi'?d like to\b", r"\bplease proceed\b", r"\bconfirm(?:ed)?\b",
    r"\bagreed\b", r"\bdone deal\b", r"\bi want it\b", r"\blet'?s go\b",
]
_INTENT_RE = re.compile("|".join(_INTENT_PATTERNS), re.IGNORECASE)


def _detect_explicit_intent(text: str) -> bool:
    """Deterministic pre-check for an explicit affirmative/action signal
    ('yes', 'go ahead', 'sign me up', ...). Used to force the composer to
    route straight to the next action step instead of re-asking a
    qualifying question it already has the answer to."""
    return bool(_INTENT_RE.search(text.strip()))


def _digest_for(category: dict, digest_id: Optional[str]) -> Optional[dict]:
    if not digest_id:
        return None
    for item in category.get("digest", []):
        if item.get("id") == digest_id:
            return item
    return None


def build_compose_input(category: dict, merchant: dict, trigger: dict, customer: Optional[dict]) -> dict:
    """Slim down the raw context dicts to what the LLM actually needs, resolving
    any id-references (e.g. trigger.payload.top_item_id -> the actual digest item)."""
    trig_payload = dict(trigger.get("payload", {}))
    top_item_id = trig_payload.pop("top_item_id", None)
    if top_item_id:
        resolved = _digest_for(category, top_item_id)
        if resolved:
            trig_payload["top_item"] = resolved

    conv_id = f"conv_{merchant.get('merchant_id')}_{trigger.get('id') or trigger.get('trigger_id', 'x')}"
    prior = conversations.get(conv_id, {})

    return {
        "category": {
            "slug": category.get("slug"),
            "voice": category.get("voice"),
            "offer_catalog": category.get("offer_catalog"),
            "peer_stats": category.get("peer_stats"),
            "seasonal_beats": category.get("seasonal_beats"),
            "trend_signals": category.get("trend_signals"),
        },
        "merchant": {
            "merchant_id": merchant.get("merchant_id"),
            "identity": merchant.get("identity"),
            "subscription": merchant.get("subscription"),
            "performance": merchant.get("performance"),
            "offers": merchant.get("offers"),
            "conversation_history": merchant.get("conversation_history", [])[-5:],
            "customer_aggregate": merchant.get("customer_aggregate"),
            "signals": merchant.get("signals"),
        },
        "trigger": {
            "id": trigger.get("id") or trigger.get("trigger_id"),
            "scope": trigger.get("scope"),
            "kind": trigger.get("kind"),
            "source": trigger.get("source"),
            "urgency": trigger.get("urgency"),
            "payload": trig_payload,
        },
        "customer": customer,
        "previous_bodies_in_conversation": list(prior.get("sent_bodies", [])),
    }


_NUMBER_RE = re.compile(r"₹\s?[\d,]+(?:\.\d+)?|\b\d{1,3}(?:,\d{3})*(?:\.\d+)?%?\b")


def _grounded_in_context(body: str, context_blob: str) -> bool:
    """Deterministic anti-hallucination check: every numeric token in the
    generated body (prices, percentages, counts) must appear somewhere in the
    source context we actually sent the LLM. Catches invented figures the
    prompt-level instruction alone might miss under time pressure."""
    body_numbers = set(_NUMBER_RE.findall(body))
    if not body_numbers:
        return True  # nothing numeric to verify
    for n in body_numbers:
        bare = n.replace("₹", "").replace(",", "").replace("%", "").strip()
        if bare and bare not in context_blob:
            return False
    return True


async def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict]) -> dict:
    payload = build_compose_input(category, merchant, trigger, customer)
    context_blob = json.dumps(payload, ensure_ascii=False).replace(",", "").replace("₹", "")
    user_prompt = "Compose the next Vera message from this context:\n\n" + json.dumps(
        payload, ensure_ascii=False, indent=2
    )
    try:
        result = await call_llm(COMPOSER_SYSTEM_PROMPT, user_prompt)
        body = str(result.get("body", "")).strip()
        if body and not _grounded_in_context(body, context_blob):
            log.warning("compose() produced an ungrounded number, retrying once: %r", body)
            # one retry with an explicit correction nudge before falling back
            retry_prompt = user_prompt + (
                "\n\nNOTE: your previous attempt included a number not present in the context "
                "above. Only use numbers that literally appear in the JSON context. Retry."
            )
            result = await call_llm(COMPOSER_SYSTEM_PROMPT, retry_prompt)
            body = str(result.get("body", "")).strip()
            if not _grounded_in_context(body, context_blob):
                log.warning("compose() still ungrounded after retry, using safe fallback")
                result = _fallback_compose(merchant, trigger)
    except LLMError as e:
        log.error("compose() LLM failure, using safe fallback: %s", e)
        result = _fallback_compose(merchant, trigger)

    result.setdefault("cta", "open_ended")
    result.setdefault("send_as", "merchant_on_behalf" if customer else "vera")
    result.setdefault("suppression_key", trigger.get("suppression_key", ""))
    result.setdefault("rationale", "Composed from category+merchant+trigger context.")
    result["body"] = str(result.get("body", "")).strip()
    return result


def _fallback_compose(merchant: dict, trigger: dict) -> dict:
    """Deterministic, non-LLM safety net if both providers fail — keeps the bot
    responding within the 30s budget instead of erroring out."""
    name = merchant.get("identity", {}).get("name", "there")
    kind = trigger.get("kind", "update")
    return {
        "body": f"Hi {name}, quick update on your account regarding {kind.replace('_', ' ')} — want the details?",
        "cta": "open_ended",
        "send_as": "vera",
        "suppression_key": trigger.get("suppression_key", ""),
        "rationale": "Fallback path — LLM providers unavailable, used minimal grounded template.",
    }


async def compose_reply(conv: dict, from_role: str, message: str) -> dict:
    # Auto-reply detection: count consecutive near-identical incoming
    # messages from the same party, on NORMALIZED text (case/whitespace/
    # trailing-punctuation insensitive) so a canned reply that differs only
    # by formatting still counts toward the streak. The caller already
    # appended this call's message to conv["turns"] before invoking us, so
    # we exclude it from the scan and count it separately as the base of 1
    # — the previous version counted it twice inside the same list, which
    # made the deterministic cutoff fire one occurrence too early (2nd
    # repeat instead of the intended 3rd).
    norm_message = _normalize_for_dedup(message)
    prior_incoming = [t["body"] for t in conv["turns"][:-1] if t["from"] == from_role]
    auto_streak = 1  # this message itself
    for b in reversed(prior_incoming):
        if _normalize_for_dedup(b) == norm_message:
            auto_streak += 1
        else:
            break

    # Deterministic short-circuit — don't rely on the LLM to notice a pattern
    # under time pressure. AUTO_REPLY_END_THRESHOLD prior near-identical
    # repeats + this one (i.e. the 3rd occurrence, per the brief's hint) =
    # canned auto-reply -> exit immediately, no further LLM call needed.
    if auto_streak > AUTO_REPLY_END_THRESHOLD:
        return {
            "action": "end",
            "rationale": f"Detected {auto_streak} near-identical replies in a row (normalized) — "
                         f"treating as a canned auto-reply, exiting to avoid spamming a non-human "
                         f"responder.",
        }

    canned_pattern_match = _looks_canned(message)
    intent_detected = _detect_explicit_intent(message)

    payload = {
        "from_role": from_role,
        "latest_message": message,
        "auto_reply_streak": auto_streak,
        "canned_pattern_match": canned_pattern_match,
        "intent_detected": intent_detected,
        "conversation_so_far": conv["turns"][-10:],
        "previous_bodies_sent_by_bot": list(conv.get("sent_bodies", [])),
    }
    user_prompt = "Decide the next move for this conversation:\n\n" + json.dumps(
        payload, ensure_ascii=False, indent=2
    )
    try:
        result = await call_llm(REPLY_SYSTEM_PROMPT, user_prompt)
        # Safety net: intent_detected is supposed to be non-negotiable, but an
        # LLM under instruction pressure can still slip a qualifying question
        # back in. Catch it deterministically and force one corrective retry
        # (same pattern as the anti-hallucination retry in compose()).
        if (
            intent_detected
            and result.get("action") == "send"
            and str(result.get("body", "")).strip().endswith("?")
        ):
            log.warning("compose_reply(): intent_detected but reply still asked a question, retrying once")
            retry_prompt = user_prompt + (
                "\n\nNOTE: intent_detected is true and your previous reply still ended in a "
                "question mark. That is not allowed here. Rewrite the reply to move straight to "
                "the next concrete action step — no question marks, no re-confirming what they "
                "already agreed to."
            )
            result = await call_llm(REPLY_SYSTEM_PROMPT, retry_prompt)
    except LLMError as e:
        log.error("compose_reply() LLM failure, using safe fallback: %s", e)
        result = {"action": "wait", "wait_seconds": 1800, "rationale": "LLM unavailable; backing off."}

    result.setdefault("action", "wait")
    if result["action"] == "wait":
        result.setdefault("wait_seconds", 1800)
    if result["action"] == "send":
        result.setdefault("cta", "open_ended")
        result["body"] = str(result.get("body", "")).strip()
    result.setdefault("rationale", "")
    return result


# --------------------------------------------------------------------------
# Endpoint models
# --------------------------------------------------------------------------

class CtxBody(BaseModel):
    scope: Literal["category", "merchant", "customer", "trigger"]
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in contexts.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TS),
        "contexts_loaded": counts,
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": TEAM_NAME,
        "team_members": [m.strip() for m in TEAM_MEMBERS.split(",") if m.strip()],
        "model": f"{GEMINI_MODEL} (primary) / {GROQ_MODEL} (fallback)",
        "approach": "Deterministic (temp=0) LLM composer grounded strictly in pushed context; "
                    "Gemini primary with Groq fallback; rule-based safety-net template if both fail.",
        "contact_email": CONTACT_EMAIL,
        "version": BOT_VERSION,
        "submitted_at": utcnow_iso(),
    }


@app.post("/v1/context")
async def push_context(body: CtxBody):
    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": utcnow_iso(),
    }


@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    conversations.clear()
    return {"accepted": True}


def _is_first_touch(conv_id: str) -> bool:
    """No prior 'vera' turn in this conversation => this send must use the
    approved WhatsApp template (24h session window rule). Once a merchant or
    customer has replied, later sends within the session can be free-form."""
    conv = conversations.get(conv_id)
    if not conv:
        return True
    return not any(t["from"] == "vera" for t in conv["turns"])


async def _compose_one_action(trg_id: str, now: str) -> Optional[dict]:
    """Resolve one trigger -> one (context, compose-call, conversation-write)
    unit. Returns None if this trigger should produce no action."""
    trig = get_ctx("trigger", trg_id)
    if not trig:
        return None

    merchant_id = trig.get("merchant_id") or trig.get("payload", {}).get("merchant_id")
    merchant = get_ctx("merchant", merchant_id) if merchant_id else None
    if not merchant:
        return None

    category_slug = merchant.get("category_slug")
    category = get_ctx("category", category_slug) if category_slug else None
    if not category:
        return None

    customer = None
    customer_id = trig.get("customer_id")
    if customer_id:
        customer = get_ctx("customer", customer_id)

    conv_id = f"conv_{merchant_id}_{trig.get('id', trg_id)}"

    # skip if this exact suppression_key already fired for this conversation
    existing = conversations.get(conv_id)
    supp_key = trig.get("suppression_key", "")
    if existing and supp_key and existing.get("last_suppression_key") == supp_key:
        return None

    try:
        composed = await compose(category, merchant, trig, customer)
    except Exception as e:  # noqa: BLE001
        log.exception("compose failed for trigger %s: %s", trg_id, e)
        return None

    if not composed.get("body"):
        return None

    first_touch = _is_first_touch(conv_id)

    conv = conversations.setdefault(conv_id, {
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "send_as": composed["send_as"],
        "turns": [],
        "sent_bodies": set(),
    })
    conv["turns"].append({"from": "vera", "body": composed["body"], "ts": now})
    conv["sent_bodies"].add(composed["body"])
    conv["last_suppression_key"] = supp_key

    action = {
        "conversation_id": conv_id,
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "send_as": composed["send_as"],
        "trigger_id": trig.get("id", trg_id),
        "body": composed["body"],
        "cta": composed.get("cta", "open_ended"),
        "suppression_key": supp_key,
        "rationale": composed.get("rationale", ""),
        "is_first_touch": first_touch,
    }
    if first_touch:
        # WhatsApp 24h session-window rule: first outbound must be a
        # pre-approved template. Subsequent free-form sends omit these.
        action["template_name"] = f"vera_{trig.get('kind', 'generic')}_v1"
        action["template_params"] = [merchant.get("identity", {}).get("name", "")]
    return action


@app.post("/v1/tick")
async def tick(body: TickBody):
    # Fan every active trigger's compose() call out concurrently — a single
    # tick can carry several active triggers, and the judge gives the whole
    # call only 30s, so sequential awaiting risks a timeout penalty.
    trigger_ids = body.available_triggers[:20]  # respect the 20-action cap up front
    results = await asyncio.gather(
        *(_compose_one_action(trg_id, body.now) for trg_id in trigger_ids),
        return_exceptions=True,
    )

    actions = []
    for trg_id, r in zip(trigger_ids, results):
        if isinstance(r, Exception):
            log.exception("tick: trigger %s raised: %s", trg_id, r)
            continue
        if r is not None:
            actions.append(r)

    return {"actions": actions[:20]}


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conv = conversations.get(body.conversation_id)
    if conv is None:
        conv = conversations.setdefault(body.conversation_id, {
            "merchant_id": body.merchant_id,
            "customer_id": body.customer_id,
            "send_as": "vera",
            "turns": [],
            "sent_bodies": set(),
        })

    conv["turns"].append({"from": body.from_role, "body": body.message, "ts": body.received_at})

    try:
        result = await compose_reply(conv, body.from_role, body.message)
    except Exception as e:  # noqa: BLE001
        log.exception("compose_reply failed: %s", e)
        return {"action": "wait", "wait_seconds": 1800, "rationale": "internal error; backing off"}

    action = result.get("action", "wait")

    if action == "send":
        response_body = result.get("body", "").strip()
        if not response_body or response_body in conv["sent_bodies"]:
            return {"action": "wait", "wait_seconds": 900, "rationale": "avoided empty/repeat message"}
        conv["turns"].append({"from": "vera", "body": response_body, "ts": utcnow_iso()})
        conv["sent_bodies"].add(response_body)
        return {
            "action": "send",
            "body": response_body,
            "cta": result.get("cta", "open_ended"),
            "rationale": result.get("rationale", ""),
        }

    if action == "end":
        return {"action": "end", "rationale": result.get("rationale", "")}

    return {
        "action": "wait",
        "wait_seconds": int(result.get("wait_seconds", 1800)),
        "rationale": result.get("rationale", ""),
    }


@app.get("/")
async def root():
    return {"service": "vera-challenge-bot", "status": "ok", "docs": "/docs"}