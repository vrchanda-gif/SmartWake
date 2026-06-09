#neded for hints in code verifier
from __future__ import annotations

#to read env variables from render
import os
from contextlib import asynccontextmanager

#need redirect for google oauth flow
#flow type is from oauth lib that manages login/authentication
#used in creating the entire oauth path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow

#load all variables for google oauth stuff
#need to get verify, get token, then store in database
from database import (
    delete_state,
    init_db,
    load_google_token,
    load_oauth_code_verifier,
    load_oauth_state,
    save_google_token,
    save_oauth_state,
)

#all variables needed for oauth process below
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

#create database table for token values and other env stuff
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

#create api
app = FastAPI(lifespan=lifespan)

#check for if api is running
@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "smartwake_api",
    }

#sanity check for env variables being properly read
def _require_google_env() -> None:
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
            detail="Missing Google OAuth environment variable(s): " + ", ".join(missing),
        )

#found in testing that we need to account for code_verifier
#if we have one use it, if not ask for a PKCE code verifier from google oauth
def _create_oauth_flow(
    *,
    code_verifier: str | None = None,
    autogenerate_code_verifier: bool = False,
) -> Flow:
    _require_google_env()   #check for all necessary env stuff before we try to run oauth flow

    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": GOOGLE_AUTH_URI,
            "token_uri": GOOGLE_TOKEN_URI,
            "redirect_uris": [GOOGLE_REDIRECT_URI],
        }
    }

    #define flow
    #get config variables
    #define health scopes being requested
    #define redirect for after user verified
    #define whether we need to make a code verifier
    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI,
        autogenerate_code_verifier=autogenerate_code_verifier,
    )

    #use saved code verifier during callback
    if code_verifier:
        flow.code_verifier = code_verifier

    return flow

#displays if we have the google oauth tokens, is our success and error screen
#keep simple just display connection status, reason if error and then values if success
@app.get("/auth/status")
def auth_status():
    token = load_google_token()

    if not token:
        return {
            "connected": False,
            "reason": "No Google OAuth token has been saved yet.",
        }

    return {
        "connected": True,
        "has_refresh_token": bool(token.get("refresh_token")),
        "expiry": token.get("expiry"),  #shows when authentication token expires for reference
        "scopes": token.get("scopes") or token.get("scope"),    #format might be scope or scopes so check both, normally scopes
    }

#creates path for initial login and stuff
#start oauth flow then ask google to run authorization 
#gets code verifier then saved to database, state for debug stuff
@app.get("/auth/google")
def auth_google():
    flow = _create_oauth_flow(autogenerate_code_verifier=True)

    #calls to the google for oauth, offline is refresh token and other two overify that we have access
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )

    save_oauth_state(state, flow.code_verifier)
    return RedirectResponse(authorization_url)  #goes to google login for user

#need callback stuff for google project and google health calls
#after sign in we go here to take in data/make requests
#check state for debug and make sure were authenticated
@app.get("/oauth/callback")
def oauth_callback(request: Request):
    error = request.query_params.get("error")

    #got 400 from testing app POST protocol, status code can help narrow down error source
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
            detail="OAuth state mismatch. Restart authorization from /auth/google.",
        )

    code = request.query_params.get("code")

    if not code:
        raise HTTPException(
            status_code=400,
            detail="Missing OAuth authorization code.",
        )

    code_verifier = load_oauth_code_verifier()  #get code verifier then rebuild oauth flow with the new code
    flow = _create_oauth_flow(code_verifier=code_verifier)

    try:
        flow.fetch_token(code=code) #if we have code try to exchange for token, error means were generating code verifier wrong
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to exchange OAuth code for token: {exc}",
        ) from exc

    credentials = flow.credentials  #success means we can load tokens

    #save credentials to database then delete state to clear up database
    save_google_token(credentials.to_json())
    delete_state("oauth_state")
    #returns the expected formay of success to /status W W W W
    return {
        "message": "Google Health authorization complete.",
        "connected": True,
        "has_refresh_token": bool(credentials.refresh_token),
        "expiry": credentials.expiry.isoformat() if credentials.expiry else None,
    }
