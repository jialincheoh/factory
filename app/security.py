"""Sign-in, sessions and CSRF for the real estate agent side."""
import secrets
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException, Request

_hasher = PasswordHasher()
# Hash compared against when the email is unknown, so response time does not
# reveal which emails have accounts.
_DUMMY_HASH = _hasher.hash(secrets.token_hex(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerifyMismatchError, InvalidHashError):
        return False


# --- login throttling: at most 10 failed attempts per IP per 15 minutes ---
_WINDOW, _MAX_FAILS = 15 * 60, 10
_fails: dict[str, deque] = defaultdict(deque)


def login_allowed(ip: str) -> bool:
    q = _fails[ip]
    while q and q[0] < time.monotonic() - _WINDOW:
        q.popleft()
    return len(q) < _MAX_FAILS


def record_failed_login(ip: str) -> None:
    _fails[ip].append(time.monotonic())


# --- sessions (signed cookie via Starlette's SessionMiddleware) ---
def current_agent_id(request: Request) -> str | None:
    return request.session.get("agent_id")


def require_agent(request: Request) -> str:
    agent_id = current_agent_id(request)
    if not agent_id:
        raise HTTPException(status_code=303, headers={"Location": "/agent/login"})
    return agent_id


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = request.session["csrf"] = secrets.token_urlsafe(32)
    return token


def check_csrf(request: Request, submitted: str | None) -> None:
    expected = request.session.get("csrf")
    if not expected or not submitted or not secrets.compare_digest(expected, submitted):
        raise HTTPException(status_code=403, detail="Form expired. Reload the page and try again.")
