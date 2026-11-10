# SPDX-License-Identifier: GPL-3.0-or-later
"""edupage-api clients opened from Saved Sessions in the Credential Store, one per school."""
from __future__ import annotations

import html
import re
import threading
import unicodedata
from contextlib import contextmanager
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

import requests

from .errors import SESSION_FAILURES, EduPageError, log_unexpected
from .credential_store import SavedSession, session_cookies
from .net import SchoolHttp, school_host

ACCOUNT_ROW = re.compile(r'data-rowid="edupage;([^";]+);([^";]+);[^"]*"')
# EduPage ended the session: a Remembered Account, where the person has one, can open a new one.
RENEWABLE = frozenset({'AUTH_REQUIRED', 'SESSION_EXPIRED'})


def get_page(edu: Any, url: str) -> str:
    """A signed-in page from the school host, as text.

    Raises:
        EduPageError: AUTH_REQUIRED when EduPage no longer accepts the session,
            TEMPORARY_ERROR on network failures and other error statuses.
    """
    try:
        response = edu.session.get(url)
    except EduPageError as exc:
        # An expired session is redirected to EduPage's central login host.
        raise EduPageError('AUTH_REQUIRED' if exc.code == 'HOST_NOT_ALLOWED' else exc.code) from None
    except requests.RequestException:
        raise EduPageError('TEMPORARY_ERROR') from None
    if response.status_code in (401, 403) or urlsplit(response.url).path.startswith('/login'):
        raise EduPageError('AUTH_REQUIRED')
    if response.status_code != 200:
        raise EduPageError('TEMPORARY_ERROR')
    return response.content.decode('utf-8', errors='replace')


def fetch_userhome(edu: Any, school: str) -> str:
    """Load `/user/` into `edu.data`; return the page so callers can read more from it.

    Raises:
        EduPageError: AUTH_REQUIRED when EduPage no longer accepts the session,
            SCHEMA_CHANGED when the page is not what edupage-api expects.
    """
    from edupage_api.login_session import LoginSession

    page = get_page(edu, f'https://{school_host(school)}/user/')
    if 'userhome(' not in page:
        raise EduPageError('AUTH_REQUIRED')
    try:
        LoginSession(edu).parse_login_data(page)
    except Exception:
        raise EduPageError('SCHEMA_CHANGED') from None
    if not isinstance(edu.data, dict) or not edu.data.get('userid'):
        raise EduPageError('SCHEMA_CHANGED')
    edu.subdomain = school
    return page


def reported_login(page: str, school: str) -> str | None:
    """The login name EduPage itself shows for this school in its account switcher."""
    for tag in re.findall(r'<a [^>]*edubarChangeEdurowBtn[^>]*>', page or ''):
        match = ACCOUNT_ROW.search(tag)
        if match and match.group(1) == school and 'selected' in tag:
            return html.unescape(match.group(2))
    return None


def open_edupage(saved: SavedSession) -> Any:
    """An `edupage_api.Edupage` logged in with saved cookies, confined to the school host."""
    from edupage_api import Edupage

    host = school_host(saved.school)
    edu = Edupage()
    edu.session.close()
    edu.session = SchoolHttp({host})
    for cookie in session_cookies(host, list(saved.cookies)):
        edu.session.cookies.set(cookie.name, cookie.value, domain=cookie.domain,
                                path=cookie.path, secure=True)
    fetch_userhome(edu, saved.school)
    edu.username = saved.login or None  # informational; nothing here switches accounts by login
    return edu


def fold(text: str) -> str:
    """Case- and accent-insensitive form: 'Šimon' and 'simon' fold alike."""
    decomposed = unicodedata.normalize('NFKD', text or '')
    return ''.join(c for c in decomposed if not unicodedata.combining(c)).casefold().strip()


_fold = fold  # used by sibling modules


