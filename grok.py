"""xAI Grok JSON decision gate; fail-closed when key/API/JSON is unavailable."""
from __future__ import annotations
import json, os
import httpx
from config import GROK_API_BASE, GROK_MODEL, GROK_TIMEOUT_SECONDS, GROK_CONFIDENCE_THRESHOLD
from prompts import SYSTEM_PROMPT, build_user_prompt

async def evaluate_token(token: dict, rugcheck_result: dict) -> dict:
    api_key = os.getenv("XAI_API_KEY", "").strip()
    if not api_key:
        return {"provider": "Grok", "decision": "SKIP", "confidence": 0.0, "reasons": ["xai_api_key_missing"], "passed": False}
    body = {
        "model": GROK_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(token, rugcheck_result)},
        ],
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=GROK_TIMEOUT_SECONDS, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}) as client:
            response = await client.post(f"{GROK_API_BASE}/chat/completions", json=body)
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        decision = str(parsed.get("decision", "SKIP")).upper()
        confidence = float(parsed.get("confidence", 0))
        if decision not in {"COPY", "SKIP"} or not 0 <= confidence <= 1:
            raise ValueError("invalid_decision_schema")
        passed = decision == "COPY" and confidence >= GROK_CONFIDENCE_THRESHOLD
        return {
            "provider": "Grok", "model": GROK_MODEL, "decision": decision,
            "confidence": confidence, "passed": passed,
            "reasons": parsed.get("reasons", []) if isinstance(parsed.get("reasons", []), list) else ["invalid_reasons"],
            "missing_data": parsed.get("missing_data", []) if isinstance(parsed.get("missing_data", []), list) else [],
            "threshold": GROK_CONFIDENCE_THRESHOLD,
        }
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
        return {"provider": "Grok", "decision": "SKIP", "confidence": 0.0, "passed": False, "reasons": [f"grok_unavailable_or_invalid:{type(exc).__name__}"]}
