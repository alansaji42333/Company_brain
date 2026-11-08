import hashlib
import logging
from datetime import datetime, timezone

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from app.chunking import chunk_document, token_count
from app.config import CHUNK_SIZE, SLACK_BOT_TOKEN, SLACK_CHANNEL_IDS, SLACK_CONVERSATION_WINDOW_MINUTES
from app.vectorstore import delete_by_doc_id

logger = logging.getLogger(__name__)

_username_cache: dict[tuple[str, str], str] = {}
_client: WebClient | None = None        # env-path client (cached, original behavior)
_client_cache: dict[str, WebClient] = {}  # per-token cache for org connector ingests
_active_client: WebClient | None = None   # client context set by ingest_slack(token=...) while that ingest runs


def _get_client(token: str | None = None) -> WebClient:
    """Resolve the WebClient to use.

    - explicit token → shared per-token cache (org connector ingests);
    - no token → the active per-ingest client if one is set, else the cached
      env client — byte-for-byte the original single-tenant behavior.
    """
    global _client
    if token:
        client = _client_cache.get(token)
        if client is None:
            client = WebClient(token=token)
            _client_cache[token] = client
        return client

    if _active_client is not None:
        return _active_client
    if _client is None:
        env_token = SLACK_BOT_TOKEN
        if not env_token:
            raise ValueError("SLACK_BOT_TOKEN must be set in .env")
        _client = WebClient(token=env_token)
    return _client


def _ensure_in_channel(channel_id: str, client: WebClient | None = None):
    """Join a channel before ingesting it.

    Slack bots must be members of a channel to read its history. The
    `channels:join` scope allows joining public channels; private channels
    still require a manual invite. Failures are logged but non-fatal so
    ingestion proceeds for channels the bot is already in.
    """
    try:
        client = client or _get_client()
        client.conversations_join(channel=channel_id)
        logger.info("Joined channel %s", channel_id)
    except SlackApiError as e:
        err = e.response.get("error", "")
        if err == "method_not_allowed_for_channel_type" or err == "already_in_channel":
            return
        logger.warning("Could not join channel %s: %s", channel_id, err)


def _resolve_user(user_id: str, client: WebClient | None = None) -> str:
    # Per-(token, user) names: two orgs may share slack user ids across
    # distinct workspaces, and the display name differs per workspace.
    key = (client.token if client else _get_client().token, user_id)
    if key not in _username_cache:
        try:
            client = client or _get_client()
            resp = client.users_info(user=user_id)
            profile = resp.get("user", {}).get("profile", {})
            display = profile.get("display_name") or profile.get("real_name") or user_id
            _username_cache[key] = display
        except SlackApiError:
            _username_cache[key] = user_id
    return _username_cache[key]


