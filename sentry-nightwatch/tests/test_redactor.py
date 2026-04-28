"""Tests for app.redactor — security boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.redactor import (
    SENSITIVE_HEADER_NAMES,
    WHITELIST_TOKENS,
    _hash_truncate,
    _is_public_ipv4,
    _is_public_ipv6,
    _is_valid_iran_nid,
    expand_test_placeholders,
    redact,
    redact_string,
)

FIXTURES = Path(__file__).parent / "fixtures"

# Stripe-style fake key, assembled at parse-time so the source bytes never
# contain the contiguous `sk_live_<24chars>` literal (otherwise GitHub's
# secret-scanning push protection rejects the commit).
_FAKE_SK_LIVE = "sk" + "_live_" + "AbCdEfGhIjKlMnOpQrStUvWx"


# ──────────────────────────────────────────────────────────────────────────
# Corpus-driven tests
# ──────────────────────────────────────────────────────────────────────────


def _load_corpus() -> list[dict]:
    raw = (FIXTURES / "pii_corpus.json").read_text(encoding="utf-8")
    return json.loads(expand_test_placeholders(raw))


@pytest.mark.parametrize("entry", _load_corpus(), ids=lambda e: f"{e['id']}-{e['category']}")
def test_corpus_no_secret_leaks(entry: dict) -> None:
    out = redact_string(entry["input"])
    if entry.get("secret", True):
        assert entry["must_not_appear_in_output"] not in out, (
            f"LEAK in {entry['category']}: {entry['must_not_appear_in_output']!r} "
            f"present in redacted output {out!r}"
        )


# ──────────────────────────────────────────────────────────────────────────
# Pattern-specific tests
# ──────────────────────────────────────────────────────────────────────────


def test_email_replaced() -> None:
    out = redact_string("contact me at user@example.com please")
    assert "user@example.com" not in out
    assert "[REDACTED:email]" in out


def test_jwt_replaced() -> None:
    jwt = "eyJhbGciOi.eyJzdWIiOi.SflKxwRJSMeKKF2QT4fwpMeJf36"
    out = redact_string(f"token={jwt}")
    assert jwt not in out
    assert "[REDACTED:jwt]" in out


def test_stripe_id_replaced() -> None:
    out = redact_string("Customer cus_NeFG1RhZdkc4Yz failed")
    assert "cus_NeFG1RhZdkc4Yz" not in out
    assert "[REDACTED:stripe]" in out


def test_stripe_key_replaced() -> None:
    out = redact_string(f"key={_FAKE_SK_LIVE}")
    assert _FAKE_SK_LIVE not in out


def test_iran_phone_replaced() -> None:
    for phone in ["+989121234567", "00989351112233", "09121234567", "0912 123 4567"]:
        out = redact_string(f"call {phone} now")
        assert phone.replace(" ", "") not in out.replace(" ", ""), f"phone {phone} leaked: {out}"


def test_iran_nid_checksum_valid_redacted() -> None:
    out = redact_string("national_id=0012345679")
    assert "0012345679" not in out
    assert "[REDACTED:nid]" in out


def test_iran_nid_checksum_invalid_kept() -> None:
    # 0000000000 — all same digits, invalid; 1234567890 — fails checksum
    out = redact_string("ticket id 1234567890")
    assert "1234567890" in out  # not redacted as NID


def test_uuid_hashed() -> None:
    uuid = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    out = redact_string(f"GET /users/{uuid}/profile")
    assert uuid not in out
    assert "[USER:" in out


def test_session_hex_hashed() -> None:
    sid = "8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e"
    out = redact_string(f"session={sid}")
    assert sid not in out
    assert "[SESSION:" in out


def test_public_ipv4_redacted() -> None:
    out = redact_string("from 8.8.8.8 connection")
    assert "8.8.8.8" not in out
    assert "[REDACTED:ipv4]" in out


def test_private_ipv4_kept() -> None:
    for ip in ["10.0.0.42", "192.168.1.10", "172.16.5.5", "127.0.0.1"]:
        out = redact_string(f"local {ip} ok")
        assert ip in out, f"private IP {ip} was wrongly redacted: {out}"


def test_public_ipv6_redacted() -> None:
    out = redact_string("origin 2001:4860:4860::8888 reached")
    assert "2001:4860:4860::8888" not in out


def test_aws_signature_redacted() -> None:
    url = "https://s3.example.com/x?X-Amz-Signature=AbCd1234SECRET"
    out = redact_string(url)
    assert "AbCd1234SECRET" not in out


def test_authorization_header_string_form() -> None:
    out = redact_string('"Authorization": "Bearer abc123token"')
    assert "abc123token" not in out
    assert "[REDACTED:header]" in out


def test_cookie_header_string_form() -> None:
    out = redact_string("Cookie: session=abc123secret; csrf=xyzPRIVATE")
    assert "abc123secret" not in out
    assert "xyzPRIVATE" not in out


# ──────────────────────────────────────────────────────────────────────────
# Whitelist preservation
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["nikrouz", "Nikrouz", "samyar", "gandom", "mamiga"])
def test_whitelist_names_preserved(name: str) -> None:
    out = redact_string(f"order created for user {name}")
    assert name in out


def test_whitelist_with_uuid_only_uuid_redacted() -> None:
    uuid = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    out = redact_string(f"Test fixture user Nikrouz logged in via {uuid}")
    assert "Nikrouz" in out
    assert uuid not in out


# ──────────────────────────────────────────────────────────────────────────
# Recursive structure traversal
# ──────────────────────────────────────────────────────────────────────────


def test_redact_dict_authorization_key() -> None:
    obj = {"Authorization": "Bearer secret", "other": "ok"}
    out = redact(obj)
    assert out["Authorization"] == "[REDACTED:header]"
    assert out["other"] == "ok"


def test_redact_nested_dict() -> None:
    obj = {"req": {"headers": {"Cookie": "s=secret"}, "url": "http://x?email=a@b.com"}}
    out = redact(obj)
    assert out["req"]["headers"]["Cookie"] == "[REDACTED:header]"
    assert "a@b.com" not in out["req"]["url"]


def test_redact_sentry_header_pair_list() -> None:
    obj = {
        "headers": [
            ["Authorization", "Bearer abc"],
            ["Content-Type", "application/json"],
            ["Set-Cookie", "sid=secret42"],
        ]
    }
    out = redact(obj)
    assert out["headers"][0] == ["Authorization", "[REDACTED:header]"]
    assert out["headers"][1] == ["Content-Type", "application/json"]
    assert out["headers"][2] == ["Set-Cookie", "[REDACTED:header]"]


def test_redact_list_with_strings() -> None:
    obj = ["user@example.com", "ok", {"email": "x@y.com"}]
    out = redact(obj)
    assert "[REDACTED:email]" in out[0]
    assert out[1] == "ok"
    assert "[REDACTED:email]" in out[2]["email"]


def test_redact_preserves_primitives() -> None:
    obj = {"count": 42, "ratio": 0.5, "ok": True, "missing": None}
    out = redact(obj)
    assert out == obj


def test_redact_full_event_no_leaks() -> None:
    """End-to-end: real Sentry event with PII through redact()."""
    raw = (FIXTURES / "sentry_event_full.json").read_text(encoding="utf-8")
    event = json.loads(expand_test_placeholders(raw))
    redacted = redact(event)
    flat = json.dumps(redacted)
    # Critical secrets that must NOT appear
    must_not_appear = [
        "cus_NeFG1RhZdkc4Yz",
        "pi_3OabcdEFG12HIJ34kl",
        _FAKE_SK_LIVE,
        "billing-test@shahrzad.ai",
        "admin@shahrzad.ai",
        "real.user@example.com",
        "user.bug@example.com",
        "+989121234567",
        "0012345679",
        "8.8.8.8",
        "138.197.76.197",
        "abc123def456SECRETSIGNATURE789xyz",
        "abc123secret",
        "8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e",
        "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    ]
    for secret in must_not_appear:
        assert secret not in flat, f"event leak: {secret!r}"
    # Whitelisted names stay
    assert "Nikrouz" in flat or "nikrouz" in flat.lower()
    # Private IP stays
    assert "10.0.0.42" in flat


# ──────────────────────────────────────────────────────────────────────────
# Mode-specific behavior
# ──────────────────────────────────────────────────────────────────────────


def test_evidence_mode_keeps_public_ip_but_scrubs_secrets() -> None:
    """Evidence mode is more permissive (keeps IPs for debug) but secrets still scrubbed."""
    s = f"from 8.8.8.8 with key {_FAKE_SK_LIVE}"
    snap = redact_string(s, mode="snapshot")
    evid = redact_string(s, mode="evidence")
    assert "8.8.8.8" not in snap
    assert "8.8.8.8" in evid
    assert _FAKE_SK_LIVE not in evid


# ──────────────────────────────────────────────────────────────────────────
# Helper unit tests
# ──────────────────────────────────────────────────────────────────────────


def test_hash_truncate_deterministic() -> None:
    assert _hash_truncate("abc") == _hash_truncate("abc")
    assert len(_hash_truncate("abc")) == 8


def test_iran_nid_checksum() -> None:
    assert _is_valid_iran_nid("0012345679")
    assert not _is_valid_iran_nid("0000000000")
    assert not _is_valid_iran_nid("1234567890")
    assert not _is_valid_iran_nid("12345")


def test_is_public_ipv4() -> None:
    assert _is_public_ipv4("8.8.8.8")
    assert not _is_public_ipv4("10.0.0.1")
    assert not _is_public_ipv4("127.0.0.1")
    assert not _is_public_ipv4("not-an-ip")


def test_is_public_ipv6() -> None:
    assert _is_public_ipv6("2001:4860:4860::8888")
    assert not _is_public_ipv6("::1")
    assert not _is_public_ipv6("not-an-ipv6")


def test_sensitive_headers_constant() -> None:
    assert "authorization" in SENSITIVE_HEADER_NAMES
    assert "cookie" in SENSITIVE_HEADER_NAMES


def test_whitelist_constant() -> None:
    assert "nikrouz" in WHITELIST_TOKENS


# ──────────────────────────────────────────────────────────────────────────
# Edge cases / coverage
# ──────────────────────────────────────────────────────────────────────────


def test_redact_tuple_input() -> None:
    out = redact(("a@b.com", "10.0.0.1", 5))
    assert isinstance(out, tuple)
    assert "a@b.com" not in out[0]
    assert out[1] == "10.0.0.1"
    assert out[2] == 5


def test_redact_unknown_type_stringified() -> None:
    class Obj:
        def __str__(self) -> str:
            return "leaked@example.com"

    out = redact(Obj())
    assert "leaked@example.com" not in out


def test_redact_string_empty_and_none() -> None:
    assert redact_string("") == ""
    assert redact_string(None) is None  # type: ignore[arg-type]


def test_self_test_runs_clean() -> None:
    from app import redactor as r

    rc = r._self_test()
    assert rc == 0


def test_self_test_missing_corpus(tmp_path, monkeypatch) -> None:
    """If pii_corpus.json is missing, self-test exits 1 with diagnostic."""
    from app import redactor as r

    fake_module_file = tmp_path / "redactor.py"
    fake_module_file.write_text("# stub")
    monkeypatch.setattr(r, "__file__", str(fake_module_file))
    rc = r._self_test()
    assert rc == 1
