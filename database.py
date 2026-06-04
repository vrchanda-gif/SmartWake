from __future__ import annotations

import json
import os
from typing import Any

import psycopg2
from psycopg2.extras import Json, RealDictCursor


def _get_database_url() -> str:
    """
    Get the Render Postgres connection URL from environment variables.
    """
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError("Missing DATABASE_URL environment variable.")

    return database_url


def get_connection():
    """
    Open a connection to the Postgres database.

    RealDictCursor makes returned rows behave like dictionaries:
        row["value"]
    instead of tuple indexing like:
        row[0]
    """
    return psycopg2.connect(
        _get_database_url(),
        cursor_factory=RealDictCursor,
    )


def init_db() -> None:
    """
    Create the SmartWake state table if it does not already exist.

    We use one small key/value table because this service only needs to
    store a few named pieces of state:
        - google_token
        - oauth_state
        - last_worker_status
        - last_posted_event_key
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS smartwake_state (
                    key TEXT PRIMARY KEY,
                    value JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

        conn.commit()


def set_state(key: str, value: dict[str, Any]) -> None:
    """
    Save a JSON-like dictionary under a named key.

    If the key already exists, this updates it.
    If the key does not exist yet, this creates it.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO smartwake_state (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key)
                DO UPDATE SET
                    value = EXCLUDED.value,
                    updated_at = NOW();
                """,
                (key, Json(value)),
            )

        conn.commit()


def get_state(key: str) -> dict[str, Any] | None:
    """
    Load a saved value from the database.

    Returns None if that key has not been saved yet.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT value
                FROM smartwake_state
                WHERE key = %s;
                """,
                (key,),
            )

            row = cur.fetchone()

    if not row:
        return None

    value = row["value"]

    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        return json.loads(value)

    return dict(value)


def delete_state(key: str) -> None:
    """
    Delete one stored key from the database.

    This is mainly used to clean up temporary OAuth state after Google
    authorization finishes.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM smartwake_state
                WHERE key = %s;
                """,
                (key,),
            )

        conn.commit()


def save_google_token(token_json: str | dict[str, Any]) -> None:
    """
    Save Google OAuth credential JSON.

    This is called after /oauth/callback successfully exchanges Google's
    temporary authorization code for real credentials.

    The stored token is later used by the background worker to refresh access
    tokens and fetch Google Health heart-rate samples.
    """
    if isinstance(token_json, str):
        token_data = json.loads(token_json)
    else:
        token_data = token_json

    set_state("google_token", token_data)


def load_google_token() -> dict[str, Any] | None:
    """
    Load the saved Google OAuth credential JSON.

    Used by:
        - /auth/status
        - future smartwake-worker Google Health client
    """
    return get_state("google_token")


def save_oauth_state(state: str, code_verifier: str | None = None) -> None:
    """
    Save temporary OAuth state and PKCE code_verifier.

    The state checks that Google's callback belongs to the flow we started.
    The code_verifier is required if Google/OAuth library uses PKCE.
    """
    data: dict[str, Any] = {"state": state}

    if code_verifier:
        data["code_verifier"] = code_verifier

    set_state("oauth_state", data)


def load_oauth_state() -> str | None:
    """
    Load the temporary OAuth state value.
    """
    data = get_state("oauth_state")

    if not data:
        return None

    state = data.get("state")

    if not isinstance(state, str):
        return None

    return state


def load_oauth_code_verifier() -> str | None:
    """
    Load the temporary PKCE code_verifier value.
    """
    data = get_state("oauth_state")

    if not data:
        return None

    code_verifier = data.get("code_verifier")

    if not isinstance(code_verifier, str):
        return None

    return code_verifier


def save_worker_status(status: dict[str, Any]) -> None:
    """
    Optional helper for later.

    The background worker can use this to save its latest status for debugging:
        - last check time
        - last schedule received
        - sample count
        - decision
        - errors
    """
    set_state("last_worker_status", status)


def load_worker_status() -> dict[str, Any] | None:
    """
    Optional helper for later.

    Lets the API or debugging tools inspect what the worker last did.
    """
    return get_state("last_worker_status")


def save_last_posted_event_key(event_key: str) -> None:
    """
    Optional helper for duplicate prevention.

    The worker can save the alarm cycle/date it already posted for, so it
    does not post the same alarm_time repeatedly.
    """
    set_state("last_posted_event_key", {"event_key": event_key})


def load_last_posted_event_key() -> str | None:
    """
    Optional helper for duplicate prevention.

    Returns the last alarm cycle/date that the worker already posted for.
    """
    data = get_state("last_posted_event_key")

    if not data:
        return None

    event_key = data.get("event_key")

    if not isinstance(event_key, str):
        return None

    return event_key