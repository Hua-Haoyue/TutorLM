"""访客令牌与管理令牌。

访客令牌是"知道 URL 不等于授权"的唯一凭据来源：明文只在签发响应里出现
一次，数据库只保存 ``HMAC-SHA256(pepper, token)``。这样即使控制面数据库泄漏，
也无法直接拿去访问实例。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

#: 令牌字节数；32 字节 = 256 bit 熵，足以抵抗在线暴力猜测。
DEFAULT_TOKEN_BYTES = 32


def generate_token(nbytes: int = DEFAULT_TOKEN_BYTES) -> str:
    """生成 URL 安全的随机令牌。"""

    return secrets.token_urlsafe(nbytes)


def hash_token(token: str, pepper: str) -> str:
    """带盐哈希。用 HMAC-SHA256 避免长度扩展与彩虹表。"""

    return hmac.new(
        pepper.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_token(token: str, token_hash: str, pepper: str) -> bool:
    """常数时间比较，避免通过响应时间泄漏哈希前缀。"""

    if not token or not token_hash:
        return False
    return hmac.compare_digest(hash_token(token, pepper), token_hash)


def bearer_token(authorization: str | None) -> str | None:
    """从 ``Authorization: Bearer x`` 头里取令牌。"""

    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    value = parts[1].strip()
    return value or None


def app_bootstrap_token(config, store, lease) -> str:
    """Per-lease server-only credential, domain-separated from browser tokens."""
    pepper = config.token_pepper.strip() or store.get_or_create_meta('token_pepper', generate_token(24))
    identity = f'app-bootstrap:v1:{lease.lease_id}:{lease.slot_id}:{lease.generation}'
    return hash_token(identity, pepper)
