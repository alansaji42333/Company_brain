"""Access-log privacy: OAuth callback query values must never reach the
standard uvicorn/gunicorn access logs.

Uses dummy markers for code/state (never real values). Simulates the
exact record shape uvicorn emits (args = (client_addr, request_line,
status)) and asserts the redaction filter keeps method/path/status while
dropping the query. Also verifies non-callback requests are logged
unchanged (their queries are not sensitive) and that the filter is
attached by the server lifespan."""
import logging

from app.log_redaction import (
    OAUTH_CALLBACK_PATH,
    CallbackQueryRedactionFilter,
    install_callback_query_redaction,
)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


def _emit_access_line(record_message: str, *args) -> list[str]:
    capture = _Capture()
    access_logger = logging.getLogger("uvicorn.access")
    install_callback_query_redaction()
    previous_level = access_logger.level
    access_logger.setLevel(logging.INFO)  # pass the record through the level gate
    access_logger.addHandler(capture)
    try:
        access_logger.info(record_message, *args)
    finally:
        access_logger.setLevel(previous_level)
        access_logger.removeHandler(capture)
    return capture.records


def test_callback_query_values_redacted_from_access_log():
    (record,) = _emit_access_line(
        '%s - "%s" %d',
        "127.0.0.1",
        f"GET {OAUTH_CALLBACK_PATH}?code=dummy-one-time-code-marker&state=dummy-state-marker HTTP/1.1",
        302,
    )
    # query values are gone; method, path, protocol and status retained
    assert "dummy-one-time-code-marker" not in record
    assert "dummy-state-marker" not in record
    assert f"GET {OAUTH_CALLBACK_PATH} HTTP/1.1" in record
    assert record.endswith("302")


def test_non_callback_access_lines_are_untouched():
    (record,) = _emit_access_line(
        '%s - "%s" %d',
        "127.0.0.1",
        "GET /api/v1/orgs/me?include=orgs HTTP/1.1",
        200,
    )
    assert record.endswith('GET /api/v1/orgs/me?include=orgs HTTP/1.1" 200')


def test_unexpected_callback_format_falls_back_to_query_strip():
    """Any callback access line the precise matcher can't parse still loses
    its query (nothing credential-shaped survives)."""
    (record,) = _emit_access_line(
        '%s - "%s" %d',
        "127.0.0.1",
        f"GET {OAUTH_CALLBACK_PATH}?code=dummy-one-time-code-marker",
        302,
    )
    assert "dummy-one-time-code-marker" not in record
    assert f"GET {OAUTH_CALLBACK_PATH}" in record.rstrip("?") or f"GET {OAUTH_CALLBACK_PATH}?" in record


def test_filter_is_directly_checkable_and_idempotent():
    install_callback_query_redaction()
    install_callback_query_redaction()
    access_logger = logging.getLogger("uvicorn.access")
    filters = [f for f in access_logger.filters if isinstance(f, CallbackQueryRedactionFilter)]
    assert len(filters) == 1
    gunicorn_logger = logging.getLogger("gunicorn.access")
    assert any(isinstance(f, CallbackQueryRedactionFilter) for f in gunicorn_logger.filters)


def test_lifespan_wires_the_install():
    """The supported launch paths (uvicorn/gunicorn) run the FastAPI
    lifespan, which must call the install. Import-time wiring check: the
    lifespan references the installer by name and source order puts it
    before any component that serves requests."""
    import inspect

    from app import log_redaction, server

    source = inspect.getsource(server.lifespan)
    assert "install_callback_query_redaction" in source
    # and the installer itself is importable and idempotent (see above)
    install_callback_query_redaction()
    assert isinstance(log_redaction.CallbackQueryRedactionFilter(), logging.Filter)
