# SPDX-License-Identifier: GPL-3.0-or-later
"""Saved Sessions and Remembered Accounts in the Credential Store (macOS Keychain or Windows Credential Manager).

A Saved Session is the EduPage session cookie from one browser sign-in, together
with the EduPage account it belongs to, stored for one school (and optionally one
account label). Sessions saved by older versions may hold several cookies and still load. There is no password field anywhere: the person
types their password into EduPage in Chrome, never into this program.

Credential Store entry, the same on both platforms: service `edupage-keyring-mcp`, account `<school>`, or
`<school>/<sha1 of label>` when a label tells several accounts at one school
apart (and for sessions saved before labels became optional);
secret = JSON {"format": 2, "account": ..., "login": ..., "cookies": [[name, value, domain, path, expires], ...]}.

A Remembered Account is a second entry, written only when the person ticked
"Remember password" in EduPage's own sign-in dialog (ADR 0002): the cookies by
which EduPage recognises that sign-in window again, and the account they are for.
Its account is `<that account>:remembered`;
secret = JSON {"format": 1, "school": ..., "account": ..., "username": ..., "cookies": [...]}.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import dataclass
from typing import Any

from .errors import EduPageError, log_unexpected

SERVICE = 'edupage-keyring-mcp'
FORMAT = 2
SECRET_LIMIT = 2 * 1024  # characters, on save; Credential Manager holds 2560 bytes of UTF-16
LOAD_LIMIT = 64 * 1024   # characters, on load; Saved Sessions from older versions are larger
COOKIE_LIMIT = 64        # cookies in a loaded Saved Session
REMEMBERED_FORMAT = 1
REMEMBERED_SUFFIX = ':remembered'  # no school name or label hash contains a colon
REMEMBERED_COOKIE_LIMIT = 4
TOKEN_CHARS = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
                        "!#$%&'*+-.^_`|~")


# The OS Credential Store backend accepted on each platform (ADR 0001); nothing else,
# with the attributes set on it before use. On Windows a Saved Session or Remembered
# Account must stay on this machine, never roam to others with a domain profile.
CREDENTIAL_STORES: dict[str, tuple[str, dict[str, Any]]] = {
    'darwin': ('keyring.backends.macOS', {}),
    'win32': ('keyring.backends.Windows', {'persist': 'local machine'}),
}


def open_credential_store(platform: str | None = None, backend: Any = None) -> SessionStore:
    """The SessionStore for this platform's Credential Store, or CREDENTIAL_STORE_REQUIRED.

    Only the built-in macOS Keychain and Windows Credential Manager backends are
    accepted: third-party plugins, file-based keyrings, chainer, fail and null are refused.
    """
    entry = CREDENTIAL_STORES.get(sys.platform if platform is None else platform)
    if entry is None:
        raise EduPageError('CREDENTIAL_STORE_REQUIRED')
    allowed, settings = entry
    if backend is None:
        import keyring
        backend = keyring.get_keyring()
    module = type(backend).__module__
    if module != allowed and not module.startswith(allowed + '.'):
        raise EduPageError('CREDENTIAL_STORE_REQUIRED')
    for name, value in settings.items():
        setattr(backend, name, value)
    return SessionStore(backend)


def account_key(school: str, label: str = '') -> str:
    if not label:
        return school
    return school + '/' + hashlib.sha1(label.casefold().encode(), usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class Cookie:
    name: str
    value: str
    domain: str
    path: str = '/'
    expires: float = -1  # seconds since epoch; -1 = session cookie

    def belongs_to(self, host: str) -> bool:
        return self.domain.lstrip('.') in (host, 'edupage.org')

    def expired(self, now: float) -> bool:
        return 0 < self.expires <= now


def parse_cookie(name: Any, value: Any, domain: Any, path: Any = '/', expires: Any = -1) -> Cookie:
    """Build a Cookie from untrusted parts, or raise SESSION_CORRUPT."""
    ok = (isinstance(name, str) and 0 < len(name) <= 128 and set(name) <= TOKEN_CHARS
          and isinstance(value, str) and len(value) <= 4096 and value.isprintable() and ';' not in value
          and isinstance(domain, str) and isinstance(path, str) and path[:1] == '/' and len(path) <= 256
          and type(expires) in (int, float))
    if not ok:
        raise EduPageError('SESSION_CORRUPT')
    return Cookie(name, value, domain, path, float(expires))


def session_cookies(host: str, cookies: list[Cookie], now: float | None = None) -> list[Cookie]:
    """The live cookies for `host`; foreign cookies are an error, expired ones are dropped."""
    if not cookies or len(cookies) > COOKIE_LIMIT:
        raise EduPageError('SESSION_CORRUPT')
    if not all(c.belongs_to(host) for c in cookies):
        raise EduPageError('SESSION_CORRUPT')
    moment = time.time() if now is None else now
    live = [c for c in cookies if not c.expired(moment)]
    if not live:
        raise EduPageError('SESSION_EXPIRED')
    return live


@dataclass(frozen=True)
class SavedSession:
    school: str
    account: str  # EduPage user ID, e.g. Rodic123
    login: str    # the login name EduPage reports, if any; informational only
    cookies: tuple[Cookie, ...]


@dataclass(frozen=True)
class RememberedAccount:
    school: str
    account: str   # EduPage user ID
    username: str  # the login name EduPage lists the remembered account under
    cookies: tuple[Cookie, ...]


class SessionStore:
    """Credential Store persistence; `backend` is anything with keyring's three functions.

    Build it with open_credential_store, which accepts only the OS Credential Store.
    """

    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def save(self, label: str, session: SavedSession) -> None:
        secret = json.dumps({
            'format': FORMAT, 'school': session.school, 'account': session.account,
            'login': session.login, 'saved': int(time.time()),
            'cookies': [[c.name, c.value, c.domain, c.path, c.expires] for c in session.cookies],
        }, separators=(',', ':'))
        if len(secret) > SECRET_LIMIT:
            raise EduPageError('SESSION_CORRUPT')
        try:
            self.backend.set_password(SERVICE, account_key(session.school, label), secret)
        except Exception as exc:
            log_unexpected(exc, 'CREDENTIAL_STORE_WRITE_FAILED')
            raise EduPageError('CREDENTIAL_STORE_WRITE_FAILED') from None

    def load(self, school: str, label: str) -> SavedSession:
        try:
            secret = self.backend.get_password(SERVICE, account_key(school, label))
        except Exception as exc:
            log_unexpected(exc, 'CREDENTIAL_STORE_LOCKED')
            raise EduPageError('CREDENTIAL_STORE_LOCKED') from None
        if secret is None:
            raise EduPageError('NOT_SIGNED_IN')
        try:
            data = json.loads(secret) if len(secret) <= LOAD_LIMIT else None
            if not (isinstance(data, dict) and data.get('format') == FORMAT
                    and data.get('school') == school and isinstance(data.get('cookies'), list)):
                raise ValueError
            cookies = tuple(parse_cookie(*spec) for spec in data['cookies'])
            return SavedSession(school, str(data['account']), str(data['login']), cookies)
        except EduPageError:
            raise
        except Exception:
            raise EduPageError('SESSION_CORRUPT') from None

    def delete(self, school: str, label: str) -> bool:
        try:
            self.backend.delete_password(SERVICE, account_key(school, label))
        except Exception:
            return False
        return True

    def save_remembered(self, label: str, remembered: RememberedAccount) -> None:
        secret = json.dumps({
            'format': REMEMBERED_FORMAT, 'school': remembered.school, 'account': remembered.account,
            'username': remembered.username, 'saved': int(time.time()),
            'cookies': [[c.name, c.value, c.domain, c.path, c.expires] for c in remembered.cookies],
        }, separators=(',', ':'))
        if not remembered.cookies or len(secret) > SECRET_LIMIT:
            raise EduPageError('SESSION_CORRUPT')
        try:
            self.backend.set_password(SERVICE, account_key(remembered.school, label) + REMEMBERED_SUFFIX, secret)
        except Exception as exc:
            log_unexpected(exc, 'CREDENTIAL_STORE_WRITE_FAILED')
            raise EduPageError('CREDENTIAL_STORE_WRITE_FAILED') from None

    def load_remembered(self, school: str, label: str) -> RememberedAccount | None:
        """The Remembered Account for a school, or None when there is none or it can't be read."""
        try:
            secret = self.backend.get_password(SERVICE, account_key(school, label) + REMEMBERED_SUFFIX)
        except Exception as exc:
            log_unexpected(exc, 'CREDENTIAL_STORE_LOCKED')
            return None
        try:
            data = json.loads(secret) if secret and len(secret) <= SECRET_LIMIT else None
            if not (isinstance(data, dict) and data.get('format') == REMEMBERED_FORMAT
                    and data.get('school') == school and isinstance(data.get('cookies'), list)
                    and 0 < len(data['cookies']) <= REMEMBERED_COOKIE_LIMIT
                    and all(isinstance(data.get(key), str) and data[key] for key in ('account', 'username'))):
                return None
            cookies = tuple(parse_cookie(*spec) for spec in data['cookies'])
        except Exception:
            return None
        return RememberedAccount(school, data['account'], data['username'], cookies)

    def delete_remembered(self, school: str, label: str) -> bool:
        try:
            self.backend.delete_password(SERVICE, account_key(school, label) + REMEMBERED_SUFFIX)
        except Exception:
            return False
        return True
