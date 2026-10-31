"""TalkTogether call signalling server.

This server authenticates each WebSocket with a Supabase access token,
checks the friendship using the caller's RLS permissions, and issues
short-lived LiveKit tokens only after the recipient accepts a call.
"""

import asyncio
import os
import uuid
from datetime import timedelta
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from livekit import api

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_PUBLISHABLE_KEY = os.getenv("SUPABASE_PUBLISHABLE_KEY", "").strip()
LIVEKIT_URL = os.getenv("LIVEKIT_URL", "").strip()
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "").strip()
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "").strip()

app = FastAPI(title="TalkTogether Server", version="0.6.0-beta.1")

# In-memory presence and calls are suitable for one Uvicorn worker only.
# Multi-process or multi-server deployment requires a shared broker such as Redis.
online_users: dict[str, WebSocket] = {}
calls: dict[str, dict[str, Any]] = {}
user_calls: dict[str, str] = {}


async def supabase_headers(access_token: str) -> dict[str, str]:
    return {
        "apikey": SUPABASE_PUBLISHABLE_KEY,
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }


async def verify_access_token(access_token: str) -> dict[str, Any] | None:
    """Validate the token with Supabase Auth, not by trusting its contents."""
    if not SUPABASE_URL or not SUPABASE_PUBLISHABLE_KEY or not access_token:
        return None

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{SUPABASE_URL}/auth/v1/user",
                headers=await supabase_headers(access_token),
            )
        if response.status_code != 200:
            return None
        user = response.json()
        if not user.get("id"):
            return None
        return user
    except (httpx.HTTPError, ValueError):
        return None


async def get_profile_name(user_id: str, access_token: str) -> str:
    """Read a display name under the authenticated user's RLS context."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/profiles",
                params={
                    "select": "username,display_name",
                    "user_id": f"eq.{user_id}",
                    "limit": "1",
                },
                headers=await supabase_headers(access_token),
            )
        if response.status_code == 200:
            rows = response.json()
            if rows:
                return str(rows[0].get("display_name") or rows[0].get("username") or "Uporabnik")[:80]
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    return "Uporabnik"


async def are_friends(user_id: str, other_id: str, access_token: str) -> bool:
    """Check a friendship using the caller's own authenticated RLS context."""
    try:
        expression = (
            f"(and(user_a.eq.{user_id},user_b.eq.{other_id}),"
            f"and(user_a.eq.{other_id},user_b.eq.{user_id}))"
        )
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{SUPABASE_URL}/rest/v1/friendships",
                params={"select": "user_a,user_b", "or": expression, "limit": "1"},
                headers=await supabase_headers(access_token),
            )
        if response.status_code != 200:
            return False
        rows = response.json()
        return bool(rows)
    except (httpx.HTTPError, ValueError, TypeError):
        return False


def create_livekit_token(user_id: str, display_name: str, room_name: str) -> str:
    if not all((LIVEKIT_API_KEY, LIVEKIT_API_SECRET, LIVEKIT_URL)):
        raise RuntimeError("LiveKit nastavitve manjkajo na strežniku.")

    return (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(str(user_id))
        .with_name((display_name or "Uporabnik")[:80])
        .with_ttl(timedelta(minutes=60))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
            )
        )
        .to_jwt()
    )


async def send_user(user_id: str, payload: dict[str, Any]) -> bool:
    socket = online_users.get(user_id)
    if socket is None:
        return False
    try:
        await socket.send_json(payload)
        return True
    except Exception:
        return False


async def release_call(call_id: str, event: dict[str, Any] | None = None) -> None:
    call = calls.pop(call_id, None)
    if not call:
        return
    caller = call["caller_id"]
    receiver = call["receiver_id"]
    if user_calls.get(caller) == call_id:
        user_calls.pop(caller, None)
    if user_calls.get(receiver) == call_id:
        user_calls.pop(receiver, None)
    if event:
        await send_user(caller, event)
        await send_user(receiver, event)


async def expire_unanswered_call(call_id: str) -> None:
    await asyncio.sleep(45)
    call = calls.get(call_id)
    if call and call.get("state") == "ringing":
        await release_call(
            call_id,
            {"type": "call_timeout", "call_id": call_id,
                "message": "Klica nihče ni sprejel."},
        )


