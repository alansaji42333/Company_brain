import logging
import hashlib
from datetime import datetime, timezone
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from app.config import SLACK_BOT_TOKEN, SLACK_CHANNEL_IDS, CHUNK_SIZE, SLACK_CONVERSATION_WINDOW_MINUTES
from app.chunking import token_count, chunk_document

logger = logging.getLogger(__name__)

_username_cache: dict[str, str] = {}
_client: WebClient | None = None


def _get_client() -> WebClient:
    global _client
    if _client is None:
        token = SLACK_BOT_TOKEN
        if not token:
            raise ValueError("SLACK_BOT_TOKEN must be set in .env")
        _client = WebClient(token=token)
    return _client


def _ensure_in_channel(channel_id: str):
    """Join a channel before ingesting it.

    Slack bots must be members of a channel to read its history. The
    `channels:join` scope allows joining public channels; private channels
    still require a manual invite. Failures are logged but non-fatal so
    ingestion proceeds for channels the bot is already in.
    """
    try:
        client = _get_client()
        client.conversations_join(channel=channel_id)
        logger.info("Joined channel %s", channel_id)
    except SlackApiError as e:
        err = e.response.get("error", "")
        if err == "method_not_allowed_for_channel_type" or err == "already_in_channel":
            return
        logger.warning("Could not join channel %s: %s", channel_id, err)


def _resolve_user(user_id: str) -> str:
    if user_id not in _username_cache:
        try:
            client = _get_client()
            resp = client.users_info(user=user_id)
            profile = resp.get("user", {}).get("profile", {})
            display = profile.get("display_name") or profile.get("real_name") or user_id
            _username_cache[user_id] = display
        except SlackApiError:
            _username_cache[user_id] = user_id
    return _username_cache[user_id]


def _ts_to_datetime(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def _ts_to_iso(ts: str) -> str:
    return _ts_to_datetime(ts).isoformat()


def _get_channel_name(channel_id: str) -> str:
    try:
        client = _get_client()
        resp = client.conversations_info(channel=channel_id)
        return resp["channel"].get("name", channel_id)
    except SlackApiError:
        return channel_id


def _get_permalink(channel_id: str, message_ts: str) -> str:
    try:
        client = _get_client()
        resp = client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
        return resp.get("permalink", "")
    except SlackApiError:
        return ""


def fetch_channel_messages(channel_id: str) -> list[dict]:
    client = _get_client()
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


def fetch_thread_replies(channel_id: str, thread_ts: str) -> list[dict]:
    client = _get_client()
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


def _format_dialogue(messages: list[dict], channel_name: str) -> str:
    lines = []
    for msg in messages:
        ts = msg.get("ts", "0")
        dt = _ts_to_datetime(ts)
        date_str = dt.strftime("%Y-%m-%d %H:%M")
        if _is_bot_message(msg):
            sender = msg.get("username", "Bot")
        else:
            user = msg.get("user", "")
            sender = _resolve_user(user) if user else "Unknown"
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
) -> list[dict]:
    dialogue = _format_dialogue(conversation, channel_name)
    first_ts = conversation[0].get("ts", "0")
    permalink = _get_permalink(channel_id, first_ts)
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
        }]

    doc = {
        "id": f"slack_{channel_id}_{window_id}",
        "name": f"#{channel_name}",
        "source_url": permalink,
        "text": dialogue,
        "user_id": user_id,
    }
    sub_chunks = chunk_document(doc)
    for c in sub_chunks:
        c["doc_id"] = f"slack_{channel_id}"
        c["doc_name"] = f"#{channel_name}"
        c["source_type"] = "slack"
        c["channel_name"] = channel_name
        c["ingested_at"] = ingested_at
    return sub_chunks


def ingest_slack_channel(channel_id: str, user_id: str = "") -> list[dict]:
    _ensure_in_channel(channel_id)
    channel_name = _get_channel_name(channel_id)
    logger.info("Fetching messages for #%s (%s)", channel_name, channel_id)

    messages = fetch_channel_messages(channel_id)
    logger.info("Fetched %d message(s) from #%s", len(messages), channel_name)

    thread_ids = set()
    for msg in messages:
        if msg.get("thread_ts") and msg.get("thread_ts") != msg.get("ts"):
            thread_ids.add(msg["thread_ts"])

    for thread_ts in list(thread_ids)[:]:
        existing_ts = {m.get("ts") for m in messages}
        if thread_ts in existing_ts:
            continue
        replies = fetch_thread_replies(channel_id, thread_ts)
        if replies:
            parent = replies[0]
            if parent.get("ts") not in existing_ts:
                messages.append(parent)

    for thread_ts in thread_ids:
        replies = fetch_thread_replies(channel_id, thread_ts)
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
        chunks = conversation_to_chunks(conv, channel_id, channel_name, window_id, user_id=user_id)
        all_chunks.extend(chunks)

    logger.info("Created %d chunk(s) from #%s", len(all_chunks), channel_name)
    return all_chunks


def ingest_slack(user_id: str = "") -> list[dict]:
    channel_ids_str = SLACK_CHANNEL_IDS
    if not channel_ids_str:
        raise ValueError("SLACK_CHANNEL_IDS must be set in .env")

    channel_ids = [c.strip() for c in channel_ids_str.split(",") if c.strip()]
    if not channel_ids:
        raise ValueError("SLACK_CHANNEL_IDS is empty")

    all_chunks = []
    for cid in channel_ids:
        try:
            chunks = ingest_slack_channel(cid, user_id=user_id)
            all_chunks.extend(chunks)
        except Exception as e:
            logger.error("Failed to ingest channel %s: %s", cid, e)

    return all_chunks
