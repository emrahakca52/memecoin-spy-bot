"""AI JSON decision gate with optional Gemini support; fail-closed on errors."""
from __future__ import annotations
import json
import logging
import os
import httpx
from config import GROK_API_BASE, GROK_MODEL, GROK_TIMEOUT_SECONDS, GROK_CONFIDENCE_THRESHOLD
from prompts import SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)


async def evaluate_token(token: dict, rugcheck_result: dict) -> dict:
    provider = os.getenv("AI_PROVIDER", "grok").strip().lower()
    prompt = build_user_prompt(token, rugcheck_result)

    if provider == "gemini":
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
        provider_name = "Gemini"
        if not api_key:
            logger.warning("AI screening skipped provider=Gemini reason=api_key_missing symbol=%s", token.get("symbol"))
            return {"provider": provider_name, "model": model, "decision": "SKIP", "confidence": 0.0,
                    "reasons": ["gemini_api_key_missing"], "passed": False, "api_ok": False}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    else:
        api_key = os.getenv("XAI_API_KEY", "").strip()
        model = GROK_MODEL
        provider_name = "Grok"
        if not api_key:
            logger.warning("AI screening skipped provider=Grok reason=api_key_missing symbol=%s", token.get("symbol"))
            return {"provider": provider_name, "model": model, "decision": "SKIP", "confidence": 0.0,
                    "reasons": ["xai_api_key_missing"], "passed": False, "api_ok": False}
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

    logger.info("AI screening request provider=%s model=%s symbol=%s", provider_name, model, token.get("symbol"))
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
        reasons = parsed.get("reasons", []) if isinstance(parsed.get("reasons", []), list) else ["invalid_reasons"]
        missing_data = parsed.get("missing_data", []) if isinstance(parsed.get("missing_data", []), list) else []
        logger.info("AI screening response provider=%s model=%s symbol=%s api_ok=true decision=%s confidence=%.3f passed=%s",
                    provider_name, model, token.get("symbol"), decision, confidence, passed)
        return {
            "provider": provider_name, "model": model, "decision": decision,
            "confidence": confidence, "passed": passed, "api_ok": True,
            "reasons": reasons, "missing_data": missing_data,
            "threshold": GROK_CONFIDENCE_THRESHOLD,
        }
    except httpx.HTTPStatusError as exc:
        # Keep provider diagnostics short and never log request headers or API keys.
        try:
            error_payload = exc.response.json()
            error_obj = error_payload.get("error", {}) if isinstance(error_payload, dict) else {}
            provider_message = str(error_obj.get("message", ""))[:240]
            provider_status = str(error_obj.get("status", ""))[:80]
        except (ValueError, TypeError):
            provider_message = ""
            provider_status = ""
        logger.warning("AI screening HTTP error provider=%s model=%s symbol=%s status=%s provider_status=%s message=%s",
                       provider_name, model, token.get("symbol"), exc.response.status_code,
                       provider_status, provider_message)
        reasons = [f"ai_http_error:{exc.response.status_code}"]
        if provider_status:
            reasons.append(f"provider_status:{provider_status}")
        if provider_message:
            reasons.append(f"provider_message:{provider_message}")
        return {"provider": provider_name, "model": model, "decision": "SKIP", "confidence": 0.0,
                "passed": False, "api_ok": False, "http_status": exc.response.status_code,
                "reasons": reasons}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, StopIteration) as exc:
        logger.warning("AI screening error provider=%s model=%s symbol=%s error=%s",
                       provider_name, model, token.get("symbol"), type(exc).__name__)
        return {"provider": provider_name, "model": model, "decision": "SKIP", "confidence": 0.0,
                "passed": False, "api_ok": False, "reasons": [f"ai_unavailable_or_invalid:{type(exc).__name__}"]}
