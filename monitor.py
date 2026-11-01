import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any

from telethon import TelegramClient, errors, events, functions, types

import database

logging.basicConfig(
    format="[%(levelname)s %(asctime)s] %(message)s",
    level=logging.INFO,
)

API_ID = int(os.getenv("TG_API_ID", "0") or 0)
API_HASH = os.getenv("TG_API_HASH", "")
SESSION_NAME = os.getenv("TG_SESSION_NAME", "venator_listener")
BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TARGET_CHAT_ID = int(os.getenv("TG_TARGET_CHAT_ID", "0") or 0)

SESSION_PATH = str(database.BASE_DIR / SESSION_NAME)

# In-memory runtime state for Zero-Drain status reporting in the Dashboard
_runtime_state: dict[str, Any] = {
    "status": "STOPPED",  # STOPPED | STARTING | AUTH_REQUIRED | QR_READY | 2FA_REQUIRED | ONLINE | BANNED | ERROR
    "account_username": None,
    "account_id": None,
    "active_folder": "Фриланс",
    "available_folders": [],
    "monitored_chats": 0,
    "monitored_chat_names": [],
    "duplicates_skipped": 0,
    "regex_skipped": 0,
    "last_event_at": None,
    "error_message": None,
    "qr_url": None,
}
_state_lock = threading.Lock()
_monitor_thread: threading.Thread | None = None
_monitor_loop: asyncio.AbstractEventLoop | None = None
_client_ref: TelegramClient | None = None
_auth_completed_event: asyncio.Event | None = None

# Ring buffer of SHA-256 message hashes to avoid duplicate LLM calls on cross-posts
_seen_hashes: deque[str] = deque(maxlen=300)
_known_chats: dict[int, str] = {}
_folder_chat_ids: set[int] = set()
_alerted_bans: set[int] = set()

# Cooldown tracking for error alerts to prevent spamming Telegram on repeated errors
_last_error_alerts: dict[str, float] = {}


def send_error_alert(title: str, details: str, cooldown_seconds: int = 180) -> None:
    """Send an immediate error alert to Telegram with anti-flood debounce."""
    now = time.time()
    err_key = f"{title}:{details[:80]}"
    last_sent = _last_error_alerts.get(err_key, 0)

    if now - last_sent < cooldown_seconds:
        return  # Suppress duplicate alert within cooldown window
    _last_error_alerts[err_key] = now

    alert_text = (
        f"🚨 <b>VENATOR ERROR ALERT</b>\n"
        f"⚠️ <b>Type:</b> {title}\n"
        f"📝 <b>Details:</b> <code>{details[:500]}</code>\n"
        f"⏰ <b>Time:</b> {time.strftime('%H:%M:%S UTC', time.gmtime())}"
    )
    send_bot_notification(alert_text)


def _update_state(**kwargs: Any) -> None:
    with _state_lock:
        _runtime_state.update(kwargs)


def get_monitor_status() -> dict[str, Any]:
    """Return a thread-safe snapshot of the Telethon listener state."""
    with _state_lock:
        return dict(_runtime_state)


