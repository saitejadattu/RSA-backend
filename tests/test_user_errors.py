from fastapi import HTTPException, status

from app.utils.user_errors import (
    AI_AUTH_MESSAGE,
    AI_INVALID_INPUT_MESSAGE,
    AI_NETWORK_MESSAGE,
    AI_PROVIDER_MESSAGE,
    AI_QUOTA_MESSAGE,
    AI_TIMEOUT_MESSAGE,
    AI_UNKNOWN_MESSAGE,
    user_facing_ai_error,
)


def test_quota_error_is_sanitized():
    error = HTTPException(status_code=502, detail="Gemini request failed: 429 quota_metric=secret model=private")
    assert user_facing_ai_error(error) == AI_QUOTA_MESSAGE


def test_timeout_error_is_sanitized():
    assert user_facing_ai_error(TimeoutError("provider deadline exceeded")) == AI_TIMEOUT_MESSAGE


def test_network_error_is_sanitized():
    assert user_facing_ai_error(ConnectionError("connection refused https://provider.internal")) == AI_NETWORK_MESSAGE


def test_provider_5xx_is_sanitized():
    assert user_facing_ai_error(HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="provider error")) == AI_PROVIDER_MESSAGE


def test_auth_error_is_sanitized():
    assert user_facing_ai_error(HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")) == AI_AUTH_MESSAGE


def test_invalid_input_is_sanitized():
    assert user_facing_ai_error(HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid JSON")) == AI_INVALID_INPUT_MESSAGE


def test_unknown_error_is_sanitized():
    assert user_facing_ai_error(RuntimeError("internal object details")) == AI_UNKNOWN_MESSAGE
