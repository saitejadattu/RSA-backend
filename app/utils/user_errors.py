from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status


AI_QUOTA_MESSAGE = "AI analysis is temporarily unavailable because the service limit was reached. Please try again later."
AI_TIMEOUT_MESSAGE = "AI analysis took too long to respond. Please try again."
AI_NETWORK_MESSAGE = "We couldn't reach the AI analysis service. Please try again."
AI_AUTH_MESSAGE = "AI analysis is temporarily unavailable. Please contact the administrator."
AI_PROVIDER_MESSAGE = "The AI analysis service is temporarily unavailable. Please try again later."
AI_INVALID_INPUT_MESSAGE = "The analysis could not be completed because the provided data is invalid."
AI_UNKNOWN_MESSAGE = "The analysis could not be completed. Please try again later."


def user_facing_ai_error(error: BaseException | Any) -> str:
    """Map technical AI/provider failures to a safe message for API clients."""
    detail = error.detail if isinstance(error, HTTPException) else error
    status_code = error.status_code if isinstance(error, HTTPException) else None
    text = str(detail or error).lower()

    if status_code == status.HTTP_429_TOO_MANY_REQUESTS or any(
        marker in text for marker in ("429", "quota", "rate limit", "rate-limit", "resource exhausted")
    ):
        return AI_QUOTA_MESSAGE
    if any(marker in text for marker in ("timeout", "timed out", "deadline exceeded")):
        return AI_TIMEOUT_MESSAGE
    if any(marker in text for marker in ("connection", "connecterror", "network", "dns", "unreachable")):
        return AI_NETWORK_MESSAGE
    if status_code in {status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN} or any(
        marker in text for marker in ("api key", "apikey", "authentication", "unauthorized", "forbidden", "invalid key")
    ):
        return AI_AUTH_MESSAGE
    if status_code is not None and status_code >= 500:
        return AI_PROVIDER_MESSAGE
    if status_code in {status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY} or any(
        marker in text for marker in ("invalid input", "invalid json", "non-object", "malformed", "unsupported")
    ):
        return AI_INVALID_INPUT_MESSAGE
    return AI_UNKNOWN_MESSAGE


def sanitize_ai_failure(error: BaseException | Any) -> str:
    """Return a safe candidate-level failure message without exposing exception data."""
    return user_facing_ai_error(error)