def compute_post_hash(text: str) -> str:
    """Compute a normalized SHA-256 fingerprint of a post to detect cross-posting."""
    normalized = re.sub(r"https?://\S+", "", text.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()[:400]
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def send_bot_notification(text: str) -> bool:
    """Send HTML notification via official Telegram Bot API using standard library."""
    if not BOT_TOKEN or not TARGET_CHAT_ID:
        logging.warning("TG_BOT_TOKEN or TG_TARGET_CHAT_ID not configured.")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TARGET_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            response.read()
        return True
    except Exception as exc:
        logging.error(f"Bot API notification failed: {exc}")
        return False


def _send_llm_request(
    api_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[dict[str, Any] | None, str | None, int | None]:
    """Execute raw HTTP request to OpenAI-compatible LLM endpoint."""
    body: dict[str, Any] = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    # Enable JSON mode on providers that support it natively
    if any(p in api_url for p in ("groq.com", "openai.com", "openrouter.ai")):
        body["response_format"] = {"type": "json_object"}

    payload = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Venator-Monitor/1.0",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(api_url, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            raw_resp = response.read().decode("utf-8")
            data = json.loads(raw_resp)
            content = data["choices"][0]["message"]["content"].strip()

            if content.startswith("```"):
                content = re.sub(
                    r"^```(?:json)?|```$", "", content, flags=re.MULTILINE
                ).strip()

            # Extract JSON object even if model added leading/trailing commentary
            json_match = re.search(r"\{.*\}", content, flags=re.DOTALL)
            if json_match:
                content = json_match.group(0)

            parsed = json.loads(content)
            parsed["_model_used"] = model
            return parsed, None, 200
    except urllib.error.HTTPError as http_err:
        err_body = http_err.read().decode("utf-8", errors="replace")[:400]
        logging.error(f"LLM HTTP {http_err.code}: {err_body}")
        return None, f"HTTP {http_err.code}: {err_body}", http_err.code
    except Exception as exc:
        logging.error(f"LLM evaluation error: {exc}")
        return None, str(exc), None


def analyze_post_with_llm(
    post_text: str,
    custom_settings: dict[str, Any] | None = None,
    allow_failover: bool = True,
) -> dict[str, Any] | None:
    """
    Evaluate a Telegram post using the configured OpenAI-compatible LLM.
    Supports automatic cascade failover across up to 3 API keys upon daily quota/rate limit exhaustion.
    """
    cfg = custom_settings or database.get_settings()
    api_url = cfg.get("llm_api_url", "").strip()
    model = cfg.get("llm_model", "openai/gpt-oss-120b").strip()
    system_prompt = cfg.get("system_prompt", database.DEFAULT_SYSTEM_PROMPT)

    if not api_url:
        logging.error("LLM_API_URL is empty.")
        return {"_error": "LLM_API_URL is empty"}

    user_prompt = (
        f"Telegram Post to evaluate:\n"
        f"\"\"\"\n{post_text[:3000]}\n\"\"\"\n\n"
        f"Evaluate this post against the developer profile and anti-scam rules.\n"
        f"You MUST output strictly a JSON object with this exact schema:\n"
        f"{{\n"
        f'  "approved": true or false,\n'
        f'  "match_score": integer from 0 to 100,\n'
        f'  "is_scam": true or false,\n'
        f'  "summary": "1 short sentence describing the task and budget",\n'
        f'  "reason": "1 short sentence explaining why it matches or why rejected",\n'
        f'  "recommended_pitch": "A or B or C"\n'
        f"}}"
    )

    # If testing via explicit custom_settings without failover (e.g. settings verification ping)
    if custom_settings is not None and not allow_failover:
        api_key = custom_settings.get("llm_api_key", "").strip()
        parsed, err, _ = _send_llm_request(api_url, api_key, model, system_prompt, user_prompt)
        if parsed is not None:
            return parsed
        return {"_error": err or "Unknown LLM error"}

    # Automatic cascade failover across up to 3 configured keys
    while True:
        api_key, key_idx = database.get_current_active_key()
        if not api_key:
            return {"_error": "No active LLM API key configured"}

        parsed, err_msg, status_code = _send_llm_request(
            api_url, api_key, model, system_prompt, user_prompt
        )

        if parsed is not None:
            parsed["_key_index"] = key_idx
            return parsed

        err_str = str(err_msg or "")
        err_lower = err_str.lower()

        # Check rate limit (HTTP 429) and auth (401/403)
        is_429 = (status_code == 429) or ("429" in err_str) or ("rate_limit" in err_lower)
        is_auth_error = status_code in (401, 403)

        # Detect daily quota exhaustion
        is_daily_quota = is_auth_error or any(
            marker in err_lower
            for marker in (
                "tokens per day",
                "requests per day",
                "tpd",
                "rpd",
                "per day",
                "daily",
                "insufficient_quota",
                "quota_exceeded",
                "exceeded your current quota",
                "billing",
            )
        )

        # Handle short TPM bursts (e.g. "try again in 10s") before switching keys
        if is_429 and not is_daily_quota:
            m = re.search(r"try again in ([0-9.]+)\s*s", err_str, re.IGNORECASE)
            wait_s = float(m.group(1)) if m else 0.0
            if 0 < wait_s <= 12.0:
                logging.info(
                    f"⏳ TPM burst limit on Key #{key_idx + 1}. Waiting {wait_s:.1f}s before retry..."
                )
                time.sleep(wait_s + 0.5)
                # Retry once with current key
                parsed, err_msg, status_code = _send_llm_request(
                    api_url, api_key, model, system_prompt, user_prompt
                )
                if parsed is not None:
                    parsed["_key_index"] = key_idx
                    return parsed
                err_str = str(err_msg or "")
                err_lower = err_str.lower()

        # If key quota or limits are exhausted, failover to the next key
        if is_429 or is_auth_error or is_daily_quota:
            logging.warning(
                f"⚠️ Key #{key_idx + 1} quota/limit exhausted ({err_str}). Switching to next key..."
            )
            next_key, next_idx = database.failover_to_next_key(key_idx, reason=err_str)
            if next_key is not None and next_idx is not None:
                send_bot_notification(
                    f"🔄 <b>Venator: Ротация API-ключей</b>\n"
                    f"⚠️ <b>Ключ #{key_idx + 1}</b> исчерпал лимит/квоту.\n"
                    f"➡️ Автоматически переключено на <b>Ключ #{next_idx + 1}</b> (из 3).\n"
                    f"Пост отправлен на повторный анализ."
                )
                logging.info(
                    f"🔄 Switched from Key #{key_idx + 1} to Key #{next_idx + 1}. Retrying post..."
                )
                continue

            # All configured keys (up to 3) are exhausted
            send_error_alert(
                "All AI Keys Quota Exhausted",
                f"Все доступные API-ключи (до 3) исчерпали квоту. Мониторинг приостановлен до сброса суточных лимитов (00:00 UTC) или добавления новых ключей. Ошибка: {err_str}",
                cooldown_seconds=300,
            )
            return {"_error": f"All AI API keys exhausted. Last error: {err_str}"}

        # Any non-quota error
        return {"_error": err_str}


async def _wait_for_qr_scan(qr_login: Any) -> None:
    """Wait asynchronously for the user to scan the Telegram QR code in the Dashboard."""
    global _auth_completed_event
    try:
        await qr_login.wait(timeout=120)
        _update_state(qr_url=None, error_message=None)
        if _auth_completed_event:
            _auth_completed_event.set()
    except errors.SessionPasswordNeededError:
        _update_state(
            status="2FA_REQUIRED",
            qr_url=None,
            error_message="Two-Step Verification (2FA) is enabled. Enter your cloud password below.",
        )
    except asyncio.TimeoutError:
        _update_state(
            status="AUTH_REQUIRED",
            qr_url=None,
            error_message="QR code expired. Click 'Generate QR Code' to try again.",
        )
    except Exception as exc:
        _update_state(
            status="AUTH_REQUIRED",
            qr_url=None,
            error_message=f"QR login failed: {exc}",
        )


async def _start_qr_coroutine() -> dict[str, Any]:
    if not _client_ref:
        return {"ok": False, "error": "Telethon client is not initialized yet."}
    if await _client_ref.is_user_authorized():
        return {"ok": True, "already_authorized": True}

    qr_login = await _client_ref.qr_login()
    _update_state(
        status="QR_READY",
        qr_url=qr_login.url,
        error_message="Scan this QR code from Telegram: Settings -> Devices -> Link Desktop Device",
    )
    asyncio.create_task(_wait_for_qr_scan(qr_login))
    return {"ok": True, "qr_url": qr_login.url}


async def _submit_2fa_coroutine(password: str) -> dict[str, Any]:
    global _auth_completed_event
    if not _client_ref:
        return {"ok": False, "error": "Telethon client not initialized."}
    try:
        await _client_ref.sign_in(password=password)
        _update_state(qr_url=None, error_message=None)
        if _auth_completed_event:
            _auth_completed_event.set()
        return {"ok": True}
    except Exception as exc:
        _update_state(error_message=f"Invalid 2FA password: {exc}")
        return {"ok": False, "error": str(exc)}


def request_qr_login() -> dict[str, Any]:
    """Trigger QR code generation from a Flask HTTP thread."""
    if not _monitor_loop:
        return {"ok": False, "error": "Monitor event loop is not running."}
    future = asyncio.run_coroutine_threadsafe(_start_qr_coroutine(), _monitor_loop)
    try:
        return future.result(timeout=15)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def submit_2fa_password(password: str) -> dict[str, Any]:
    """Submit Telegram 2FA cloud password from a Flask HTTP thread."""
    if not _monitor_loop:
        return {"ok": False, "error": "Monitor event loop is not running."}
    future = asyncio.run_coroutine_threadsafe(
        _submit_2fa_coroutine(password), _monitor_loop
    )
    try:
        return future.result(timeout=15)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

def _extract_peer_id(peer: Any) -> int | None:
    pid = (
        getattr(peer, "channel_id", None)
        or getattr(peer, "chat_id", None)
        or getattr(peer, "user_id", None)
    )
    return int(pid) if pid else None


async def _sync_folder_chats(client: TelegramClient) -> dict[str, Any]:
    """
    Sync chats strictly from the configured Telegram Folder (or Shared Folder Invite Link).
    Automatically joins any newly added channels if a t.me/addlist/... link is configured.
    """
    cfg = database.get_settings()
    target_folder = (cfg.get("target_folder") or "Фриланс").strip()
    invite_link = (cfg.get("folder_invite_link") or "").strip()

    matched_filter_id: int | None = None
    folder_ids: set[int] = set()

    # 1. If a shared folder invite link (t.me/addlist/slug) is set, auto-join/sync it
    if "addlist/" in invite_link:
        slug = invite_link.split("addlist/")[-1].split("?")[0].strip("/")
        if slug:
            try:
                invite = await client(
                    functions.chatlists.CheckChatlistInviteRequest(slug=slug)
                )
                if isinstance(invite, types.chatlists.ChatlistInvite):
                    await client(
                        functions.chatlists.JoinChatlistInviteRequest(
                            slug=slug, peers=invite.peers
                        )
                    )
                    for p in invite.peers:
                        pid = _extract_peer_id(p)
                        if pid:
                            folder_ids.add(pid)
                elif isinstance(invite, types.chatlists.ChatlistInviteAlready):
                    matched_filter_id = getattr(invite, "filter_id", None)
                    missing = list(getattr(invite, "missing_peers", []))
                    if missing:
                        await client(
                            functions.chatlists.JoinChatlistInviteRequest(
                                slug=slug, peers=missing
                            )
                        )
                    for p in list(getattr(invite, "already_peers", [])) + missing:
                        pid = _extract_peer_id(p)
                        if pid:
                            folder_ids.add(pid)
            except Exception as exc:
                logging.warning(f"Shared folder link sync warning: {exc}")

    # 2. Read all Dialog Filters (Folders) on the account
    available_folders: list[str] = []
    try:
        res = await client(functions.messages.GetDialogFiltersRequest())
        filters = getattr(res, "filters", res)
        for f in filters:
            raw_title = getattr(f, "title", None)
            if raw_title is None:
                continue
            title_str = getattr(raw_title, "text", str(raw_title)).strip()
            if title_str:
                available_folders.append(title_str)

            f_id = getattr(f, "id", None)
            is_match = (title_str.lower() == target_folder.lower()) or (
                matched_filter_id is not None and f_id == matched_filter_id
            )
            if is_match:
                if title_str:
                    target_folder = title_str
                peers = list(getattr(f, "include_peers", [])) + list(
                    getattr(f, "pinned_peers", [])
                )
                for p in peers:
                    pid = _extract_peer_id(p)
                    if pid:
                        folder_ids.add(pid)
    except Exception as exc:
        logging.error(f"Failed to fetch dialog filters: {exc}")

    # 3. Map chat IDs to human-readable titles
    _known_chats.clear()
    async for dialog in client.iter_dialogs():
        if dialog.is_channel or dialog.is_group:
            _known_chats[int(dialog.entity.id)] = dialog.name

    _folder_chat_ids.clear()
    _folder_chat_ids.update(folder_ids)
    chat_names = [_known_chats.get(cid, f"ID {cid}") for cid in _folder_chat_ids]

    _update_state(
        active_folder=target_folder,
        available_folders=available_folders,
        monitored_chats=len(_folder_chat_ids),
        monitored_chat_names=sorted(chat_names),
    )
    logging.info(
        f"Synced folder '{target_folder}': {len(_folder_chat_ids)} chats active "
        f"(Available folders: {available_folders})"
    )
    return {
        "ok": True,
        "active_folder": target_folder,
        "available_folders": available_folders,
        "monitored_chats": len(_folder_chat_ids),
        "monitored_chat_names": sorted(chat_names),
    }


def sync_target_folder() -> dict[str, Any]:
    """Trigger an immediate folder re-sync from a Flask HTTP thread."""
    if not _monitor_loop or not _client_ref:
        return {"ok": False, "error": "Listener is not connected yet."}
    future = asyncio.run_coroutine_threadsafe(
        _sync_folder_chats(_client_ref), _monitor_loop
    )
    try:
        return future.result(timeout=20)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

async def _run_telethon_loop(interactive_auth: bool = False) -> None:
    """Main event-driven MTProto loop (Zero-Polling, epoll socket sleep)."""
    global _monitor_loop, _client_ref, _auth_completed_event
    _monitor_loop = asyncio.get_running_loop()
    _auth_completed_event = asyncio.Event()

    if not API_ID or not API_HASH or API_HASH == "your_telegram_api_hash_here":
        _update_state(
            status="ERROR",
            error_message="TG_API_ID or TG_API_HASH not set in .env",
        )
        logging.error("Missing TG_API_ID or TG_API_HASH in .env")
        return

    _update_state(status="STARTING", error_message=None, qr_url=None)
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    _client_ref = client

    try:
        await client.connect()
        is_authed = await client.is_user_authorized()

        if not is_authed:
            _update_state(
                status="AUTH_REQUIRED",
                error_message="Session not authorized. Click 'Generate QR Code' below to login via Telegram.",
            )
            logging.info("Waiting for Web Dashboard QR authorization...")
            await _auth_completed_event.wait()

        me = await client.get_me()
        my_id = me.id
        username = me.username or str(my_id)

        _update_state(
            status="ONLINE",
            account_username=username,
            account_id=my_id,
            error_message=None,
            qr_url=None,
        )
        await _sync_folder_chats(client)

        # Automatically re-sync whenever a folder is edited or a chat is added to it
        @client.on(events.Raw((types.UpdateDialogFilter, types.UpdateDialogFilters)))
        async def on_folder_updated(_update: Any) -> None:
            logging.info("Telegram folder change detected. Re-syncing folder chats...")
            await _sync_folder_chats(client)

        # Register Ban & Kick Handlers (only alert for chats in the monitored folder)
        @client.on(events.ChatAction)
        async def on_chat_action(event: events.ChatAction.Event) -> None:
            if (event.user_kicked or event.user_left) and event.user_id == my_id:
                chat = await event.get_chat()
                raw_id = getattr(chat, "id", event.chat_id)
                if raw_id in _alerted_bans:
                    return
                _alerted_bans.add(raw_id)

                chat_title = getattr(
                    chat, "title", _known_chats.get(raw_id, f"ID {raw_id}")
                )
                database.log_ban_event(
                    "GROUP_KICK",
                    chat_title,
                    "Listener account was kicked or removed from group.",
                )
                alert_text = (
                    f"🚫 <b>VENATOR ALERT: Kicked from Group!</b>\n"
                    f"📂 <b>Group:</b> {chat_title}\n"
                    f"ℹ️ Listener <code>@{username}</code> was removed."
                )
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, send_bot_notification, alert_text)

        @client.on(events.Raw(types.UpdateChannel))
        async def on_channel_update(update: types.UpdateChannel) -> None:
            channel_id = update.channel_id
            if channel_id not in _folder_chat_ids or channel_id in _alerted_bans:
                return

            chat_title = _known_chats.get(channel_id, f"Channel ID {channel_id}")
            try:
                entity = await client.get_entity(types.PeerChannel(channel_id))
                if isinstance(entity, types.ChannelForbidden) or getattr(
                    entity, "kicked", False
                ):
                    _alerted_bans.add(channel_id)
                    chat_title = getattr(entity, "title", chat_title)
                    database.log_ban_event(
                        "CHANNEL_BAN",
                        chat_title,
                        "Access revoked (ChannelForbidden).",
                    )
                    alert_text = (
                        f"🚫 <b>VENATOR ALERT: Banned from Channel!</b>\n"
                        f"📂 <b>Channel:</b> {chat_title}\n"
                        f"ℹ️ Access revoked (ChannelForbidden)."
                    )
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, send_bot_notification, alert_text)
            except (errors.ChannelPrivateError, errors.UserBannedInChannelError):
                _alerted_bans.add(channel_id)
                database.log_ban_event(
                    "CHANNEL_BAN",
                    chat_title,
                    "Channel threw ChannelPrivateError / UserBannedInChannelError.",
                )
                alert_text = (
                    f"🚫 <b>VENATOR ALERT: Banned from Channel!</b>\n"
                    f"📂 <b>Channel:</b> {chat_title}\n"
                    f"ℹ️ Channel threw ChannelPrivateError / UserBannedInChannelError."
                )
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, send_bot_notification, alert_text)
            except Exception:
                pass

        @client.on(events.NewMessage)
        async def on_new_message(event: events.NewMessage.Event) -> None:
            # Normalize chat ID to positive integer regardless of channel/supergroup/chat prefix
            chat_id = abs(event.chat_id)
            if chat_id > 1000000000000:
                chat_id = int(str(chat_id)[3:])  # strip -100 prefix

            # Ignore if chat is not in the monitored folder
            if chat_id not in _folder_chat_ids:
                return

            cfg = database.get_settings()
            if cfg.get("is_paused"):
                return

            raw_text = (event.raw_text or "").strip()
            if len(raw_text) < 20:
                return

            chat_title = _known_chats.get(chat_id, f"Chat {chat_id}")
            logging.info(f"⚡ Processing message from '{chat_title}': {raw_text[:60]}...")

            # 1. Stop-Words Check (e.g. #резюме)
            _, stops = database.get_compiled_regex()
            for stop_pat in stops:
                if stop_pat.search(raw_text):
                    logging.info(f"⏩ Skipped by stop-word: {stop_pat.pattern}")
                    with _state_lock:
                        _runtime_state["regex_skipped"] += 1
                    return

            # 2. SHA-256 Cross-Post Deduplication
            post_hash = compute_post_hash(raw_text)
            if post_hash in _seen_hashes:
                logging.info(f"⏩ Skipped duplicate hash from '{chat_title}'")
                with _state_lock:
                    _runtime_state["duplicates_skipped"] += 1
                return
            _seen_hashes.append(post_hash)

            chat = await event.get_chat()
            chat_title = getattr(chat, "title", chat_title)
            _known_chats[chat_id] = chat_title

            chat_username = getattr(chat, "username", None)
            if chat_username:
                msg_link = f"https://t.me/{chat_username}/{event.id}"
            else:
                clean_id = str(event.chat_id).replace("-100", "").replace("-", "")
                msg_link = f"https://t.me/c/{clean_id}/{event.id}"

            # 3. Evaluate EVERY new message with LLM
            logging.info(f"🧠 Calling LLM ({cfg.get('llm_model')}) for '{chat_title}'...")
            loop = asyncio.get_running_loop()
            verdict = await loop.run_in_executor(
                None, analyze_post_with_llm, raw_text
            )
            if not verdict or "_error" in verdict:
                err_msg = str(verdict.get('_error') if verdict else 'Empty response from LLM provider')
                logging.error(f"❌ LLM error: {err_msg}")

                if "all ai keys quota exhausted" not in err_msg.lower() and "all ai api keys exhausted" not in err_msg.lower():
                    if "429" in err_msg or "rate_limit" in err_msg.lower():
                        send_error_alert(
                            "AI Rate Limit Reached (HTTP 429)",
                            f"Limits exceeded for model '{cfg.get('llm_model')}'. Post from '{chat_title}' skipped. Error: {err_msg}",
                            cooldown_seconds=300
                        )
                    else:
                        send_error_alert(
                            "AI Model / API Failure",
                            f"Failed evaluating post from '{chat_title}' using '{cfg.get('llm_model')}': {err_msg}",
                            cooldown_seconds=120
                        )
                return

            logging.info(f"🔍 Raw LLM Output: {verdict}")

            # Resilient key extraction for any LLM schema variation
            approved = bool(verdict.get("approved") or verdict.get("is_match") or False)
            is_scam = bool(verdict.get("is_scam") or verdict.get("scam") or False)
            score_val = verdict.get("match_score") or verdict.get("score") or verdict.get("match") or 0
            try:
                score = int(score_val)
            except Exception:
                score = 0

            summary = str(verdict.get("summary") or verdict.get("description") or raw_text[:100])
            reason = str(verdict.get("reason") or verdict.get("explanation") or verdict.get("verdict") or "Evaluated by AI")
            pitch = str(verdict.get("recommended_pitch") or verdict.get("pitch") or "A")
            model_used = str(verdict.get("_model_used", cfg.get("llm_model", "")))
            min_score = int(cfg.get("min_score", 70))

            final_approved = approved and (not is_scam) and (score >= min_score)
            logging.info(
                f"📊 Verdict: approved={final_approved}, score={score}%, scam={is_scam} ({reason})"
            )

            database.log_order(
                chat_title=chat_title,
                msg_link=msg_link,
                approved=final_approved,
                is_scam=is_scam,
                match_score=score,
                summary=summary,
                reason=reason,
                recommended_pitch=pitch,
                model_used=model_used,
                raw_text=raw_text,
            )
            _update_state(last_event_at=time.strftime("%H:%M:%S UTC", time.gmtime()))

            if not final_approved:
                return

            snippet = raw_text[:480] + "..." if len(raw_text) > 480 else raw_text
            snippet = (
                snippet.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )

            alert_msg = (
                f"🔥 <b>Venator Match ({score}%)</b>\n"
                f"📂 <b>Source:</b> {chat_title}\n"
                f"📝 <b>Task:</b> {summary}\n"
                f"✅ <b>Fit:</b> {reason}\n"
                f"📨 <b>Pitch Template:</b> <code>{pitch}</code>\n"
                f"🧠 <b>Model:</b> <code>{model_used}</code>\n\n"
                f"<blockquote>{snippet}</blockquote>\n\n"
                f"👉 <a href='{msg_link}'>Open Post in Telegram</a>"
            )
            logging.info(f"🚀 Pushing alert to Telegram bot for '{chat_title}'...")
            await loop.run_in_executor(None, send_bot_notification, alert_msg)

        await client.run_until_disconnected()

    except (
        errors.UserDeactivatedError,
        errors.UserDeactivatedBanError,
        errors.AuthKeyUnregisteredError,
        errors.SessionRevokedError,
    ) as ban_err:
        err_name = type(ban_err).__name__
        _update_state(status="BANNED", error_message=f"{err_name}: {ban_err}")
        database.log_ban_event("ACCOUNT_BAN", "TELEGRAM_CORE", f"{err_name}: {ban_err}")
        send_bot_notification(
            f"🚨 <b>CRITICAL VENATOR ALERT: Listener Account Restricted!</b>\n"
            f"⚠️ <b>Error:</b> <code>{err_name}: {ban_err}</code>"
        )
        logging.critical(f"Listener account restricted: {ban_err}")
    except Exception as exc:
        _update_state(status="ERROR", error_message=str(exc))
        logging.error(f"Telethon loop error: {exc}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def _thread_target() -> None:
    """Dedicated background thread entry point with isolated asyncio event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_run_telethon_loop(interactive_auth=False))


def start_monitor_background() -> None:
    """Start the Telethon listener in a daemon thread if not already running."""
    global _monitor_thread
    if _monitor_thread and _monitor_thread.is_alive():
        return
    _monitor_thread = threading.Thread(
        target=_thread_target, name="VenatorTelethonWorker", daemon=True
    )
    _monitor_thread.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Venator Telegram AI Order Monitor")
    parser.add_argument(
        "--auth",
        action="store_true",
        help="Run interactive Telegram login to create the .session file",
    )
    args = parser.parse_args()
    database.init_db()
    asyncio.run(_run_telethon_loop(interactive_auth=args.auth))