async def handle_message(user_id: str, access_token: str, message: dict[str, Any]) -> None:
    action = str(message.get("type", ""))

    # Application heartbeat for active WebSocket sessions.
    if action == "ping":
        await send_user(user_id, {"type": "pong"})
        return

    if action == "call":
        receiver_id = str(message.get("to_user_id", "")).strip()
        try:
            uuid.UUID(receiver_id)
        except (ValueError, AttributeError):
            await send_user(user_id, {"type": "error", "message": "Neveljaven uporabnik."})
            return

        if receiver_id == user_id:
            await send_user(user_id, {"type": "error", "message": "Samega sebe ne moreš poklicati."})
            return

        if user_id in user_calls or receiver_id in user_calls:
            await send_user(user_id, {"type": "error", "message": "Ti ali prijatelj sta že v drugem klicu."})
            return

        if receiver_id not in online_users:
            await send_user(user_id, {"type": "call_unavailable", "message": "Prijatelj trenutno ni povezan."})
            return

        if not await are_friends(user_id, receiver_id, access_token):
            await send_user(user_id, {"type": "error", "message": "Klic je dovoljen samo med potrjenima prijateljema."})
            return

        # Re-check busy/online state after the asynchronous database request.
        if user_id in user_calls or receiver_id in user_calls or receiver_id not in online_users:
            await send_user(user_id, {"type": "call_unavailable", "message": "Prijatelj trenutno ni na voljo."})
            return

        call_id = str(uuid.uuid4())
        room_name = f"talktogether-{uuid.uuid4().hex}"
        caller_name = await get_profile_name(user_id, access_token)
        call = {
            "call_id": call_id,
            "caller_id": user_id,
            "receiver_id": receiver_id,
            "caller_name": caller_name,
            "room_name": room_name,
            "state": "ringing",
        }
        calls[call_id] = call
        user_calls[user_id] = call_id
        user_calls[receiver_id] = call_id

        await send_user(user_id, {"type": "outgoing_ringing", "call_id": call_id})
        delivered = await send_user(
            receiver_id,
            {"type": "incoming_call", "call_id": call_id, "caller_name": caller_name},
        )
        if not delivered:
            await release_call(call_id)
            await send_user(user_id, {"type": "call_unavailable", "message": "Prijatelj je izgubil povezavo."})
            return
        asyncio.create_task(expire_unanswered_call(call_id))
        return

    call_id = str(message.get("call_id", "")).strip()
    call = calls.get(call_id)
    if not call:
        await send_user(user_id, {"type": "error", "message": "Klic ne obstaja več."})
        return

    caller_id = call["caller_id"]
    receiver_id = call["receiver_id"]
    if user_id not in (caller_id, receiver_id):
        await send_user(user_id, {"type": "error", "message": "Za ta klic nimaš dovoljenja."})
        return

    if action == "accept":
        if user_id != receiver_id or call.get("state") != "ringing":
            await send_user(user_id, {"type": "error", "message": "Tega klica ne moreš sprejeti."})
            return
        if caller_id not in online_users:
            await release_call(call_id, {"type": "call_ended", "call_id": call_id,
                                         "message": "Klicatelj ni več povezan."})
            return

        call["state"] = "active"
        room = call["room_name"]
        caller_name = call["caller_name"]
        receiver_name = await get_profile_name(receiver_id, access_token)
        try:
            caller_token = create_livekit_token(caller_id, caller_name, room)
            receiver_token = create_livekit_token(receiver_id, receiver_name, room)
        except Exception:
            await release_call(call_id, {"type": "call_error", "call_id": call_id,
                                         "message": "Strežnik ni mogel pripraviti zvočnega klica."})
            return

        common = {
            "type": "call_accepted",
            "call_id": call_id,
            "server_url": LIVEKIT_URL,
            "room_name": room,
        }
        caller_delivered = await send_user(
            caller_id,
            {**common, "token": caller_token, "other_name": receiver_name},
        )
        receiver_delivered = await send_user(
            receiver_id,
            {**common, "token": receiver_token, "other_name": caller_name},
        )
        if not caller_delivered or not receiver_delivered:
            await release_call(
                call_id,
                {"type": "call_ended", "call_id": call_id,
                 "message": "Povezava enega od udeležencev je bila prekinjena."},
            )
        return

    if action == "reject":
        if user_id != receiver_id or call.get("state") != "ringing":
            await send_user(user_id, {"type": "error", "message": "Tega klica ne moreš zavrniti."})
            return
        await release_call(call_id, {"type": "call_rejected", "call_id": call_id,
                                     "message": "Klic je bil zavrnjen."})
        return

    if action in ("hangup", "cancel"):
        await release_call(call_id, {"type": "call_ended", "call_id": call_id,
                                     "message": "Klic je končan."})
        return

    await send_user(user_id, {"type": "error", "message": "Nepoznana zahteva."})


@app.get("/")
async def home():
    return {"app": "TalkTogether Server", "version": "0.6.0-beta.1", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.6.0-beta.1"}


@app.get("/version")
async def version():
    return {"app": "TalkTogether Server", "version": "0.6.0-beta.1"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    authorization = websocket.headers.get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        await websocket.close(code=4401, reason="Authentication required")
        return

    access_token = authorization[7:].strip()
    user = await verify_access_token(access_token)
    if not user:
        await websocket.close(code=4401, reason="Invalid or expired session")
        return

    user_id = str(user["id"])
    await websocket.accept()
    online_users[user_id] = websocket
    await websocket.send_json({"type": "connected", "user_id": user_id})

    try:
        while True:
            incoming = await websocket.receive_json()
            if not isinstance(incoming, dict):
                await websocket.send_json({"type": "error", "message": "Neveljavno sporočilo."})
                continue
            await handle_message(user_id, access_token, incoming)
    except WebSocketDisconnect:
        pass
    except Exception:
        # Avoid returning internal exception details to clients.
        pass
    finally:
        was_current_connection = online_users.get(user_id) is websocket
        if was_current_connection:
            online_users.pop(user_id, None)
        active_call_id = user_calls.get(user_id) if was_current_connection else None
        if active_call_id:
            await release_call(
                active_call_id,
                {"type": "call_ended", "call_id": active_call_id,
                 "message": "Povezava drugega udeleženca je bila prekinjena."},
            )
