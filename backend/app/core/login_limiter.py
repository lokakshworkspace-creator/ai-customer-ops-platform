"""Login rate limiting: failed logins per email + client IP, counted in Redis.

How it works
* A failed login increments a counter keyed by a hash of (normalised email, client IP). The first
  failure starts a fixed window (`window_s`); the counter expires with it.
* Once the counter reaches `max_failures`, further attempts from that email + IP are refused with
  429 and a Retry-After (the seconds left in the window) BEFORE any password is checked, so a
  blocked caller learns nothing, not even whether a guess was right, and no argon2 work is done.
  Blocked attempts are not counted, so they cannot extend the window.
* A successful login deletes the counter.
* Unknown emails are counted exactly like known ones, so the limiter is no oracle for which emails
  exist. The key holds a hash, never the email.

THE KNOWN LIMIT. The limit is per email AND IP. An attacker who rotates through many IPs can try
`max_failures` passwords per IP against one email without being stopped by this limiter. It stops
one client from hammering an account (and cannot be used to lock a victim out from another IP); it
is not a defence against a distributed attack. Argon2's cost is the only other brake.

FAIL OPEN. Every Redis call is wrapped in a short timeout and an `except Exception`. If Redis is
down or slow, the limiter does nothing and a warning is logged, and logins keep working. The
alternative (fail closed) would let a Redis outage lock every user out of the system. The
cost:
while Redis is down there is no rate limiting. Redis is disposable by design (golden rule 4): it
only ever makes the system stricter, never decides who may log in.

CLIENT IP. See `client_ip`: the socket peer, or the entry `trusted_proxy_hops` from the right of
X-Forwarded-For when the deployment says there are that many proxies in front of the API.
"""

import asyncio
import hashlib
import ipaddress
import math

import structlog
from redis.asyncio import Redis
from starlette.requests import Request

log = structlog.get_logger(__name__)

KEY_PREFIX = "ops:login-fail:"

# INCR, and make sure the key has a TTL even if an earlier process died between the two calls
# (a counter without a TTL would block forever).
_INCR_WITH_TTL = """
local c = redis.call('INCR', KEYS[1])
if c == 1 or redis.call('PTTL', KEYS[1]) < 0 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
return c
"""


def normalise_email(email: str) -> str:
    return email.strip().lower()


def client_ip(request: Request, trusted_hops: int) -> str:
    """The caller's address. With `trusted_hops` = 0 only the socket peer is used. With N > 0 the
    Nth entry from the RIGHT of X-Forwarded-For (the one our own proxy appended) is used; the
    left side is whatever the client sent. A missing, short or malformed header falls back to the
    peer rather than trusting it."""
    peer = request.client.host if request.client else "unknown"
    if trusted_hops <= 0:
        return peer
    header = request.headers.get("x-forwarded-for", "")
    parts = [p.strip() for p in header.split(",") if p.strip()]
    if len(parts) < trusted_hops:
        return peer
    candidate = parts[-trusted_hops]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return peer


class LoginLimiter:
    def __init__(
        self,
        redis: Redis,
        *,
        max_failures: int,
        window_s: int,
        timeout_s: float = 0.5,
        prefix: str = KEY_PREFIX,
    ) -> None:
        self._redis = redis
        self.max_failures = max_failures
        self.window_s = window_s
        self._timeout_s = timeout_s
        self._prefix = prefix

    def key(self, email: str, ip: str) -> str:
        digest = hashlib.sha256(f"{normalise_email(email)}|{ip}".encode()).hexdigest()[:32]
        return f"{self._prefix}{digest}"

    async def retry_after(self, email: str, ip: str) -> int | None:
        """Seconds until this email + IP may try again, or None if it may try now."""
        key = self.key(email, ip)
        try:
            async with asyncio.timeout(self._timeout_s):
                raw = await self._redis.get(key)
                if raw is None or int(raw) < self.max_failures:
                    return None
                ttl_ms = await self._redis.pttl(key)
        except Exception as exc:
            self._unavailable("check", exc)
            return None
        return max(1, math.ceil(ttl_ms / 1000)) if ttl_ms > 0 else 1

    async def record_failure(self, email: str, ip: str) -> None:
        try:
            async with asyncio.timeout(self._timeout_s):
                await self._redis.eval(
                    _INCR_WITH_TTL, 1, self.key(email, ip), str(self.window_s * 1000)
                )
        except Exception as exc:
            self._unavailable("record", exc)

    async def reset(self, email: str, ip: str) -> None:
        try:
            async with asyncio.timeout(self._timeout_s):
                await self._redis.delete(self.key(email, ip))
        except Exception as exc:
            self._unavailable("reset", exc)

    @staticmethod
    def _unavailable(op: str, exc: Exception) -> None:
        log.warning("login_limiter_unavailable", op=op, error=type(exc).__name__)
