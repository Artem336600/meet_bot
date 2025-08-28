from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OAuthToken, User
from app.db.session import get_async_session


router = APIRouter(prefix="/oauth/google", tags=["oauth"])


def _get_client_config() -> dict:
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET are not set")
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [os.getenv("GOOGLE_OAUTH_REDIRECT", "http://localhost:8000/oauth/google/callback")],
        }
    }


def _scopes() -> list[str]:
    return [
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    ]


@router.get("/start")
async def oauth_start(request: Request, tg_id: str | None = None) -> RedirectResponse:
    flow = Flow.from_client_config(_get_client_config(), scopes=_scopes())
    flow.redirect_uri = os.getenv("GOOGLE_OAUTH_REDIRECT", "http://localhost:8000/oauth/google/callback")

    # Сформируем один параметр state, содержащий tg_id (без добавления второго state в URL)
    custom_state = None
    if tg_id:
        import secrets

        nonce = secrets.token_urlsafe(8)
        custom_state = f"{nonce}:{tg_id}"

    authorization_url, _state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=custom_state,
    )
    return RedirectResponse(authorization_url)


async def _get_or_create_user(session: AsyncSession, tg_id: Optional[int] = None) -> User:
    if tg_id is not None:
        result = await session.execute(select(User).where(User.tg_id == tg_id))
        user = result.scalar_one_or_none()
        if user:
            return user
    user = User(tg_id=tg_id)
    session.add(user)
    await session.flush()
    return user


@router.get("/callback")
async def oauth_callback(code: str, state: Optional[str] = None, session: AsyncSession = Depends(get_async_session)):
    flow = Flow.from_client_config(_get_client_config(), scopes=_scopes())
    flow.redirect_uri = os.getenv("GOOGLE_OAUTH_REDIRECT", "http://localhost:8000/oauth/google/callback")

    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Failed to fetch token: {exc}")

    creds: Credentials = flow.credentials
    # Имитация текущего пользователя: tg_id=None (в реальном боте связать по auth-сессии)
    # если в state есть tg_id — свяжем
    tg_id: Optional[int] = None
    if state and ":" in state:
        try:
            _, tg_str = state.split(":", 1)
            tg_id = int(tg_str)
        except Exception:
            tg_id = None
    user = await _get_or_create_user(session, tg_id=tg_id)

    expires_at: Optional[datetime] = None
    if creds.expiry:
        # creds.expiry уже timezone-aware
        expires_at = creds.expiry
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

    token = OAuthToken(
        user_id=user.id,
        provider="google",
        access_token=creds.token,
        refresh_token=creds.refresh_token,
        expires_at=expires_at,
    )
    # upsert по (user_id, provider)
    existing = await session.execute(
        select(OAuthToken).where(OAuthToken.user_id == user.id, OAuthToken.provider == "google")
    )
    existing = existing.scalar_one_or_none()
    if existing:
        existing.access_token = token.access_token
        existing.refresh_token = token.refresh_token or existing.refresh_token
        existing.expires_at = token.expires_at
    else:
        session.add(token)

    await session.commit()

    return {"status": "ok", "user_id": user.id, "has_refresh_token": bool(token.refresh_token)}


