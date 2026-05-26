import logging
from datetime import datetime
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from app.config import SLACK_BOT_TOKEN, SLACK_CHANNEL_IDS, GOOGLE_SHEET_ID, GOOGLE_SHEET_TAB
from app.google_auth import get_credentials

logger = logging.getLogger(__name__)

TOOL_SCHEMAS = [
    {
        "name": "send_slack_message",
        "description": "Send a message to a Slack channel. Only works for channels the bot has been invited to.",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "The Slack channel ID to post in (must be one of the configured channels)",
                },
                "message": {
                    "type": "string",
                    "description": "The message text to send",
                },
            },
            "required": ["channel_id", "message"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": "Create an event on the user's primary Google Calendar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "The event title/summary",
                },
                "start_time": {
                    "type": "string",
                    "description": "ISO 8601 start time (e.g. 2026-06-20T14:00:00)",
                },
                "end_time": {
                    "type": "string",
                    "description": "ISO 8601 end time (e.g. 2026-06-20T15:00:00)",
                },
                "description": {
                    "type": "string",
                    "description": "Optional event description",
                },
                "attendee_emails": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of attendee email addresses",
                },
            },
            "required": ["title", "start_time", "end_time"],
        },
    },
    {
        "name": "append_sheet_row",
        "description": "Append a row of data to a configured Google Sheet.",
        "input_schema": {
            "type": "object",
            "properties": {
                "values": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The cell values for the new row, one per column",
                },
            },
            "required": ["values"],
        },
    },
]


def describe_tool_call(tool_name: str, tool_input: dict) -> str:
    if tool_name == "send_slack_message":
        channel_id = tool_input.get("channel_id", "?")
        allowed = set(c.strip() for c in SLACK_CHANNEL_IDS.split(",") if c.strip())
        channel_label = f"channel {channel_id}"
        msg = tool_input.get("message", "")
        return f"Send to {channel_label}: \"{msg}\""

    if tool_name == "create_calendar_event":
        title = tool_input.get("title", "Untitled")
        start = tool_input.get("start_time", "?")
        end = tool_input.get("end_time", "?")
        desc = ""
        try:
            st = datetime.fromisoformat(start)
            et = datetime.fromisoformat(end)
            start_fmt = st.strftime("%Y-%m-%d %H:%M")
            end_fmt = et.strftime("%H:%M")
            desc = f" on {start_fmt}–{end_fmt}"
        except (ValueError, TypeError):
            desc = f" from {start} to {end}"
        attendees = tool_input.get("attendee_emails", [])
        if attendees:
            desc += f", inviting {', '.join(attendees)}"
        return f"Create calendar event \"{title}\"{desc}"

    if tool_name == "append_sheet_row":
        values = tool_input.get("values", [])
        vals_str = " | ".join(values)
        return f"Append row to sheet: [{vals_str}]"

    return f"Execute tool {tool_name} with input {tool_input}"


def execute_tool(tool_name: str, tool_input: dict) -> dict:
    if tool_name == "send_slack_message":
        return _execute_slack_message(tool_input)
    if tool_name == "create_calendar_event":
        return _execute_calendar_event(tool_input)
    if tool_name == "append_sheet_row":
        return _execute_sheet_row(tool_input)
    return {"success": False, "error": f"Unknown tool: {tool_name}"}


def _execute_slack_message(tool_input: dict) -> dict:
    channel_id = tool_input.get("channel_id", "")
    message = tool_input.get("message", "")

    allowed = set(c.strip() for c in SLACK_CHANNEL_IDS.split(",") if c.strip())
    if channel_id not in allowed:
        return {
            "success": False,
            "error": f"Channel {channel_id} is not in the configured SLACK_CHANNEL_IDS. Allowed channels: {', '.join(allowed) if allowed else 'none'}",
        }

    if not SLACK_BOT_TOKEN:
        return {"success": False, "error": "SLACK_BOT_TOKEN is not configured"}

    try:
        client = WebClient(token=SLACK_BOT_TOKEN)
        resp = client.chat_postMessage(channel=channel_id, text=message)
        permalink = resp.get("message", {}).get("permalink", "")
        ts = resp.get("ts", "")
        return {
            "success": True,
            "permalink": permalink,
            "ts": ts,
            "message": f"Message sent to channel {channel_id}",
        }
    except SlackApiError as e:
        logger.error("Slack API error: %s", e)
        return {"success": False, "error": f"Slack API error: {e.response.get('error', str(e))}"}


def _execute_calendar_event(tool_input: dict) -> dict:
    try:
        creds = get_credentials()
        service = build("calendar", "v3", credentials=creds)
    except Exception as e:
        return {"success": False, "error": f"Failed to authenticate with Google Calendar: {e}"}

    event = {
        "summary": tool_input.get("title", ""),
        "description": tool_input.get("description", ""),
        "start": {
            "dateTime": tool_input.get("start_time", ""),
            "timeZone": "UTC",
        },
        "end": {
            "dateTime": tool_input.get("end_time", ""),
            "timeZone": "UTC",
        },
    }

    attendees = tool_input.get("attendee_emails", [])
    if attendees:
        event["attendees"] = [{"email": e} for e in attendees]

    try:
        created = service.events().insert(calendarId="primary", body=event).execute()
        return {
            "success": True,
            "event_id": created.get("id", ""),
            "html_link": created.get("htmlLink", ""),
            "message": f"Event created: {created.get('htmlLink', '')}",
        }
    except HttpError as e:
        logger.error("Calendar API error: %s", e)
        return {"success": False, "error": f"Calendar API error: {e}"}


def _execute_sheet_row(tool_input: dict) -> dict:
    if not GOOGLE_SHEET_ID:
        return {"success": False, "error": "GOOGLE_SHEET_ID is not configured"}

    values = tool_input.get("values", [])
    if not values:
        return {"success": False, "error": "No values provided for the row"}

    try:
        creds = get_credentials()
        service = build("sheets", "v4", credentials=creds)
    except Exception as e:
        return {"success": False, "error": f"Failed to authenticate with Google Sheets: {e}"}

    body = {"values": [values]}
    range_name = f"'{GOOGLE_SHEET_TAB}'!A:A"

    try:
        result = service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range=range_name,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()
        updated_range = result.get("updates", {}).get("updatedRange", "")
        return {
            "success": True,
            "updated_range": updated_range,
            "message": f"Row appended to sheet ({updated_range})",
        }
    except HttpError as e:
        logger.error("Sheets API error: %s", e)
        return {"success": False, "error": f"Sheets API error: {e}"}
