"""Neutralise externally sourced values before they reach a log line.

A value that came from a request path, an S3 object or an exception message can
carry CR/LF or other control characters. Logged raw, those forge extra log lines
or hide text from a reader (CWE-117, CodeQL ``py/log-injection``). ``log_safe``
renders the value as a single printable line: CR and LF become the literal
escapes ``\\r`` / ``\\n``, every other C0/C1 control character (and DEL) becomes
``\\xNN``, and very long values are truncated.

Apply it at the log call, to each externally sourced argument, rather than
trusting an upstream validator: the sink is the only place that knows the value
is about to be written to a log.
"""

from __future__ import annotations

import re

_MAX_LEN = 500
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def log_safe(value: object, *, max_len: int = _MAX_LEN) -> str:
    """``value`` as one printable log-safe line (see module doc)."""
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    text = _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", text)
    if len(text) > max_len:
        text = text[:max_len] + "...[truncated]"
    return text
