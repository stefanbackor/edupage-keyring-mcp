# SPDX-License-Identifier: GPL-3.0-or-later
"""Sign in again through EduPage's own "Remember password", without a password.

After a successful sign-in EduPage asks "Remember login credentials?". When the
person ticks "Remember password" there, EduPage sets two long-lived cookies
(`edusrs`, `egdid`) and from then on lists that account as remembered to a
browser that sends them: its sign-in page gets a short token for the account
and posts it where the password would go. This module does the same over
`SchoolHttp`, so the cookies only ever go to the school's own host. No password
is read, stored or sent (ADR 0002).

A Remembered Account is used only when a tool call finds the Saved Session
ended: never by the keep-alive, never on a timer. A sign-in that works replaces
the Saved Session. When EduPage no longer lists the account, refuses the token
or asks for a two-factor step, the Remembered Account is forgotten and the
person signs in in a Chrome window as before. Each attempt writes one line to
the server log, e.g. `remembered-sign-in <school> READY`.
"""
from __future__ import annotations

import time
from typing import Any, Callable

import requests

from .credential_store import (Cookie, RememberedAccount, SavedSession, SessionStore, parse_cookie,
                               session_cookies)
from .errors import EduPageError, log_line, log_unexpected
from .net import SchoolHttp, school_host

SESSION_COOKIE = 'PHPSESSID'
# The cookies EduPage's "Remember password" works by; nothing else is kept.
REMEMBER_COOKIES = ('edusrs', 'egdid')
# After a failure that keeps the Remembered Account (a captcha, a page we can't read),
# wait before trying that school again: repeated sign-ins make a captcha worse.
RETRY_SECONDS = 15 * 60
FORM = {'Content-Type': 'application/x-www-form-urlencoded'}


def remember_cookies(host: str, jar: Any) -> tuple[Cookie, ...]:
    """EduPage's remember cookies for `host` out of a cookie jar (requests' or Playwright's)."""
    found = {}
    for item in jar:
        name, value, domain, path, expires = (
            item.get(key) if isinstance(item, dict) else getattr(item, key, None)
            for key in ('name', 'value', 'domain', 'path', 'expires'))
        if name not in REMEMBER_COOKIES or str(domain or '').lstrip('.') not in (host, 'edupage.org'):
            continue
        try:
            found[name] = parse_cookie(name, value, domain, path or '/', expires or -1)
        except EduPageError:
            continue
    return tuple(found[name] for name in REMEMBER_COOKIES if name in found)


def open_sign_in(school: str, cookies: tuple[Cookie, ...]) -> Any:
    """A signed-out edupage-api client that sends only the remember cookies, to the school host only."""
    from edupage_api import Edupage

    host = school_host(school)
    edu = Edupage()
    edu.session.close()
    edu.session = SchoolHttp({host})
    for cookie in session_cookies(host, list(cookies)):
        edu.session.cookies.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path, secure=True,
                                expires=int(cookie.expires) if cookie.expires > 0 else None)
    return edu


def remembered_user(edu: Any, school: str, username: str = '') -> dict | None:
    """The account EduPage lists as remembered with its password for this school, if any.

    Raises:
        EduPageError: SCHEMA_CHANGED when EduPage's answer can't be read.
    """
    from edupage_api.compression import RequestData
    from edupage_api.login_session import LoginSession

    base = f'https://{school_host(school)}/login/?cmd=MainLogin'
    edu.session.get(base)
    answer = edu.session.post(base + '&akcia=getData', headers=FORM,
                              data=RequestData.encode_request_body({'rpcparams': '{}'}))
    data = LoginSession.parse_rpc_response(answer.text) if answer.status_code == 200 else None
    if not isinstance(data, dict):
        raise EduPageError('SCHEMA_CHANGED')
    users = data.get('storedUsers')
    for user in users if isinstance(users, list) else ():
        if (isinstance(user, dict) and user.get('ep') == school and isinstance(user.get('em'), str)
                and user['em'] and isinstance(user.get('ph'), str) and user['ph']
                and username in ('', user['em'])):
            return user
    return None


def remembered_username(school: str, cookies: tuple[Cookie, ...]) -> str | None:
    """The login name EduPage remembers a password for with these cookies, right after a sign-in."""
    if not cookies:
        return None
    try:
        edu = open_sign_in(school, cookies)
    except EduPageError:
        return None
    try:
        user = remembered_user(edu, school)
        return user['em'] if user else None
    except (EduPageError, requests.RequestException):
        return None
    except Exception as exc:
        log_unexpected(exc, 'EDUPAGE_ERROR')
        return None
    finally:
        edu.session.close()


