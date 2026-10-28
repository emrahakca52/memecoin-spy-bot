"""AI JSON decision gate with optional Gemini free-tier support; fail-closed on errors."""
from __future__ import annotations
import json
import os
import httpx
from config import GROK_API_BASE, GROK_MODEL, GROK_TIMEOUT_SECONDS, GROK_CONFIDENCE_THRESHOLD
from prompts import SYSTEM_PROMPT, build_user_prompt


async def evaluate_token(token: dict, rugcheck_result: dict) -> dict:
    provider = os.getenv("AI_PROVIDER", "grok").strip().lower()
    prompt = build_user_prompt(token, rugcheck_result)

    if provider == "gemini":
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip()
        if not api_key:
            return {"provider": "Gemini", "decision": "SKIP", "confidence": 0.0,
                    "reasons": ["gemini_api_key_missing"], "passed": False}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        provider_name = "Gemini"
    else:
        api_key = os.getenv("XAI_API_KEY", "").strip()
        model = GROK_MODEL
        if not api_key:
            return {"provider": "Grok", "decision": "SKIP", "confidence": 0.0,
                    "reasons": ["xai_api_key_missing"], "passed": False}
        url = f"{GROK_API_BASE}/chat/completions"
        body = {
            "model": model, "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        provider_name = "Grok"

    try:
        async with httpx.AsyncClient(timeout=GROK_TIMEOUT_SECONDS, headers=headers) as client:
            response = await client.post(url, json=body)
        response.raise_for_status()
        payload = response.json()
        if provider == "gemini":
            parts = payload["candidates"][0]["content"]["parts"]
            content = next(part["text"] for part in parts if isinstance(part.get("text"), str))
        else:
            content = payload["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        decision = str(parsed.get("decision", "SKIP")).upper()
        confidence = float(parsed.get("confidence", 0))
        if decision not in {"COPY", "SKIP"} or not 0 <= confidence <= 1:
            raise ValueError("invalid_decision_schema")
        passed = decision == "COPY" and confidence >= GROK_CONFIDENCE_THRESHOLD
        return {
            "provider": provider_name, "model": model, "decision": decision,
            "confidence": confidence, "passed": passed,
            "reasons": parsed.get("reasons", []) if isinstance(parsed.get("reasons", []), list) else ["invalid_reasons"],
            "missing_data": parsed.get("missing_data", []) if isinstance(parsed.get("missing_data", []), list) else [],
            "threshold": GROK_CONFIDENCE_THRESHOLD,
        }
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, StopIteration) as exc:
        return {"provider": provider_name, "model": model, "decision": "SKIP", "confidence": 0.0,
                "passed": False, "reasons": [f"ai_unavailable_or_invalid:{type(exc).__name__}"]}
