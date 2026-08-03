import secrets
import string
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status

from app.config.constants import (
    AUTH_STATUS_AUTHENTICATED,
    AUTH_STATUS_PASSWORD_RESET_REQUIRED,
    ROLE_ADMIN,
    ROLE_STUDENT,
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_PASSWORD_RESET,
)
from app.config.settings import get_settings
from app.db.collections import ADMINS, STUDENTS
from app.db.mongodb import get_database
from app.services.student_service import find_student_by_identifier
from app.utils.jwt import create_token, decode_token
from app.utils.object_id import to_object_id
from app.utils.password import hash_password, verify_password


ADMIN_EMAIL = "admin@2931"
ADMIN_PASSWORD = "admin2931"


def _access_token(student: dict) -> str:
    settings = get_settings()
    return create_token(
        subject=str(student["_id"]),
        token_type=TOKEN_TYPE_ACCESS,
        expires_delta=timedelta(minutes=settings.jwt_access_token_expire_minutes),
        extra_claims={"role": ROLE_STUDENT},
    )


def _admin_access_token() -> str:
    settings = get_settings()
    return create_token(
        subject=ADMIN_EMAIL,
        token_type=TOKEN_TYPE_ACCESS,
        expires_delta=timedelta(minutes=settings.jwt_access_token_expire_minutes),
        extra_claims={"role": ROLE_ADMIN, "name": "Admin"},
    )


def _admin_token_for(admin: dict) -> str:
    """Access token that carries the admin's identity, so every action they take
    can be attributed to them in the audit trail."""
    settings = get_settings()
    return create_token(
        subject=str(admin["_id"]),
        token_type=TOKEN_TYPE_ACCESS,
        expires_delta=timedelta(minutes=settings.jwt_access_token_expire_minutes),
        extra_claims={"role": ROLE_ADMIN, "name": admin.get("name"), "email": admin.get("email")},
    )


def _admin_reset_token(admin: dict) -> str:
    settings = get_settings()
    return create_token(
        subject=str(admin["_id"]),
        token_type=TOKEN_TYPE_PASSWORD_RESET,
        expires_delta=timedelta(minutes=settings.jwt_password_reset_expire_minutes),
        extra_claims={"role": ROLE_ADMIN},
    )


def _reset_token(student: dict) -> str:
    settings = get_settings()
    return create_token(
        subject=str(student["_id"]),
        token_type=TOKEN_TYPE_PASSWORD_RESET,
        expires_delta=timedelta(minutes=settings.jwt_password_reset_expire_minutes),
        extra_claims={"role": ROLE_STUDENT},
    )


async def check_student(identifier: str) -> dict:
    student = await find_student_by_identifier(identifier)
    if student is None:
        return {
            "exists": False,
            "password_setup_required": False,
            "force_password_reset": False,
            "message": "Student not found",
        }

    reset_required = bool(student.get("force_password_reset")) or not bool(student.get("is_password_set"))
    return {
        "exists": True,
        "password_setup_required": reset_required,
        "force_password_reset": bool(student.get("force_password_reset")),
        "message": "Student found",
    }


async def login(identifier: str, password: str) -> dict:
    student = await find_student_by_identifier(identifier)
    if student is None or not student.get("password_hash"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if not verify_password(password, student["password_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if student.get("force_password_reset") or not student.get("is_password_set"):
        return {
            "status": AUTH_STATUS_PASSWORD_RESET_REQUIRED,
            "reset_token": _reset_token(student),
            "role": ROLE_STUDENT,
            "message": "Password reset required",
        }

    return {
        "status": AUTH_STATUS_AUTHENTICATED,
        "access_token": _access_token(student),
        "role": ROLE_STUDENT,
        "message": "Login successful",
    }


async def unified_login(identifier: str, password: str) -> dict:
    """Single sign-in entry point. An email routes to an admin account; anything
    else (a mobile number) to a student. The response's `role` tells the client
    where to send the user. Error messages stay generic so nothing leaks."""
    if "@" in (identifier or ""):
        return await admin_login(identifier, password)
    return await login(identifier, password)


async def admin_login(email: str, password: str) -> dict:
    email_norm = email.strip().lower()

    # Legacy shared admin, kept as a fallback so nothing breaks during rollout.
    if email_norm == ADMIN_EMAIL and password == ADMIN_PASSWORD:
        return {
            "status": AUTH_STATUS_AUTHENTICATED,
            "access_token": _admin_access_token(),
            "role": ROLE_ADMIN,
            "message": "Admin login successful",
        }

    # Per-admin accounts (seeded via scripts/seed_admins.py).
    db = get_database()
    admin = await db[ADMINS].find_one({"email": email_norm})
    if admin is None or not admin.get("password_hash") or not verify_password(password, admin["password_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    # First login (or a re-issued temp password): force them to set their own.
    if admin.get("force_password_reset") or not admin.get("is_password_set"):
        return {
            "status": AUTH_STATUS_PASSWORD_RESET_REQUIRED,
            "reset_token": _admin_reset_token(admin),
            "role": ROLE_ADMIN,
            "message": "Set your own password to continue",
        }

    return {
        "status": AUTH_STATUS_AUTHENTICATED,
        "access_token": _admin_token_for(admin),
        "role": ROLE_ADMIN,
        "message": "Admin login successful",
    }


async def set_password(reset_token: str, new_password: str) -> dict:
    try:
        payload = decode_token(reset_token, expected_type=TOKEN_TYPE_PASSWORD_RESET)
        subject_id = to_object_id(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid reset token")

    db = get_database()
    now = datetime.now(timezone.utc)
    fields = {
        "password_hash": hash_password(new_password),
        "is_password_set": True,
        "force_password_reset": False,
        "password_updated_at": now,
        "updated_at": now,
    }
    # The reset token carries the role, so the same endpoint serves admins and
    # students without leaking one into the other's collection.
    if payload.get("role") == ROLE_ADMIN:
        result = await db[ADMINS].update_one({"_id": subject_id}, {"$set": fields})
        if result.matched_count == 0:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin not found")
        return {"message": "Password updated successfully"}

    result = await db[STUDENTS].update_one({"_id": subject_id, "role": ROLE_STUDENT}, {"$set": fields})
    if result.matched_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Student not found")
    return {"message": "Password updated successfully"}


def generate_temp_password(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def admin_reset_password(identifier: str, temp_password: str | None = None) -> dict:
    student = await find_student_by_identifier(identifier)
    if student is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Student not found")

    password = temp_password or generate_temp_password()
    now = datetime.now(timezone.utc)
    db = get_database()
    await db[STUDENTS].update_one(
        {"_id": student["_id"]},
        {
            "$set": {
                "password_hash": hash_password(password),
                "force_password_reset": True,
                "updated_at": now,
                "password_updated_at": now,
            }
        },
    )
    return {
        "message": "Temporary password created. Student must reset password after login.",
        "temp_password": password,
    }