def library_sign_in(remembered: RememberedAccount) -> tuple[str, Cookie, tuple[Cookie, ...]] | str:
    """Sign in the way EduPage's page does for a remembered account.

    Returns the account ID, the new session cookie and the remember cookies as
    they are now, or the code of what stopped it.

    Raises:
        EduPageError: TEMPORARY_ERROR when EduPage could not be reached.
    """
    from edupage_api.exceptions import BadCredentialsException, CaptchaException

    host = school_host(remembered.school)
    try:
        edu = open_sign_in(remembered.school, remembered.cookies)
    except EduPageError:
        return 'REMEMBERED_ACCOUNT_GONE'  # the cookies have expired
    try:
        try:
            user = remembered_user(edu, remembered.school, remembered.username)
            if user is None:
                return 'REMEMBERED_ACCOUNT_GONE'
            second_factor = edu.login(user['em'], user['ph'], remembered.school)
        except BadCredentialsException:
            return 'REMEMBERED_ACCOUNT_REJECTED'
        except CaptchaException:
            return 'CAPTCHA_REQUIRED'
        except requests.RequestException:
            raise EduPageError('TEMPORARY_ERROR') from None
        if second_factor is not None:
            return 'TWO_FACTOR_REQUIRED'
        account = str((edu.data or {}).get('userid') or '')
        for cookie in edu.session.cookies:
            if cookie.name == SESSION_COOKIE and str(cookie.domain).lstrip('.') == host:
                session = parse_cookie(cookie.name, cookie.value, cookie.domain, cookie.path or '/',
                                       cookie.expires or -1)
                return account, session, remember_cookies(host, edu.session.cookies)
        return 'SCHEMA_CHANGED'  # signed in, but EduPage issued no session cookie
    finally:
        edu.session.close()


class RememberedSignIn:
    """Turns a school's Remembered Account into a new Saved Session; None when it can't."""

    # EduPage's answer means the Remembered Account will not work next time either.
    FORGET = frozenset({'REMEMBERED_ACCOUNT_GONE', 'REMEMBERED_ACCOUNT_REJECTED', 'TWO_FACTOR_REQUIRED',
                        'ACCOUNT_MISMATCH'})

    def __init__(self, store: SessionStore,
                 sign_in: Callable[[RememberedAccount], Any] = library_sign_in,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.store, self._sign_in, self._clock = store, sign_in, clock
        self._not_before: dict[tuple[str, str], float] = {}

    def __call__(self, school: str, label: str) -> SavedSession | None:
        key = (school, label)
        if self._clock() < self._not_before.get(key, 0):
            return None
        remembered = self.store.load_remembered(school, label)
        if remembered is None:
            return None
        try:
            outcome = self._sign_in(remembered)
        except EduPageError as exc:
            log_line(f'remembered-sign-in {school} {exc.code}')
            if exc.code == 'TEMPORARY_ERROR':
                raise
            self._not_before[key] = self._clock() + RETRY_SECONDS  # e.g. a redirect off the school host
            return None
        except Exception as exc:
            log_unexpected(exc, 'EDUPAGE_ERROR')
            outcome = 'EDUPAGE_ERROR'
        if not isinstance(outcome, str):
            account, cookie, current = outcome
            if account == remembered.account:
                session = SavedSession(school, account, remembered.username, (cookie,))
                self.store.save(label, session)
                if len(current) == len(remembered.cookies) and current != remembered.cookies:
                    self._keep(label, RememberedAccount(school, account, remembered.username, current))
                log_line(f'remembered-sign-in {school} READY')
                return session
            outcome = 'ACCOUNT_MISMATCH'  # EduPage now remembers a different account
        if outcome in self.FORGET:
            self.store.delete_remembered(school, label)
        else:
            self._not_before[key] = self._clock() + RETRY_SECONDS
        log_line(f'remembered-sign-in {school} {outcome}')
        return None

    def _keep(self, label: str, renewed: RememberedAccount) -> None:
        """Store cookies EduPage has just reissued; the sign-in stands even if that fails."""
        try:
            self.store.save_remembered(label, renewed)
        except EduPageError:
            pass
