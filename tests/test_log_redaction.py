"""Access-log privacy: OAuth callback query values must never reach the
standard uvicorn/gunicorn access logs.

Uses dummy markers for code/state (never real values). Coverage:

- The GENUINE Uvicorn access record (five-field args tuple, msg template
  '%s - "%s %s HTTP/%s" %d') passed through the installed filter AND the
  actual uvicorn.logging.AccessFormatter on a real handler: one normal
  formatted callback line, method/path/HTTP-version/status retained, dummy
  code/state absent, and no logging error on stderr (302 and an error
  status).
- The sanitized record.arguments themselves contain no query values, so a
  later handler or crash diagnostic cannot print them.
- Generic Gunicorn-style string records, unknown-format fallback,
  idempotent installation, and unchanged non-callback query logging."""
import logging

from uvicorn.logging import AccessFormatter

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


# Uvicorn's real template/args shape (uvicorn.protocols.utils et al.).
_UVICORN_ACCESS_MSG = '%s - "%s %s HTTP/%s" %d'
_DUMMY_CODE = "dummy-one-time-code-marker"
_DUMMY_STATE = "dummy-state-marker"


def _emit_through_real_uvicorn_formatter(client_addr, method, path, http_version, status_code, capfd):
    """Log a genuine five-argument Uvicorn access record and format it with
    the ACTUAL installed AccessFormatter through a REAL StreamHandler.
    Returns stdout/stderr text for assertions."""
    install_callback_query_redaction()
    access_logger = logging.getLogger("uvicorn.access")
    previous_level, previous_propagate = access_logger.level, access_logger.propagate
    access_logger.setLevel(logging.INFO)
    access_logger.propagate = False
    handler = logging.StreamHandler()  # real handler, writing to the real stderr
    handler.setFormatter(
        AccessFormatter(
            fmt='%(client_addr)s - "%(request_line)s" %(status_code)s',
        )
    )
    access_logger.addHandler(handler)
    try:
        access_logger.info(_UVICORN_ACCESS_MSG, client_addr, method, path, http_version, status_code)
    finally:
        access_logger.removeHandler(handler)
        access_logger.setLevel(previous_level)
        access_logger.propagate = previous_propagate
    captured = capfd.readouterr()
    return captured.out + captured.err


def test_real_uvicorn_formatter_keeps_callback_line_302_and_sanitizes(capfd):
    err = _emit_through_real_uvicorn_formatter(
        "127.0.0.1:12345",
        "GET",
        f"{OAUTH_CALLBACK_PATH}?code={_DUMMY_CODE}&state={_DUMMY_STATE}",
        "1.1",
        302,
        capfd,
    )
    # exactly ONE normal formatted callback line — not dropped, no error
    value, sep, _first = err.rstrip("\n").partition("\n")
    rest = (sep and _first) or ""
    assert value.startswith("127.0.0.1:12345 - ")
    assert 'GET /api/v1/connectors/drive/oauth/callback HTTP/1.1' in err
    assert "302" in value
    assert rest == ""
    assert _DUMMY_CODE not in err
    assert _DUMMY_STATE not in err
    assert "code=" not in err and "state=" not in err
    # no logging error / formatter traceback on stderr
    assert "Logging error" not in err
    assert "Traceback" not in err


def test_real_uvicorn_formatter_with_error_status(capfd):
    err = _emit_through_real_uvicorn_formatter(
        "127.0.0.1:12346",
        "GET",
        f"{OAUTH_CALLBACK_PATH}?code={_DUMMY_CODE}&state={_DUMMY_STATE}",
        "1.1",
        500,
        capfd,
    )
    assert "GET /api/v1/connectors/drive/oauth/callback HTTP/1.1" in err
    assert " 500 " in err or err.rstrip().endswith("500 Internal Server Error")
    assert _DUMMY_CODE not in err and _DUMMY_STATE not in err
    assert "Logging error" not in err
    assert "Traceback" not in err


def test_sanitized_record_arguments_carry_no_query_values():
    """After the filter, record.args is still a five-field tuple (matching
    Uvicorn's formatter contract) but WITHOUT any callback query value —
    safe for any later handler or diagnostic that formats the record."""
    full_path = f"{OAUTH_CALLBACK_PATH}?code={_DUMMY_CODE}&state={_DUMMY_STATE}"
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=_UVICORN_ACCESS_MSG,
        args=("127.0.0.1:1234", "GET", full_path, "1.1", 302),
        exc_info=None,
    )
    install_callback_query_redaction()
    access_logger = logging.getLogger("uvicorn.access")
    filt = next(f for f in access_logger.filters if isinstance(f, CallbackQueryRedactionFilter))
    assert filt.filter(record) is True
    assert record.args == (
        "127.0.0.1:1234",
        "GET",
        OAUTH_CALLBACK_PATH,
        "1.1",
        302,
    )
    assert _DUMMY_CODE not in str(record.args)
    assert _DUMMY_STATE not in str(record.args)
    # and the template still renders safely with the sanitized tuple
    assert _DUMMY_CODE not in record.getMessage()
    assert f"GET {OAUTH_CALLBACK_PATH} HTTP/1.1" in record.getMessage()


def _make_generic_record(msg, args):
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


def test_callback_query_values_redacted_from_access_log():
    (record,) = _emit_access_line(
        '%s - "%s" %d',
        "127.0.0.1",
        f"GET {OAUTH_CALLBACK_PATH}?code={_DUMMY_CODE}&state={_DUMMY_STATE} HTTP/1.1",
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


def test_non_callback_real_uvicorn_record_untouched():
    """A genuine Uvicorn five-field record for a DIFFERENT route keeps its
    full query (non-callback queries are not sensitive)."""
    record = _make_generic_record(
        _UVICORN_ACCESS_MSG, ("127.0.0.1:9999", "GET", "/api/v1/orgs/me?include=orgs", "1.1", 200)
    )
    filt = CallbackQueryRedactionFilter()
    before = record.args
    assert filt.filter(record) is True
    assert record.args == before  # untouched, still five fields
    assert "include=orgs" in record.getMessage()


def test_unexpected_callback_format_falls_back_to_query_strip():
    """Any callback access line the precise matcher can't parse still loses
    its query (nothing credential-shaped survives)."""
    (record,) = _emit_access_line(
        '%s - "%s" %d',
        "127.0.0.1",
        f"GET {OAUTH_CALLBACK_PATH}?code={_DUMMY_CODE}",
        302,
    )
    assert "dummy-one-time-code-marker" not in record
    assert f"GET {OAUTH_CALLBACK_PATH}" in record.rstrip("?") or f"GET {OAUTH_CALLBACK_PATH}?" in record


def test_gunicorn_style_record_still_sanitized():
    """The enabled Gunicorn access-log format ('%(message)s', args =
    (dict,)) and other string-shaped records remain covered by the generic
    path — they must NOT be treated as Uvicorn's five-field shape."""
    (record,) = _emit_access_line(
        '%s "%s" %s',
        "127.0.0.1:8",
        f"GET {OAUTH_CALLBACK_PATH}?code={_DUMMY_CODE}&state={_DUMMY_STATE} HTTP/1.1",
        "200",
    )
    assert _DUMMY_CODE not in record and _DUMMY_STATE not in record
    assert f"GET {OAUTH_CALLBACK_PATH} HTTP/1.1" in record


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
    lifespan, which must call the install."""
    import inspect

    from app import log_redaction, server

    source = inspect.getsource(server.lifespan)
    assert "install_callback_query_redaction" in source
    install_callback_query_redaction()
    assert isinstance(log_redaction.CallbackQueryRedactionFilter(), logging.Filter)
