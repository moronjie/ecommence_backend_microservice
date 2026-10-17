from ecom_common import success_response
from fastapi import APIRouter

from app.core.dependencies import AuthServiceDep, CurrentUserId
from app.schemas.auth import LoginRequest, RefreshRequest, RegisterRequest, UserOut

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/register")
async def register(body: RegisterRequest, service: AuthServiceDep):
    user = await service.register(body)
    return success_response(UserOut.model_validate(user), "Account created", 201)


@router.post("/login")
async def login(body: LoginRequest, service: AuthServiceDep):
    tokens = await service.login(body.email, body.password)
    return success_response(tokens, "Login successful")


@router.post("/refresh")
async def refresh(body: RefreshRequest, service: AuthServiceDep):
    tokens = await service.refresh(body.refresh_token)
    return success_response(tokens, "Token refreshed")


@router.post("/logout")
async def logout(body: RefreshRequest, service: AuthServiceDep, user_id: CurrentUserId):
    await service.logout(body.refresh_token, user_id)
    return success_response(message="Logged out")


@router.get("/me")
async def me(service: AuthServiceDep, user_id: CurrentUserId):
    user = await service.get_user(user_id)
    return success_response(UserOut.model_validate(user), "Current user")
