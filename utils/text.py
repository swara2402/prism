"""
utils.text
==========

Text utilities for evidence compression and pattern matching.
"""
from __future__ import annotations

import hashlib
import re
from typing import List, Tuple

# Pre-compiled regex patterns for anomaly detection
ERROR_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("error", re.compile(r"\berror\b", re.IGNORECASE)),
    ("exception", re.compile(r"\bexception\b", re.IGNORECASE)),
    ("fatal", re.compile(r"\bfatal\b", re.IGNORECASE)),
    ("panic", re.compile(r"\bpanic\b", re.IGNORECASE)),
    ("timeout", re.compile(r"\btimeout\b|\btimed out\b", re.IGNORECASE)),
    ("refused", re.compile(r"\bconnection refused\b", re.IGNORECASE)),
    ("oom", re.compile(r"\boom\b|out of memory", re.IGNORECASE)),
    ("crash", re.compile(r"\bcrash(ed)?\b", re.IGNORECASE)),
    ("denied", re.compile(r"\bpermission denied\b|\b403\b", re.IGNORECASE)),
    ("not_found", re.compile(r"\b404\b|\bnot found\b", re.IGNORECASE)),
    ("5xx", re.compile(r"\b5\d{2}\b")),
    ("429", re.compile(r"\b429\b|too many requests", re.IGNORECASE)),
]

API_PATTERN = re.compile(
    r"(?:GET|POST|PUT|DELETE|PATCH)\s+(/\S+)"
)
SERVICE_PATTERN = re.compile(
    r"\b(service|svc|app|microservice)[\s=:']+([a-zA-Z0-9_\-]+)",
    re.IGNORECASE,
)
TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+\-]\d{2}:?\d{2})?"
)


def hash_text(text: str) -> str:
    """Return a stable SHA-256 hash of ``text`` (used for dedup / IDs)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def normalize_log_line(line: str) -> str:
    """Normalize a log line by stripping timestamps and redundant whitespace."""
    line = TIMESTAMP_PATTERN.sub("<TS>", line)
    line = re.sub(r"\s+", " ", line).strip()
    return line


def classify_log_severity(line: str) -> Tuple[str, float]:
    """
    Classify the severity of a log line.

    Returns a tuple ``(label, score)`` where score is in [0, 1].
    """
    matches: List[str] = []
    for label, pat in ERROR_PATTERNS:
        if pat.search(line):
            matches.append(label)

    if not matches:
        return ("info", 0.1)

    severity_map = {
        "panic": 1.0,
        "fatal": 0.95,
        "oom": 0.9,
        "crash": 0.85,
        "exception": 0.7,
        "error": 0.6,
        "5xx": 0.7,
        "timeout": 0.5,
        "refused": 0.55,
        "429": 0.4,
        "denied": 0.3,
        "not_found": 0.25,
    }
    score = max(severity_map.get(m, 0.3) for m in matches)
    label = matches[0]
    return (label, score)


def extract_apis(text: str) -> List[str]:
    """Extract referenced API endpoint paths from ``text``."""
    return API_PATTERN.findall(text)


def extract_services(text: str) -> List[str]:
    """Extract service identifiers referenced in ``text``."""
    return [m.group(2) for m in SERVICE_PATTERN.finditer(text)]


def jaccard_similarity(a: set[str], b: set[str]) -> float:
    """Compute Jaccard similarity between two sets."""
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def tokenize(text: str) -> List[str]:
    """Simple word tokenizer for log text."""
    return [t for t in re.findall(r"[a-zA-Z][a-zA-Z0-9_\-]{1,}", text.lower())]
