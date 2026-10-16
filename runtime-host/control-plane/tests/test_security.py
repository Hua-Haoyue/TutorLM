"""令牌与凭据处理。"""

from __future__ import annotations

import pytest

from control_plane.security import (
    bearer_token,
    generate_token,
    hash_token,
    verify_token,
)


def test_tokens_are_unique_and_long():
    tokens = {generate_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(token) >= 32 for token in tokens)


def test_hash_is_deterministic_and_salted():
    a = hash_token("secret", "pepper-1")
    b = hash_token("secret", "pepper-1")
    c = hash_token("secret", "pepper-2")
    assert a == b
    assert a != c  # 不同盐 -> 不同哈希
    assert a != hash_token("secret2", "pepper-1")


def test_verify_roundtrip():
    token = generate_token()
    digest = hash_token(token, "p")
    assert verify_token(token, digest, "p") is True
    assert verify_token(token + "x", digest, "p") is False
    assert verify_token(token, digest, "wrong") is False


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("BEARER  abc ", "abc"),
        ("Basic abc", None),
        ("Bearer", None),
        ("", None),
        (None, None),
    ],
)
def test_bearer_parsing(raw, expected):
    assert bearer_token(raw) == expected
