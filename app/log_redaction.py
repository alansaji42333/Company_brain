"""Keep OAuth callback query values out of standard access logs.

Uvicorn's access logger (the repository's documented launch paths: plain
`uvicorn app.server:app` from the README and the entrypoint's uvicorn
fallback) logs the full request target, including the OAuth callback's
`code` and `state` query values. Gunicorn's access log is disabled by
default; when enabled it flows through the same logging machinery, so the
filter is installed for it too.

Reverse proxies, tunnels and hosting platforms log OUTSIDE this process:
this module cannot redact them — operators must set a no-query or
redacting access format there themselves (see README). No claim is made
that any external service has been verified.

Behavior: callback access records keep method, bare path, HTTP version and
status; the query string (code/state values) is dropped for the OAuth
callback route only. Non-callback requests are logged exactly as before.
Installed from app.server's lifespan so every launch path that runs the
app is covered. Operational-hygiene control, not an authentication control.
Test coverage: tests/test_log_redaction.py.
"""
import logging
import re

# The single org-less OAuth callback route (one deployment-wide redirect URI;
# registered with Google exactly like this).
OAUTH_CALLBACK_PATH = "/api/v1/connectors/drive/oauth/callback"

# A request target inside an access record, e.g.:
#   "GET /api/v1/connectors/drive/oauth/callback?code=X&state=Y HTTP/1.1"
_ACCESS_TARGET_RE = re.compile(
    r'"(?P<method>[A-Z]+) (?P<path>[^ ?"]*' + re.escape(OAUTH_CALLBACK_PATH) + r'[a-zA-Z0-9_/-]*)'
    r'(?:\?(?P<query>[^ "]*))?(?P<proto> HTTP/[^"]+)"'
)


def _redact_target(match: "re.Match[str]") -> str:
    return f'"{match.group("method")} {match.group("path")}{match.group("proto")}"'


class CallbackQueryRedactionFilter(logging.Filter):
    """Drop the query string of OAuth-callback access records before they
    are formatted. Records for any other request pass through unchanged."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True  # never break logging over a formatting oddity
        if OAUTH_CALLBACK_PATH not in message or "?" not in message:
            return True
        redacted = _ACCESS_TARGET_RE.sub(_redact_target, message)
        if redacted == message:
            # Unexpected access format the precise regex did not catch: fall
            # back to blindly stripping query strings on the line — nothing
            # credential-shaped survives.
            redacted = re.sub(r'\?(?:[^ "]*) HTTP/', " HTTP/", message)
            redacted = re.sub(r'\?[^"]*', "?", redacted)
        # Replace the record's substance wholesale (plain msg, args dropped)
        # so no later formatter variant can resurrect the raw values.
        record.msg, record.args = redacted, None
        return True


_LOGGERS = ("uvicorn.access", "gunicorn.access")


def install_callback_query_redaction() -> None:
    """Attach the filter to the supported access loggers (idempotent).
    Called from the FastAPI lifespan so it runs under uvicorn, gunicorn and
    the entrypoint's uvicorn fallback alike."""
    for logger_name in _LOGGERS:
        access_logger = logging.getLogger(logger_name)
        if not any(isinstance(f, CallbackQueryRedactionFilter) for f in access_logger.filters):
            access_logger.addFilter(CallbackQueryRedactionFilter())
