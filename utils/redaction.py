"""
utils.redaction
===============

PII / secret redaction applied to incident evidence *before* it is stored
or sent to an LLM.

Logs and other evidence frequently contain credentials, tokens, session IDs,
emails, IPs, internal URLs — all of which should never be persisted raw or
exposed to a model.  This module provides an idempotent scrubber.

Redaction is **idempotent**: running it twice yields the same result as once,
because a redacted marker can never match a later pattern.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, List, Mapping

# ---------------------------------------------------------------------------
# Patterns — ordered so longer / more specific matches win first
# ---------------------------------------------------------------------------

_PATTERNS = [
    # JWT
    re.compile(
        r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    ),
    # Generic bearer tokens
    re.compile(r"(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    # AWS access keys / secret access keys
    re.compile(r"(AKIA|ASIA)[A-Z0-9]{16}"),
    re.compile(r"(aws_secret_access_key[\"'\s:=]+)[A-Za-z0-9/+=]{16,}"),
    # Private keys
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    # Generic "secret"/"token"/"password"/"key" = value  (word boundary aware)
    re.compile(
        r"(?i)(\b(?:secret|token|password|passwd|passphrase|api[-_]?key|"
        r"client[-_]?secret|access[-_]?token|auth[-_]?token)\b[\"'\s:=]+)"
        r"[A-Za-z0-9._~+/=-]{6,}"
    ),
    # Hyphenated credential tokens like ``secret-token-abc123``, ``api-key-9f42``.
    # Conservative by design: over-redaction is safer than under-redaction.
    re.compile(
        r"(?i)\b(?:super[-_])?(?:secret|token|passwd|password|api[-_]?key|"
        r"client[-_]?secret|access[-_]?token|auth[-_]?token|private[-_]?key)"
        r"[-_][A-Za-z0-9._~+/=-]{8,}\b"
    ),
    # DSN / connection strings with credentials
    re.compile(r"([a-z][a-z0-9+.-]*://)[^\s/@]+@", re.IGNORECASE),
    # Session IDs / Authorization header values
    re.compile(
        r"(?i)(\b(?:session[-_]?id|x-?auth[-_]?token|authorization|"
        r"set-cookie)\b[\"'\s:=]+)[A-Za-z0-9._~+/=-]{8,}"
    ),
    # Google API keys
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    # Slack tokens
    re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"),
    # OpenAI keys (sk-..., sk-proj-...)
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    # GitHub personal / fine-grained access tokens
    re.compile(r"\bgh[ousrp]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    # GitLab personal access tokens
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    # npm access tokens
    re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b"),
    # Stripe secret / restricted keys (sk_/rk_ live/test)
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
]

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_CREDIT_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,16}\b")
_INTERNAL_URL_RE = re.compile(
    r"\b(?:http[s]?://)?(?:localhost|127\.0\.0\.1|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})"
    r"(?::\d{1,5})?(?:/\S*)?"
)

#: Replacement markers — identical left/right so redaction is idempotent.
_SECRET_MARKER = "[REDACTED_SECRET]"
_EMAIL_MARKER = "[REDACTED_EMAIL]"
_IP_MARKER = "[REDACTED_IP]"
_KEY_MARKER = "[REDACTED_KEY]"
_URL_MARKER = "[REDACTED_URL]"


def scrub(text: str) -> str:
    """Return *text* with credentials / PII replaced by markers."""
    if not text:
        return text
    out = text
    for pattern in _PATTERNS:
        out = pattern.sub(_SECRET_MARKER, out)
    out = _EMAIL_RE.sub(_EMAIL_MARKER, out)
    out = _CREDIT_CARD_RE.sub(_KEY_MARKER, out)
    out = _INTERNAL_URL_RE.sub(_URL_MARKER, out)
    out = _IPV4_RE.sub(_IP_MARKER, out)
    return out


def scrub_collection(value: Any) -> Any:
    """Recursively scrub a nested JSON structure in place and return it."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [scrub_collection(item) for item in value]
    if isinstance(value, dict):
        return {k: scrub_collection(v) for k, v in value.items()}
    return value


def scrub_iterable(values: Iterable[str]) -> List[str]:
    """Scrub a list of strings."""
    return [scrub(v) for v in values]


def scrub_mapping(values: Mapping[str, Any]) -> dict:
    """Scrub every string value in a mapping."""
    return scrub_collection(dict(values))


__all__ = [
    "scrub",
    "scrub_collection",
    "scrub_iterable",
    "scrub_mapping",
]