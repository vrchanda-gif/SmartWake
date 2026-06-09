from __future__ import annotations

import json
import os

#used for hint types, similar type of import in api
from typing import Any

#use postgres for managing database stuff
#neeed import for storing in JSONB
#use dict cursor so i can see values in database easier
#stored like key    value   time saved for each row
import psycopg2
from psycopg2.extras import Json, RealDictCursor

#get url from render that postgres import helped generate
def _get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError("Missing DATABASE_URL environment variable.")#simple sanity check

    return database_url

#creates connection to render postgres
#cursor stuff is for format later can do row["value"]
def get_connection():
    return psycopg2.connect(
        _get_database_url(),
        cursor_factory=RealDictCursor,
    )


#open database connection
#create cursor for SQL to navigate postgres stuff
#commit after, is centered around connecitng to the data base only when we need to
def init_db() -> None:
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

#formats what wer saving into postgres
#dictionary under specified key
#insert into key a specified value
#use Json() function for format so we can have expected values
def set_state(key: str, value: dict[str, Any]) -> None:
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

#loads a saved value from postgres using key
#key is string like formatted above
#check just key and return the row
def get_state(key: str) -> dict[str, Any] | None:
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

    #get value, if its JSON string instead of JSONB then we use json.loads
    #
    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        return json.loads(value)

    return dict(value)

#deletes rwos from table, code verifier is temporary so we need to delete
def delete_state(key: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM smartwake_state
                WHERE key = %s;
                """,
                (key,), #remove a row associated with a key
            )

        conn.commit()

#if we have a token thatsa a json string then do json.loads
#just in case if its normal just take in that value
def save_google_token(token_json: str | dict[str, Any]) -> None:
    if isinstance(token_json, str):
        token_data = json.loads(token_json)
    else:
        token_data = token_json

    set_state("google_token", token_data)

#use previous get to return google token for use
def load_google_token() -> dict[str, Any] | None:
    return get_state("google_token")

#need to save state stuff bc code verifier is exchanged for token when going through oauth path
#used to check were on the right oauth path
def save_oauth_state(state: str, code_verifier: str | None = None) -> None:
    data: dict[str, Any] = {"state": state}
    #load code verifier if we have one
    if code_verifier:
        data["code_verifier"] = code_verifier

    set_state("oauth_state", data)#saved as oauth state

#loads everything save in oauth state to check were in the right path during callback
def load_oauth_state() -> str | None:
    data = get_state("oauth_state")

    if not data:
        return None

    state = data.get("state")
    return state if isinstance(state, str) else None

#loads code verifier for exchange for tokens
def load_oauth_code_verifier() -> str | None:
    data = get_state("oauth_state")

    if not data:
        return None

    code_verifier = data.get("code_verifier")
    return code_verifier if isinstance(code_verifier, str) else None


#used for api debugging stuff
#returns status of background worker to see if were on
def save_worker_status(status: dict[str, Any]) -> None:
    set_state("last_worker_status", status)

#used to shut down system, save time stamp of last post request then only restart logic and stuff when we get a later POST
def save_last_posted_event_key(event_key: str) -> None:
    set_state("last_posted_event_key", {"event_key": event_key})

#load last posted alarm time so we can check if we need to restart flow
def load_last_posted_event_key() -> str | None:
    data = get_state("last_posted_event_key")

    if not data:
        return None

    event_key = data.get("event_key")
    return event_key if isinstance(event_key, str) else None

#if were done then save thatbut also if we didnt need to POST still end
#two keys, saved and unsaved post times
def save_completed_event_key(event_key: str) -> None:

    set_state("completed_event_key", {"event_key": event_key})

#check for main to see if we need to start or stay dormant
def load_completed_event_key() -> str | None:
    data = get_state("completed_event_key")

    if not data:
        return None

    event_key = data.get("event_key")
    return event_key if isinstance(event_key, str) else None