class SchoolSession:
    """A lazily opened edupage-api client for one school, guarded by one lock.

    `label` names the Credential Store entry when one school has several accounts; it
    is empty for the usual one account per school. `renew` signs in again with
    the school's Remembered Account and returns the new Saved Session, or None
    when there is none or EduPage no longer accepts it.
    """

    def __init__(self, school: str, label: str,
                 loader: Callable[[str, str], SavedSession],
                 opener: Callable[[SavedSession], Any] = open_edupage,
                 lock: Any = None,
                 renew: Callable[[str, str], SavedSession | None] | None = None) -> None:
        self.school = school
        self.label = label
        self.host = school_host(school)
        self._loader, self._opener, self._renew = loader, opener, renew
        self.lock = lock if lock is not None else threading.RLock()
        self.edu: Any = None
        self.saved: SavedSession | None = None

    def connect(self) -> Any:
        if self.edu is not None:
            return self.edu
        try:
            saved = self._loader(self.school, self.label)
            edu = self._opener(saved)
        except EduPageError as exc:
            if self._renew is None or exc.code not in RENEWABLE:
                raise
            renewed = self._renew(self.school, self.label)
            if renewed is None:
                raise
            saved, edu = renewed, self._opener(renewed)
        if str(edu.data.get('userid')) != saved.account:
            edu.session.close()
            raise EduPageError('ACCOUNT_MISMATCH')
        self.saved, self.edu = saved, edu
        return edu

    @property
    def renews(self) -> bool:
        """Whether an expired session may be replaced by signing in with a Remembered Account."""
        return self._renew is not None

    def keep_alive(self) -> str | None:
        """Load `/user/` if this session is open, so EduPage doesn't end it for inactivity.

        Returns READY, the failure code, or None when the session isn't open. Never
        opens one: no Credential Store read, no sign-in, no use of a Remembered Account. A session failure drops the
        session until a tool call connects it again.
        """
        with self.lock:
            if self.edu is None:
                return None
            try:
                fetch_userhome(self.edu, self.school)
            except EduPageError as exc:
                code = exc.code
            except Exception as exc:
                code = 'LOCAL_ERROR'
                log_unexpected(exc, code)  # class and frames only, as for tool calls
            else:
                return 'READY'
            if code in SESSION_FAILURES:
                self.reset()
            return code

    def reset(self) -> None:
        if self.edu is not None:
            try:
                self.edu.session.close()
            except Exception:
                pass
        self.edu = None

    @property
    def role(self) -> str:
        account = str(self.connect().data.get('userid', ''))
        return ('parent' if account.startswith('Rodic') else
                'student' if account.startswith('Student') else 'teacher')

    def children(self) -> list[dict]:
        """A parent's children, or the student themself."""
        data = self.connect().data
        db = data.get('dbi') if isinstance(data.get('dbi'), dict) else {}
        pupils = db.get('students') if isinstance(db.get('students'), dict) else {}
        classes = db.get('classes') if isinstance(db.get('classes'), dict) else {}
        role = self.role
        if role == 'parent':
            ids = [str(i) for i in data.get('parentStudentids') or [] if isinstance(i, (str, int))]
        elif role == 'student':
            ids = [str(data['userid'])[len('Student'):]]
        else:
            ids = []
        found = []
        for position, pupil_id in enumerate(ids, 1):
            pupil = pupils.get(pupil_id) if isinstance(pupils.get(pupil_id), dict) else {}
            given = str(pupil.get('firstname') or '').strip() or f'Child {position}'
            full = ' '.join(filter(None, [str(pupil.get('firstname') or '').strip(),
                                          str(pupil.get('lastname') or '').strip()])) or given
            group = classes.get(str(pupil.get('classid'))) if pupil.get('classid') else None
            found.append({'ref': f'{self.school}/{pupil_id}', 'student_id': pupil_id,
                          'name': full, 'first_name': given,
                          'class': group.get('name') if isinstance(group, dict) else None,
                          'school': self.school})
        return found

    def resolve_child(self, child: str) -> dict | None:
        """Find one child by ref, student ID, full name or first name."""
        options = self.children()
        if not child:
            return options[0] if len(options) == 1 else None
        wanted = fold(str(child))
        exact = [o for o in options if wanted in (o['ref'].casefold(), o['student_id'].casefold())]
        if exact:
            return exact[0]
        for field in ('name', 'first_name'):
            hits = [o for o in options if fold(o[field]) == wanted]
            if len(hits) == 1:
                return hits[0]
        return None

    @contextmanager
    def as_child(self, child_id: str | None) -> Iterator[Any]:
        """Select one child of a parent account for the block.

        EduPage keeps the selected child in the session, and some pages (grades,
        menus) read whichever child is selected. So every child-scoped call
        selects its own child first, under the school's lock, and nothing reads
        child data without doing so. Parent-level pages (timeline, messages,
        sending) are unaffected by the selection, as checked against a live
        account, so there is no switch back. If selecting fails, the session is
        dropped rather than trusted.
        """
        edu = self.connect()
        if child_id is None or self.role != 'parent':
            yield edu
            return
        try:
            edu.switch_to_child(int(child_id))
        except Exception:
            self.reset()  # the selection may be half applied server-side
            raise
        try:
            yield edu
        finally:
            edu._selected_child_id = None  # edupage-api's own memory of the selection
