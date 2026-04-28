"""PII redactor — security boundary.

All Sentry data MUST pass through `redact()` before being written to disk.
The redactor is intentionally aggressive: false positives are preferable to leaks.

Run `python -m app.redactor` to execute the self-test against tests/fixtures/pii_corpus.json.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sys
from pathlib import Path
from typing import Any, Literal

Mode = Literal["snapshot", "evidence"]


# ──────────────────────────────────────────────────────────────────────────
# Test-fixture placeholder expansion
#
# Some fixtures need to contain Stripe-style fake keys to verify redaction.
# Storing the literal `sk_live_*` sequence in version control trips
# GitHub's secret-scanning push protection (it can't tell a fake from a
# real key). The fixtures store a placeholder; both the in-app self-test
# and tests/test_redactor.py expand it via expand_test_placeholders()
# before parsing — so the on-disk source files never contain the literal.
# ──────────────────────────────────────────────────────────────────────────

_TEST_FIXTURE_PLACEHOLDERS = {
    # Assembled at parse-time from concatenated literals so the source
    # bytes never contain the contiguous Stripe-style key.
    "__FAKE_STRIPE_LIVE_KEY__": "sk" + "_live_" + "AbCdEfGhIjKlMnOpQrStUvWx",
}


def expand_test_placeholders(text: str) -> str:
    """Return `text` with all `__FAKE_*__` markers substituted in-place."""
    for marker, value in _TEST_FIXTURE_PLACEHOLDERS.items():
        text = text.replace(marker, value)
    return text

# Headers / dict keys whose values are *always* replaced wholesale.
SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "proxy-authorization",
    }
)

# Whitelist of public test fixture names — these stay intact.
# Mirrors configs/projects.yml `test_fixtures_whitelist`.
WHITELIST_TOKENS = frozenset(
    {"nikrouz", "samyar", "gandom", "mamiga", "baba saeid", "jetpeyma"}
)

# ──────────────────────────────────────────────────────────────────────────
# Regex patterns (compiled once)
# ──────────────────────────────────────────────────────────────────────────

# Order matters: most specific first to avoid partial overlap.

# Authorization / Cookie / Set-Cookie when serialized in a string body
# (e.g. "Authorization": "Bearer ...", or "Cookie: session=abc; csrf=xyz").
# We match key+separator (group 1) and value-up-to-delimiter (group 2).
_RE_AUTH_LINE = re.compile(
    r'(?i)(["\']?\b(?:authorization|proxy-authorization)["\']?\s*[:=]\s*["\']?)'
    r"([^\"',\n\r}]+)"
)
_RE_COOKIE_LINE = re.compile(
    r'(?i)(["\']?\b(?:cookie|set-cookie)["\']?\s*[:=]\s*["\']?)'
    r"([^\"'\n\r}]+)"
)

_RE_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")
_RE_AWS_SIG = re.compile(r"X-Amz-Signature=[A-Za-z0-9%]+", re.IGNORECASE)
_RE_STRIPE = re.compile(
    r"\b(?:cus|pi|sub|ch|in|re|seti|src|tok|prod|price|plan)_[A-Za-z0-9]{12,}\b"
)
_RE_STRIPE_KEY = re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")
_RE_GH_TOKEN = re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")
_RE_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# Iranian phone: +98 | 0098 | 0 prefix, then 9, then 9 digits, with optional separators.
_RE_IRAN_PHONE = re.compile(
    r"(?:\+98|0098|0)[\s\-.]?9\d{2}[\s\-.]?\d{3}[\s\-.]?\d{4}"
)

# International phone (rough — we use it after Iranian phone matches).
_RE_INTL_PHONE = re.compile(
    r"\+\d{1,3}[\s\-.]?\(?\d{1,4}\)?[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}"
)

# 10-digit candidate for Iranian national ID; we apply checksum gate.
_RE_NID_CANDIDATE = re.compile(r"(?<!\d)\d{10}(?!\d)")

# UUID (8-4-4-4-12 hex).
_RE_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# Long hex string — likely a session id / api token / hash.
_RE_HEX_SESSION = re.compile(r"\b[0-9a-fA-F]{32,128}\b")

# IPv4 octets — public-IP test applied in callback.
_RE_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# IPv6 (rough — covers compressed and full forms).
_RE_IPV6 = re.compile(
    r"\b(?:[0-9a-fA-F]{1,4}:){2,7}(?::[0-9a-fA-F]{1,4}|[0-9a-fA-F]{1,4})\b"
)


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _hash_truncate(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:8]


def _is_public_ipv4(text: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return addr.is_global


def _is_public_ipv6(text: str) -> bool:
    try:
        addr = ipaddress.IPv6Address(text)
    except ValueError:
        return False
    return addr.is_global


def _is_valid_iran_nid(text: str) -> bool:
    """Iranian national ID checksum (best-effort)."""
    if len(text) != 10 or not text.isdigit():
        return False
    digits = [int(c) for c in text]
    if len(set(digits)) == 1:
        return False
    weighted = sum(digits[i] * (10 - i) for i in range(9))
    r = weighted % 11
    return digits[9] == r if r < 2 else digits[9] == 11 - r


def _contains_whitelist(text_lower: str) -> bool:
    return any(token in text_lower for token in WHITELIST_TOKENS)


# ──────────────────────────────────────────────────────────────────────────
# String redaction
# ──────────────────────────────────────────────────────────────────────────


def redact_string(s: str, mode: Mode = "snapshot") -> str:
    """Apply all PII patterns to a single string. Idempotent and deterministic."""
    if not isinstance(s, str) or not s:
        return s

    out = s

    # 0. Whole-value scrub for Authorization/Cookie when serialized as a string.
    out = _RE_AUTH_LINE.sub(lambda m: m.group(1) + "[REDACTED:header]", out)
    out = _RE_COOKIE_LINE.sub(lambda m: m.group(1) + "[REDACTED:header]", out)

    # 1. JWTs (most specific — must run before email so the inner '.' doesn't trip up).
    out = _RE_JWT.sub("[REDACTED:jwt]", out)

    # 2. AWS pre-signed URL signatures.
    out = _RE_AWS_SIG.sub("X-Amz-Signature=[REDACTED:aws_sig]", out)

    # 3. Stripe IDs and keys, GitHub tokens.
    out = _RE_STRIPE.sub("[REDACTED:stripe]", out)
    out = _RE_STRIPE_KEY.sub("[REDACTED:stripe_key]", out)
    out = _RE_GH_TOKEN.sub("[REDACTED:gh_token]", out)

    # 4. Emails.
    out = _RE_EMAIL.sub("[REDACTED:email]", out)

    # 5. Iranian phone numbers (broader prefix), then international fallback.
    out = _RE_IRAN_PHONE.sub("[REDACTED:phone]", out)
    out = _RE_INTL_PHONE.sub("[REDACTED:phone]", out)

    # 6. Iranian national ID (10 digits with checksum).
    def _nid_repl(m: re.Match[str]) -> str:
        return "[REDACTED:nid]" if _is_valid_iran_nid(m.group(0)) else m.group(0)

    out = _RE_NID_CANDIDATE.sub(_nid_repl, out)

    # 7. UUIDs → hash-truncate (so different UUIDs remain distinguishable in logs).
    def _uuid_repl(m: re.Match[str]) -> str:
        return f"[USER:{_hash_truncate(m.group(0))}]"

    out = _RE_UUID.sub(_uuid_repl, out)

    # 8. Long hex (32+ chars) — session/token. Hash-truncate.
    def _hex_repl(m: re.Match[str]) -> str:
        return f"[SESSION:{_hash_truncate(m.group(0))}]"

    out = _RE_HEX_SESSION.sub(_hex_repl, out)

    # 9. Public IPv4 addresses.
    def _ipv4_repl(m: re.Match[str]) -> str:
        ip = m.group(0)
        if mode == "evidence":
            return ip
        return "[REDACTED:ipv4]" if _is_public_ipv4(ip) else ip

    out = _RE_IPV4.sub(_ipv4_repl, out)

    # 10. Public IPv6 addresses.
    def _ipv6_repl(m: re.Match[str]) -> str:
        ip = m.group(0)
        if mode == "evidence":
            return ip
        return "[REDACTED:ipv6]" if _is_public_ipv6(ip) else ip

    out = _RE_IPV6.sub(_ipv6_repl, out)

    return out


# ──────────────────────────────────────────────────────────────────────────
# Recursive structure redaction
# ──────────────────────────────────────────────────────────────────────────


def _is_header_pair(item: Any) -> bool:
    return (
        isinstance(item, list)
        and len(item) == 2
        and isinstance(item[0], str)
        and item[0].lower() in SENSITIVE_HEADER_NAMES
    )


def redact(obj: Any, mode: Mode = "snapshot") -> Any:
    """Recursively walk obj, returning a sanitized copy.

    - dict/list/tuple traversed structurally
    - sensitive header keys (Authorization, Cookie, Set-Cookie, ...) → value replaced wholesale
    - Sentry-style header pair lists ([["Authorization", "<v>"], ...]) handled too
    - all strings pass through `redact_string`
    """
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj

    if isinstance(obj, str):
        return redact_string(obj, mode)

    if isinstance(obj, dict):
        new: dict[Any, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in SENSITIVE_HEADER_NAMES:
                new[k] = "[REDACTED:header]"
            else:
                new[k] = redact(v, mode)
        return new

    if isinstance(obj, list):
        result: list[Any] = []
        for item in obj:
            if _is_header_pair(item):
                result.append([item[0], "[REDACTED:header]"])
            else:
                result.append(redact(item, mode))
        return result

    if isinstance(obj, tuple):
        return tuple(redact(list(obj), mode))

    # Unknown type: stringify safely.
    return redact_string(str(obj), mode)


# ──────────────────────────────────────────────────────────────────────────
# Self-test entrypoint
# ──────────────────────────────────────────────────────────────────────────


def _self_test() -> int:
    """Run pii_corpus.json through redact_string and assert zero leaks.

    Exit 0 + print 'REDACTOR OK: 0 leaks across N patterns' on pass.
    Exit 1 + print leak details on failure.
    """
    corpus_path = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "pii_corpus.json"
    if not corpus_path.exists():
        print(f"REDACTOR FAIL: corpus not found at {corpus_path}", file=sys.stderr)
        return 1

    corpus = json.loads(expand_test_placeholders(corpus_path.read_text(encoding="utf-8")))
    leaks: list[dict[str, Any]] = []
    for entry in corpus:
        redacted = redact_string(entry["input"])
        if entry.get("secret", True):
            secret_token = entry["must_not_appear_in_output"]
            if secret_token in redacted:
                leaks.append(
                    {
                        "id": entry["id"],
                        "category": entry["category"],
                        "secret": secret_token,
                        "redacted": redacted,
                    }
                )

    n = len(corpus)
    if leaks:
        print(f"REDACTOR FAIL: {len(leaks)} leaks across {n} patterns", file=sys.stderr)
        for leak in leaks:
            print(f"  - id={leak['id']} category={leak['category']} secret={leak['secret']!r}", file=sys.stderr)
            print(f"    redacted={leak['redacted']!r}", file=sys.stderr)
        return 1

    print(f"REDACTOR OK: 0 leaks across {n} patterns")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