def _ts_to_datetime(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def _ts_to_iso(ts: str) -> str:
    return _ts_to_datetime(ts).isoformat()


def _get_channel_name(channel_id: str, client: WebClient | None = None) -> str:
    try:
        client = client or _get_client()
        resp = client.conversations_info(channel=channel_id)
        return resp["channel"].get("name", channel_id)
    except SlackApiError:
        return channel_id


def _get_permalink(channel_id: str, message_ts: str, client: WebClient | None = None) -> str:
    try:
        client = client or _get_client()
        resp = client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
        return resp.get("permalink", "")
    except SlackApiError:
        return ""


def fetch_channel_messages(channel_id: str, client: WebClient | None = None) -> list[dict]:
    client = client or _get_client()
    messages = []
    cursor = None
    while True:
        params = {
            "channel": channel_id,
            "limit": 100,
        }
        if cursor:
            params["cursor"] = cursor
        try:
            resp = client.conversations_history(**params)
        except SlackApiError as e:
            logger.warning("Error fetching channel %s: %s", channel_id, e)
            break
        messages.extend(resp.get("messages", []))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return messages


def fetch_thread_replies(channel_id: str, thread_ts: str, client: WebClient | None = None) -> list[dict]:
    client = client or _get_client()
    replies = []
    cursor = None
    while True:
        params = {
            "channel": channel_id,
            "ts": thread_ts,
            "limit": 100,
        }
        if cursor:
            params["cursor"] = cursor
        try:
            resp = client.conversations_replies(**params)
        except SlackApiError as e:
            logger.warning("Error fetching thread %s in %s: %s", thread_ts, channel_id, e)
            break
        replies.extend(resp.get("messages", []))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return replies


def _is_bot_message(msg: dict) -> bool:
    return msg.get("subtype") in ("bot_message",) or bool(msg.get("bot_id"))


def _format_dialogue(messages: list[dict], channel_name: str, client: WebClient | None = None) -> str:
    lines = []
    for msg in messages:
        ts = msg.get("ts", "0")
        dt = _ts_to_datetime(ts)
        date_str = dt.strftime("%Y-%m-%d %H:%M")
        if _is_bot_message(msg):
            sender = msg.get("username", "Bot")
        else:
            user = msg.get("user", "")
            sender = _resolve_user(user, client) if user else "Unknown"
        text = msg.get("text", "")
        lines.append(f"[#{channel_name}, {date_str}]")
        lines.append(f"{sender}: {text}")
    return "\n".join(lines)


def _build_window_id(messages: list[dict]) -> str:
    combined = "".join(m.get("ts", "") for m in messages)
    return hashlib.md5(combined.encode()).hexdigest()[:12]


def _message_ts(msg: dict) -> float:
    return float(msg.get("ts", "0"))


def group_into_conversations(messages: list[dict], channel_id: str, channel_name: str) -> list[list[dict]]:
    thread_map: dict[str, list[dict]] = {}
    standalone: list[dict] = []

    for msg in messages:
        if msg.get("subtype") == "channel_join" or msg.get("subtype") == "channel_leave":
            continue
        thread_ts = msg.get("thread_ts")
        if thread_ts:
            if thread_ts not in thread_map:
                thread_map[thread_ts] = []
            thread_map[thread_ts].append(msg)
        else:
            standalone.append(msg)

    groups: list[list[dict]] = []

    for thread_ts, thread_msgs in thread_map.items():
        groups.append(thread_msgs)

    standalone.sort(key=_message_ts)
    if standalone:
        current_group = [standalone[0]]
        for msg in standalone[1:]:
            gap = _message_ts(msg) - _message_ts(current_group[-1])
            if gap > SLACK_CONVERSATION_WINDOW_MINUTES * 60:
                groups.append(current_group)
                current_group = [msg]
            else:
                current_group.append(msg)
        groups.append(current_group)

    return groups


def conversation_to_chunks(
    conversation: list[dict],
    channel_id: str,
    channel_name: str,
    window_id: str,
    user_id: str = "",
    org_id: str = "",
    client: WebClient | None = None,
) -> list[dict]:
    dialogue = _format_dialogue(conversation, channel_name, client)
    first_ts = conversation[0].get("ts", "0")
    permalink = _get_permalink(channel_id, first_ts, client)
    ingested_at = datetime.now(timezone.utc).isoformat()

    token_len = token_count(dialogue)
    if token_len <= CHUNK_SIZE:
        return [{
            "doc_id": f"slack_{channel_id}",
            "doc_name": f"#{channel_name}",
            "source_url": permalink,
            "chunk_index": 0,
            "text": dialogue,
            "source_type": "slack",
            "channel_name": channel_name,
            "ingested_at": ingested_at,
            "user_id": user_id,
            "org_id": org_id,
        }]

    doc = {
        "id": f"slack_{channel_id}_{window_id}",
        "name": f"#{channel_name}",
        "source_url": permalink,
        "text": dialogue,
        "user_id": user_id,
        "org_id": org_id,
    }
    sub_chunks = chunk_document(doc, user_id=user_id, org_id=org_id)
    for c in sub_chunks:
        c["doc_id"] = f"slack_{channel_id}"
        c["doc_name"] = f"#{channel_name}"
        c["source_type"] = "slack"
        c["channel_name"] = channel_name
        c["ingested_at"] = ingested_at
    return sub_chunks


def ingest_slack_channel(channel_id: str, user_id: str = "", org_id: str = "") -> list[dict]:
    """Pipeline for ONE channel. Uses the active per-ingest client when an
    org-token ingest is running (ingest_slack), else the cached env client —
    the public signature intentionally stays 3-arg."""
    client = _get_client()
    _ensure_in_channel(channel_id, client)
    channel_name = _get_channel_name(channel_id, client)
    logger.info("Fetching messages for #%s (%s)", channel_name, channel_id)

    messages = fetch_channel_messages(channel_id, client)
    logger.info("Fetched %d message(s) from #%s", len(messages), channel_name)

    thread_ids = set()
    for msg in messages:
        if msg.get("thread_ts") and msg.get("thread_ts") != msg.get("ts"):
            thread_ids.add(msg["thread_ts"])

    for thread_ts in list(thread_ids)[:]:
        existing_ts = {m.get("ts") for m in messages}
        if thread_ts in existing_ts:
            continue
        replies = fetch_thread_replies(channel_id, thread_ts, client)
        if replies:
            parent = replies[0]
            if parent.get("ts") not in existing_ts:
                messages.append(parent)

    for thread_ts in thread_ids:
        replies = fetch_thread_replies(channel_id, thread_ts, client)
        existing = {m.get("ts") for m in messages}
        for r in replies:
            if r.get("ts") not in existing:
                messages.append(r)
                existing.add(r.get("ts"))

    logger.info("After thread resolution: %d total message(s) for #%s", len(messages), channel_name)

    conversations = group_into_conversations(messages, channel_id, channel_name)
    logger.info("Grouped into %d conversation(s) for #%s", len(conversations), channel_name)

    all_chunks = []
    for conv in conversations:
        window_id = _build_window_id(conv)
        chunks = conversation_to_chunks(conv, channel_id, channel_name, window_id, user_id=user_id, org_id=org_id, client=client)
        all_chunks.extend(chunks)

    logger.info("Created %d chunk(s) from #%s", len(all_chunks), channel_name)
    return all_chunks


def ingest_slack(
    user_id: str = "",
    org_id: str = "",
    token: str | None = None,
    channel_ids: list[str] | None = None,
) -> dict[str, dict]:
    """Ingest Slack channels with REPLACE semantics, per-org capable.

    `token`/`channel_ids` are the org's resolved connector values (provided by
    the worker); when BOTH are None the behavior is byte-for-byte the original
    env path (SLACK_BOT_TOKEN / SLACK_CHANNEL_IDS). While an org token is in
    use it is installed as the active per-ingest client context so every fetch
    helper (and any caller inside the loop) uses that workspace's credentials.

    Returns ``{channel_id: {"chunks": [...], "chunks_deleted": n}}``: for each
    channel the stored chunks of ``slack_{channel_id}`` (this org only) are
    deleted BEFORE the fresh chunks are returned, and the caller is
    responsible for storing them. A channel whose pipeline fails is skipped
    entirely (its stored chunks are left untouched rather than
    deleted-then-lost).
    """
    global _active_client
    if token:
        # Per-token client context for this ingest (reset in finally).
        _active_client = _get_client(token)

    try:
        channel_ids = list(channel_ids) if channel_ids is not None else _env_channel_ids()
        result: dict[str, dict] = {}
        for cid in channel_ids:
            try:
                chunks = ingest_slack_channel(cid, user_id=user_id, org_id=org_id)
            except Exception as e:
                logger.error("Failed to ingest channel %s: %s", cid, e)
                continue
            chunks_deleted = delete_by_doc_id(f"slack_{cid}", org_id)
            result[cid] = {"chunks": chunks, "chunks_deleted": chunks_deleted}

        return result
    finally:
        global_state_reset()


def _env_channel_ids() -> list[str]:
    """Original env fallback: raises exactly as before when unconfigured."""
    channel_ids_str = SLACK_CHANNEL_IDS
    if not channel_ids_str:
        raise ValueError("SLACK_CHANNEL_IDS must be set in .env")

    ids = [c.strip() for c in channel_ids_str.split(",") if c.strip()]
    if not ids:
        raise ValueError("SLACK_CHANNEL_IDS is empty")
    return ids


def global_state_reset():
    global _active_client
    _active_client = None
