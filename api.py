from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow

from database import (
    delete_state,
    init_db,
    load_google_token,
    load_oauth_code_verifier,
    load_oauth_state,
    save_google_token,
    save_oauth_state,
)


GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")

GOOGLE_AUTH_URI = os.getenv(
    "GOOGLE_AUTH_URI",
    "https://accounts.google.com/o/oauth2/v2/auth",
)

GOOGLE_TOKEN_URI = os.getenv(
    "GOOGLE_TOKEN_URI",
    "https://oauth2.googleapis.com/token",
)

GOOGLE_HEALTH_SCOPE = os.getenv(
    "GOOGLE_HEALTH_SCOPE",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
)

SCOPES = [GOOGLE_HEALTH_SCOPE]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs once when the Render FastAPI service starts.

    This makes sure the database table exists before OAuth routes try to
    save or load token data.
    """
    init_db()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    """
    Simple Render health check endpoint.
    """
    return {
        "status": "ok",
        "service": "smartwake-api",
    }


def _require_google_env() -> None:
    """
    Makes OAuth errors easier to understand.

    If one of these values is missing in Render's Environment tab,
    /auth/google should fail with a clear message instead of a confusing
    Google OAuth error.
    """
    missing = []

    if not GOOGLE_CLIENT_ID:
        missing.append("GOOGLE_CLIENT_ID")

    if not GOOGLE_CLIENT_SECRET:
        missing.append("GOOGLE_CLIENT_SECRET")

    if not GOOGLE_REDIRECT_URI:
        missing.append("GOOGLE_REDIRECT_URI")

    if missing:
        raise HTTPException(
            status_code=500,
            detail=(
                "Missing Google OAuth environment variable(s): "
                + ", ".join(missing)
            ),
        )


def _create_oauth_flow(
    *,
    code_verifier: str | None = None,
    autogenerate_code_verifier: bool = False,
) -> Flow:
    """
    Creates the Google OAuth web-server flow.

    If PKCE is used, the code_verifier generated during /auth/google must be
    restored during /oauth/callback before exchanging the code for tokens.
    """
    _require_google_env()

    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": GOOGLE_AUTH_URI,
            "token_uri": GOOGLE_TOKEN_URI,
            "redirect_uris": [GOOGLE_REDIRECT_URI],
        }
    }

    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI,
        autogenerate_code_verifier=autogenerate_code_verifier,
    )

    if code_verifier:
        flow.code_verifier = code_verifier

    return flow


@app.get("/auth/status")
def auth_status():
    """
    Shows whether Google OAuth has been completed.

    Important: this does not return the actual token or refresh token.
    It only returns safe status information.
    """
    token = load_google_token()

    if not token:
        return {
            "connected": False,
            "reason": "No Google OAuth token has been saved yet.",
        }

    return {
        "connected": True,
        "has_refresh_token": bool(token.get("refresh_token")),
        "expiry": token.get("expiry"),
        "scopes": token.get("scopes") or token.get("scope"),
    }


@app.get("/auth/google")
def auth_google():
    """
    Starts the one-time Google OAuth setup.

    You visit this route in your browser once. It redirects you to Google.
    After you approve access, Google redirects back to /oauth/callback.
    """
    flow = _create_oauth_flow(autogenerate_code_verifier=True)

    authorization_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )

    save_oauth_state(state, flow.code_verifier)

    return RedirectResponse(authorization_url)


@app.get("/oauth/callback")
def oauth_callback(request: Request):
    """
    Handles Google's redirect after consent.

    Google sends back a temporary code. This route exchanges that code for
    token JSON, then stores the token JSON in Postgres through database.py.
    """
    error = request.query_params.get("error")

    if error:
        raise HTTPException(
            status_code=400,
            detail=f"Google OAuth failed: {error}",
        )

    returned_state = request.query_params.get("state")
    expected_state = load_oauth_state()

    if not expected_state or returned_state != expected_state:
        raise HTTPException(
            status_code=400,
            detail=(
                "OAuth state mismatch. "
                "Restart authorization from /auth/google."
            ),
        )

    code = request.query_params.get("code")

    if not code:
        raise HTTPException(
            status_code=400,
            detail="Missing OAuth authorization code.",
        )

    code_verifier = load_oauth_code_verifier()

    flow = _create_oauth_flow(code_verifier=code_verifier)

    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to exchange OAuth code for token: {exc}",
        ) from exc

    credentials = flow.credentials

    save_google_token(credentials.to_json())
    delete_state("oauth_state")

    return {
        "message": "Google Health authorization complete.",
        "connected": True,
        "has_refresh_token": bool(credentials.refresh_token),
        "expiry": credentials.expiry.isoformat() if credentials.expiry else None,
    }