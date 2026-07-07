from fastapi import APIRouter, Depends

from app.schemas.auth import (
    AdminLoginRequest,
    AdminLoginResponse,
    AdminResetPasswordRequest,
    AdminResetPasswordResponse,
    LoginRequest,
    LoginResponse,
    SetPasswordRequest,
    SetPasswordResponse,
    StudentCheckRequest,
    StudentCheckResponse,
)
from app.services import auth_service
from app.utils.dependencies import require_admin_sync_token


router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post("/check-student", response_model=StudentCheckResponse)
async def check_student(payload: StudentCheckRequest) -> dict:
    return await auth_service.check_student(payload.identifier)


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest) -> dict:
    return await auth_service.login(payload.identifier, payload.password)


@router.post("/admin-login", response_model=AdminLoginResponse)
async def admin_login(payload: AdminLoginRequest) -> dict:
    return await auth_service.admin_login(payload.email, payload.password)


@router.post("/set-password", response_model=SetPasswordResponse)
async def set_password(payload: SetPasswordRequest) -> dict:
    return await auth_service.set_password(payload.reset_token, payload.new_password)


@router.post(
    "/admin-reset-password",
    response_model=AdminResetPasswordResponse,
    dependencies=[Depends(require_admin_sync_token)],
)
async def admin_reset_password(payload: AdminResetPasswordRequest) -> dict:
    return await auth_service.admin_reset_password(payload.identifier, payload.temp_password)
