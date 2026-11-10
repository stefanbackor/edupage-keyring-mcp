# SPDX-License-Identifier: GPL-3.0-or-later
"""The tool implementations. Every public method returns a JSON-safe dict."""
from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import requests

from . import meals
from .client import RENEWABLE, SchoolSession, _fold
from .data import (MAX_TIMELINE_DAYS, _name, event_data, grade_data, lesson_data,
                   missing_teacher_names, parse_day, parse_since, plain, recipient_matches,
                   tolerant_grades)
from .errors import SESSION_FAILURES, EduPageError, log_unexpected
from .net import SchoolHttp, is_edupage_host

MAX_MESSAGE_CHARS = 5000
MEAL_TYPES = tuple(meals.MEAL_SLOTS)
# Fixed text for the agent while edupage-api cannot act on the canteen page.
MEAL_ACTIONS_UNAVAILABLE = {
    'reason': 'Ordering, cancelling and rating meals are temporarily unavailable: the edupage-api '
              'library this server uses cannot read the current EduPage canteen page. Nothing was sent.',
    'next_step': 'Tell the person to order, cancel or rate the meal in EduPage itself '
                 '(the Jedálny lístok / menu page). get_meals still shows the menu.',
}
RECIPIENT_RE = re.compile(r'(Teacher|Student|StudentOnly|Rodic)(-?\d{1,12})')

# Timeline groups by EduPage event type code (`typ`).
HOMEWORK_TYPES = {'homework', 'homeworkstudentstav', 'etesthw'}
ASSIGNMENT_TYPES = HOMEWORK_TYPES | {'bexam', 'oexam', 'rexam', 'pexam', 'sexam', 'testing',
                                     'testpridelenie', 'testvysledok'}
ABSENCE_TYPES = {'student_absent', 'ospravedlnenka', 'representation', 'pipnutie'}
EVENT_TYPES = {'event', 'schoolevent', 'excursion', 'trip', 'parentsevening', 'meeting',
               'culture', 'freeday', 'holiday', 'sholiday'}
NEWS_TYPES = {'news'}
MESSAGE_TYPES = {'sprava'}
# Statuses a fresh browser sign-in can fix.
SIGNIN_FIXES = SESSION_FAILURES | {'NOT_SIGNED_IN', 'SESSION_CORRUPT'}
MAX_SIGNIN_WAIT = 50  # seconds per call; stays under common MCP client request timeouts
FILE_BUDGET = 3 * 1024 * 1024
ALLOWED_FILES = (('application/pdf', (b'%PDF-',)), ('image/png', (b'\x89PNG\r\n\x1a\n',)),
                 ('image/jpeg', (b'\xff\xd8\xff',)), ('image/gif', (b'GIF87a', b'GIF89a')))


def sniff(body: bytes) -> str | None:
    """The file type by its leading bytes, if it is one we pass on."""
    for kind, signatures in ALLOWED_FILES:
        if any(body.startswith(sig) for sig in signatures):
            return kind
    return None


@dataclass
class Letter:
    """One listed message: what the model may see, plus file URLs it may not."""
    view: dict
    files: dict[str, tuple[str, str]] = field(default_factory=dict)  # file id -> (name, url)


def message_files(host: str, message_id: str, data: dict) -> dict[str, tuple[str, str]]:
    """File ids for a message's attachments; EduPage keeps them as {url: name} in 'attachements'."""
    listed = data.get('attachements')
    if not isinstance(listed, dict):
        return {}
    files: dict[str, tuple[str, str]] = {}
    for link, name in listed.items():
        if not (isinstance(link, str) and isinstance(name, str)) or len(files) >= 50:
            continue
        absolute = urljoin(f'https://{host}/', link)
        if urlsplit(absolute).scheme == 'https' and is_edupage_host(urlsplit(absolute).hostname or ''):
            files[f'{message_id}.{len(files) + 1}'] = (name[:200], absolute)
    return files

# Library exceptions → fixed status codes. Messages are never passed on.
EXCEPTION_CODES = {
    'NotLoggedInException': 'AUTH_REQUIRED',
    'BadCredentialsException': 'AUTH_REQUIRED',
    'ExpiredSessionException': 'AUTH_REQUIRED',
    'InvalidChildException': 'UNKNOWN_CHILD',
    'NotParentException': 'NOT_A_PARENT_ACCOUNT',
    'InsufficientPermissionsException': 'INSUFFICIENT_PERMISSIONS',
    'MissingDataException': 'DATA_NOT_AVAILABLE',
    'InvalidMealsData': 'MEALS_NOT_AVAILABLE',
    'FailedToChangeMealError': 'MEAL_CHANGE_REJECTED',
    'FailedToRateException': 'MEAL_RATING_REJECTED',
    'InvalidRecipientsException': 'INVALID_RECIPIENT',
    'FailedToParseGradeDataError': 'SCHEMA_CHANGED',
    'InvalidTeacherException': 'SCHEMA_CHANGED',
    'RetryLaterException': 'RATE_LIMITED',
    'UnknownServerError': 'TEMPORARY_ERROR',
    'RequestError': 'EDUPAGE_REJECTED',
}



# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
class FullTools:
    """All edupage-api backed tools for the configured schools."""

    def __init__(self, sessions: list[SchoolSession], download_hosts: set[str] | None = None,
                 signin: Any = None) -> None:
        if not sessions:
            raise ValueError('at least one school')
        self.sessions = {s.school: s for s in sessions}
        self.primary = sessions[0].school
        self.download_hosts = frozenset(download_hosts or ())
        self.signin = signin  # a signin.BrowserSignIn, or None where windows must not open
        self._inbox: dict[str, dict[str, Letter]] = {}  # school -> message id -> Letter

    # -- plumbing ----------------------------------------------------------
    def school(self, name: str) -> SchoolSession:
        if not name:
            return self.sessions[self.primary]
        if name not in self.sessions:
            raise EduPageError('UNKNOWN_SCHOOL', schools=list(self.sessions))
        return self.sessions[name]

    def child(self, child: str, school: str) -> tuple[SchoolSession, dict | None]:
        """Find the school and child for a child-scoped call."""
        candidates = [self.school(school)] if school else list(self.sessions.values())
        known = []
        for session in candidates:
            try:
                with session.lock:
                    found = session.resolve_child(child)
                    options = session.children()
            except Exception as exc:
                if not isinstance(exc, EduPageError):
                    log_unexpected(exc, 'LOCAL_ERROR')
                if school or len(candidates) == 1:
                    if isinstance(exc, EduPageError):
                        raise
                    raise EduPageError('LOCAL_ERROR', error_type=type(exc).__name__) from None
                continue
            if found is not None:
                return session, found
            if session.role != 'parent' and not child:
                return session, None
            known.extend(o['name'] for o in options)
        raise EduPageError('CHILD_REQUIRED' if not child else 'UNKNOWN_CHILD', children=known)

    def run(self, session: SchoolSession | None, fn: Callable[[], dict]) -> dict:
        """Execute `fn`, mapping every failure to a fixed status code.

        A call that finds its open session ended by EduPage runs once more:
        connecting again signs in with the school's Remembered Account, if it has one.
        """
        was_open = session is not None and session.edu is not None
        result = self.run_once(session, fn)
        if was_open and session.renews and result.get('status') in RENEWABLE:
            result = self.run_once(session, fn)
        return result

    def run_once(self, session: SchoolSession | None, fn: Callable[[], dict]) -> dict:
        try:
            if session is None:
                result = fn()
            else:
                with session.lock:
                    result = fn()
            return {'status': 'READY', 'untrusted_content': True, **result}
        except EduPageError as exc:
            if session is not None and exc.code in SESSION_FAILURES:
                session.reset()
            return {'status': exc.code, **exc.extra}
        except requests.RequestException:
            return {'status': 'TEMPORARY_ERROR'}
        except Exception as exc:  # library errors: fixed code + class name only
            name = type(exc).__name__
            code = EXCEPTION_CODES.get(name)
            if code is None:
                code = 'SCHEMA_CHANGED' if isinstance(
                    exc, (ValueError, KeyError, IndexError, AttributeError, TypeError)) else 'EDUPAGE_ERROR'
            if session is not None and code == 'AUTH_REQUIRED':
                session.reset()
            log_unexpected(exc, code)
            return {'status': code, 'error_type': name}

    def scoped(self, school: str, fn: Callable[[SchoolSession, Any], dict]) -> dict:
        try:
            session = self.school(school)
        except EduPageError as exc:
            return {'status': exc.code, **exc.extra}
        return self.run(session, lambda: {'school': session.school, **fn(session, session.connect())})

    def for_child(self, child: str, school: str,
                  fn: Callable[[SchoolSession, Any, dict | None], dict]) -> dict:
        try:
            session, found = self.child(child, school)
        except EduPageError as exc:
            return {'status': exc.code, **exc.extra}
        except Exception as exc:
            log_unexpected(exc, 'LOCAL_ERROR')
            return {'status': 'LOCAL_ERROR', 'error_type': type(exc).__name__}

        def go() -> dict:
            with session.as_child(found['student_id'] if found else None) as edu:
                return {'school': session.school,
                        'child': {k: found[k] for k in ('ref', 'name', 'class')} if found else None,
                        **fn(session, edu, found)}
        return self.run(session, go)

    # -- session & people --------------------------------------------------
    def list_schools(self) -> dict:
        schools = []
        for name, session in self.sessions.items():
            try:
                with session.lock:
                    schools.append({'school': name, 'status': 'READY', 'role': session.role,
                                    'children': len(session.children())})
            except EduPageError as exc:
                schools.append({'school': name, 'status': exc.code})
            except Exception as exc:
                log_unexpected(exc, 'EDUPAGE_ERROR')
                schools.append({'school': name, 'status': 'EDUPAGE_ERROR', 'error_type': type(exc).__name__})
        return {'status': 'READY', 'primary': self.primary, 'schools': schools}

    def list_children(self, school: str = '') -> dict:
        try:
            targets = [self.school(school)] if school else list(self.sessions.values())
        except EduPageError as exc:
            return {'status': exc.code, **exc.extra}
        children, unavailable = [], []
        for session in targets:
            try:
                with session.lock:
                    children.extend(session.children())
            except EduPageError as exc:
                unavailable.append({'school': session.school, 'status': exc.code})
            except Exception as exc:
                log_unexpected(exc, 'LOCAL_ERROR')
                unavailable.append({'school': session.school, 'status': 'LOCAL_ERROR',
                                    'error_type': type(exc).__name__})
        return {'status': 'READY', 'children': children, 'unavailable_schools': unavailable}

    def auth_status(self, school: str = '') -> dict:
        result = self.scoped(school, lambda s, edu: {'role': s.role})
        name = school or self.primary
        if result['status'] != 'READY' and self.signin is not None and name in self.sessions:
            state = 'IN_PROGRESS' if self.signin.running(name) else self.signin.outcome(name)
            if state:
                result['signin'] = state
        return result

    def sign_in(self, school: str = '') -> dict:
        """Open EduPage's sign-in page in Chrome when the saved session no longer works."""
        try:
            session = self.school(school)
        except EduPageError as exc:
            return {'status': exc.code, **exc.extra}
        name = session.school
        if self.signin is None:
            return {'status': 'SIGNIN_UNAVAILABLE', 'school': name}
        if self.signin.running(name):
            return {'status': 'SIGNIN_IN_PROGRESS', 'school': name}
        current = self.auth_status(name)
        if current['status'] == 'READY':
            return {'status': 'ALREADY_SIGNED_IN', 'school': name}
        if current['status'] not in SIGNIN_FIXES:
            return {'status': current['status'], 'school': name}

        def finished() -> None:
            with session.lock:
                session.reset()  # the next call loads the new session from the Credential Store
        code = self.signin.start(name, session.label, finished)
        result: dict[str, Any] = {'status': code, 'school': name}
        if code == 'SIGNIN_STARTED':
            result['next'] = ('A Chrome window is open on EduPage. Tell the person to sign in there '
                              '(including any two-factor step), then call wait_for_sign_in.')
            result['next'] += (' Mention once that ticking "Remember password" in the dialog EduPage shows '
                               'after signing in lets this computer sign in again by itself next time.')
        return result

    def wait_for_sign_in(self, school: str = '', seconds: int = 45) -> dict:
        """Wait for a sign-in started by sign_in to finish, then report the session."""
        try:
            session = self.school(school)
        except EduPageError as exc:
            return {'status': exc.code, **exc.extra}
        name = session.school
        if self.signin is None:
            return {'status': 'SIGNIN_UNAVAILABLE', 'school': name}
        try:
            seconds = max(1, min(int(seconds), MAX_SIGNIN_WAIT))
        except (TypeError, ValueError):
            seconds = 45
        if not self.signin.wait(name, seconds):
            return {'status': 'SIGNIN_IN_PROGRESS', 'school': name,
                    'next': 'The person has not finished signing in yet; call wait_for_sign_in again.'}
        result = self.auth_status(name)
        outcome = self.signin.outcome(name)
        if outcome:
            result['signin'] = outcome
        return result

    def with_next_step(self, result: dict, school: str = '') -> dict:
        """Add a `next` step to a result whose session a fresh sign-in would fix.

        The step asks the agent to get the person's consent before opening a
        sign-in window, so no window opens unasked. `school` is the one the
        tool was called for, if any."""
        if 'next' in result:
            return result
        if result.get('status') in SIGNIN_FIXES:
            only = self.primary if len(self.sessions) == 1 else ''
            schools = [result.get('school') or (school if school in self.sessions else only)]
        else:  # list_children and list_schools report each school's status
            listed = result.get('unavailable_schools') or result.get('schools') or ()
            schools = [s['school'] for s in listed
                       if isinstance(s, dict) and s.get('status') in SIGNIN_FIXES]
        if not schools:
            return result
        named = ', '.join(s for s in schools if s)
        where = f'EduPage ({named})' if named else 'EduPage'
        if self.signin is None:
            login = f'edupage-keyring-mcp login {schools[0] or "<school>"}'
            step = (f'The person is not signed in to {where}. Tell them to sign in by running '
                    f'`{login}` in a terminal, then try again.')
        else:
            target = f'sign_in for that school ({named})' if named else 'sign_in for that school'
            step = (f'The person is not signed in to {where}. Ask them whether to open the EduPage '
                    f'sign-in page in Chrome. Only if they agree, call {target}, then wait_for_sign_in.')
        return {**result, 'next': step}

    def user_id(self, school: str = '') -> dict:
        return self.scoped(school, lambda s, edu: {'user_id': edu.get_user_id(), 'role': s.role})

    def school_year(self, school: str = '') -> dict:
        return self.scoped(school, lambda s, edu: {'school_year': edu.get_school_year()})

    def find_student(self, name: str, school: str = '') -> dict:
        needle = _fold(name)
        if len(needle) < 2:
            return {'status': 'NAME_TOO_SHORT'}

        def go(session: SchoolSession, edu: Any) -> dict:
            people = (edu.get_all_students() if session.role == 'parent' else edu.get_students()) or []
            matches = []
            for person in people:
                label = getattr(person, 'name', None) or getattr(person, 'name_short', '') or ''
                if needle in _fold(label):
                    matches.append({'student_id': person.person_id, 'name': label,
                                    'class_id': getattr(person, 'class_id', None)})
            return {'matches': matches[:20], 'total_matches': len(matches)}
        return self.scoped(school, go)

    def roster(self, kind: str, school: str = '') -> dict:
        getters = {'students': 'get_students', 'all_students': 'get_all_students',
                   'teachers': 'get_teachers', 'classes': 'get_classes',
                   'classrooms': 'get_classrooms', 'subjects': 'get_subjects'}
        method = getters[kind]
        return self.scoped(school, lambda s, edu: {kind: plain(getattr(edu, method)() or [])})

    # -- timetables --------------------------------------------------------
    def get_timetable(self, child: str = '', day: str = '', school: str = '') -> dict:
        try:
            when = parse_day(day)
        except EduPageError as exc:
            return {'status': exc.code}

        def go(session: SchoolSession, edu: Any, found: dict | None) -> dict:
            table = edu.get_my_timetable(when)
            return {'date': when.isoformat(), 'lessons': [lesson_data(l) for l in (table.lessons if table else [])]}
        return self.for_child(child, school, go)

    def get_week_timetable(self, child: str = '', week_of: str = '', school: str = '') -> dict:
        try:
            anchor = parse_day(week_of)
        except EduPageError as exc:
            return {'status': exc.code}
        if not week_of and anchor.weekday() >= 5:
            anchor += timedelta(days=7 - anchor.weekday())
        monday = anchor - timedelta(days=anchor.weekday())

        def go(session: SchoolSession, edu: Any, found: dict | None) -> dict:
            days = []
            for offset in range(5):
                day = monday + timedelta(days=offset)
                table = edu.get_my_timetable(day)
                days.append({'date': day.isoformat(),
                             'lessons': [lesson_data(l) for l in (table.lessons if table else [])]})
            return {'monday': monday.isoformat(), 'days': days}
        return self.for_child(child, school, go)

    def get_timetable_for(self, target_type: str, target_id: str, day: str = '', school: str = '') -> dict:
        lookups = {'teacher': ('get_teachers', 'person_id'), 'student': ('get_students', 'person_id'),
                   'class': ('get_classes', 'class_id'), 'classroom': ('get_classrooms', 'classroom_id')}
        if target_type not in lookups:
            return {'status': 'INVALID_TARGET_TYPE', 'allowed': list(lookups)}
        try:
            when = parse_day(day)
        except EduPageError as exc:
            return {'status': exc.code}
        method, attr = lookups[target_type]

        def go(session: SchoolSession, edu: Any) -> dict:
            target = next((t for t in getattr(edu, method)() or []
                           if str(getattr(t, attr, '')) == str(target_id)), None)
            if target is None:
                raise EduPageError('UNKNOWN_TARGET')
            table = edu.get_timetable(target, when)
            return {'target': f'{target_type}:{target_id}', 'date': when.isoformat(),
                    'lessons': [lesson_data(l) for l in (table.lessons if table else [])]}
        return self.scoped(school, go)

    def get_periods(self, school: str = '') -> dict:
        def go(session: SchoolSession, edu: Any) -> dict:
            rows = edu.data.get('zvonenia') or []
            return {'periods': [{'period': r.get('period') or r.get('id'), 'start': r.get('starttime'),
                                 'end': r.get('endtime')} for r in rows if isinstance(r, dict)]}
        return self.scoped(school, go)

    def get_next_ringing_time(self, at: str = '', school: str = '') -> dict:
        try:
            moment = datetime.fromisoformat(at.replace('Z', '+00:00')) if at else datetime.now()
            if moment.tzinfo is not None:  # school bells are in local time
                moment = moment.astimezone().replace(tzinfo=None)
        except (TypeError, ValueError):
            return {'status': 'INVALID_DATE'}
        if abs((moment.date() - date.today()).days) > 400:
            return {'status': 'DATE_OUT_OF_RANGE'}

        def go(session: SchoolSession, edu: Any) -> dict:
            rows = edu.data.get('zvonenia')
            # The library recurses forever on an empty bell schedule.
            if not isinstance(rows, list) or not rows or not all(
                    isinstance(r, dict) and r.get('starttime') and r.get('endtime') for r in rows):
                raise EduPageError('DATA_NOT_AVAILABLE')
            return {'next': plain(edu.get_next_ringing_time(moment))}
        return self.scoped(school, go)

    def get_timetable_changes(self, day: str = '', school: str = '') -> dict:
        try:
            when = parse_day(day, back=30, ahead=30)
        except EduPageError as exc:
            return {'status': exc.code}
        return self.scoped(school, lambda s, edu: {
            'date': when.isoformat(), 'changes': plain(edu.get_timetable_changes(when) or [])})

    def get_missing_teachers(self, day: str = '', school: str = '') -> dict:
        try:
            when = parse_day(day, back=30, ahead=30)
        except EduPageError as exc:
            return {'status': exc.code}
        def go(session: SchoolSession, edu: Any) -> dict:
            try:
                names = [_name(t) for t in edu.get_missing_teachers(when) or []]
                return {'date': when.isoformat(), 'teachers': names}
            except Exception as exc:
                if type(exc).__name__ != 'InvalidTeacherException':
                    raise
            # The library fails when a listed name is not in its teacher roster
            # (e.g. double spaces). Read the same header without matching.
            from edupage_api.substitution import Substitution
            page = Substitution(edu)._Substitution__get_substitution_data(when)
            return {'date': when.isoformat(), 'teachers': missing_teacher_names(page),
                    'parser': 'tolerant'}
        return self.scoped(school, go)

    # -- grades ------------------------------------------------------------
    def get_grades(self, child: str = '', year: int = 0, term: str = '', school: str = '') -> dict:
        if term not in ('', 'FIRST', 'SECOND') or (bool(year) != bool(term)):
            return {'status': 'INVALID_TERM', 'hint': 'Give both year (e.g. 2026) and term FIRST|SECOND, or neither.'}
        if year and not 2000 <= year <= date.today().year + 1:
            return {'status': 'INVALID_TERM'}

        def go(session: SchoolSession, edu: Any, found: dict | None) -> dict:
            from edupage_api import Term
            try:
                grades = edu.get_grades_for_term(year, Term[term]) if term else edu.get_grades()
                return {'grades': [grade_data(g) for g in grades or []]}
            except (AttributeError, KeyError, TypeError, ValueError):
                # The library aborts the whole list on one incomplete grade row.
                # Fetch the same page through the library and read it row by row.
                from edupage_api.grades import Grades
                module = Grades(edu)
                raw = (module._Grades__get_grade_data_for_term(Term[term], year) if term
                       else module._Grades__get_grade_data())
                rows, skipped = tolerant_grades(edu.data, raw)
                return {'grades': rows, 'skipped_rows': skipped, 'parser': 'tolerant'}
        return self.for_child(child, school, go)

    # -- timeline ----------------------------------------------------------
    def timeline(self, kinds: set[str] | None, child: str = '', since: str = '',
                 school: str = '') -> dict:
        try:
            start = parse_since(since)
        except EduPageError as exc:
            return {'status': exc.code}

        def collect(session: SchoolSession, edu: Any, only: dict | None) -> dict:
            # A parent's timeline covers every child, even after switch_to_child,
            # so items are matched to children by their recipient instead.
            kids = session.children()
            events = edu.get_notification_history(start) or []
            rows = []
            for event in events:
                kind = plain(event.event_type) if event.event_type is not None else None
                if kinds is not None and kind not in kinds:
                    continue
                row = event_data(event)
                row['for_children'] = [k['first_name'] for k in kids if recipient_matches(row, k)]
                if only is not None and row['for_children'] and only['first_name'] not in row['for_children']:
                    continue  # explicitly for a sibling
                rows.append(row)
            rows.sort(key=lambda r: r['timestamp'] or '', reverse=True)
            return {'since': start.isoformat(), 'items': rows[:500]}

        if child:
            return self.timeline_for_child(child, school, collect)
        return self.scoped(school, lambda s, edu: collect(s, edu, None))

    def get_notifications(self, since: str = '', child: str = '', types: str = '',
                          school: str = '') -> dict:
        kinds = {t.strip() for t in types.split(',') if t.strip()} or None
        if kinds and any(not re.fullmatch(r'[a-z_0-9]{1,30}', k) for k in kinds):
            return {'status': 'INVALID_TYPES'}
        return self.timeline(kinds, child, since, school)

    # -- meals -------------------------------------------------------------
    # get_meals reads the canteen page itself (meals.py). choose_meal, sign_off_meal
    # and rate_meal still go through edupage-api, which 0.13.3 cannot do on the
    # current page; they then answer MEAL_ACTIONS_UNAVAILABLE and send nothing.
    @staticmethod
    def _load_meals(edu: Any, school: str, day: date) -> Any:
        """edupage-api's meals for a day, ready for its choose/sign_off/rate calls.

        Raises:
            EduPageError: MEAL_ACTIONS_UNAVAILABLE when the canteen page has this
                day's meals but edupage-api cannot read it; MEALS_NOT_AVAILABLE
                when the school has no canteen data.
        """
        try:
            listed = edu.get_meals(day)
        except Exception as exc:
            if not isinstance(exc, (AttributeError, KeyError, TypeError, ValueError)) \
                    and type(exc).__name__ != 'InvalidMealsData':
                raise
            listed = None
        if listed is None:
            if any(meals.load_day(edu, school, day).values()):  # our own reader still sees meals
                raise EduPageError('MEAL_ACTIONS_UNAVAILABLE', **MEAL_ACTIONS_UNAVAILABLE)
            raise EduPageError('MEALS_NOT_AVAILABLE')
        return listed

    def _meal(self, edu: Any, school: str, day: date, meal_type: str) -> Any:
        meal = getattr(self._load_meals(edu, school, day), meal_type, None)
        if meal is None:
            raise EduPageError('MEALS_NOT_AVAILABLE')
        return meal

    def get_meals(self, child: str = '', day: str = '', school: str = '') -> dict:
        """Read from the canteen page directly; edupage-api 0.13.3 cannot parse it (see meals.py)."""
        try:
            when = parse_day(day, back=7, ahead=30)
        except EduPageError as exc:
            return {'status': exc.code}

        def go(session: SchoolSession, edu: Any, found: dict | None) -> dict:
            day_meals = meals.load_day(edu, session.school, when)
            return {'date': when.isoformat(),
                    'meals': {t: m.view() if m else None for t, m in day_meals.items()}}
        return self.for_child(child, school, go)

    def _meal_args(self, day: str, meal_type: str) -> date:
        if meal_type not in MEAL_TYPES:
            raise EduPageError('INVALID_MEAL_TYPE', allowed=list(MEAL_TYPES))
        return parse_day(day, back=0, ahead=30)

    def choose_meal(self, child: str, day: str, meal_type: str, number: int, school: str = '') -> dict:
        try:
            when = self._meal_args(day, meal_type)
        except EduPageError as exc:
            return {'status': exc.code, **exc.extra}

        def go(session: SchoolSession, edu: Any, found: dict | None) -> dict:
            meal = self._meal(edu, session.school, when, meal_type)
            letters = 'ABCDEFGH'
            if not 1 <= number <= len(letters) or letters[number - 1] not in (meal.chooseable_menus or []):
                raise EduPageError('INVALID_MENU_NUMBER', choosable=plain(meal.chooseable_menus))
            meal.choose(edu, number)
            return {'ordered': True, 'date': when.isoformat(), 'meal_type': meal_type,
                    'menu': letters[number - 1]}
        return self.for_child(child, school, go)

    def sign_off_meal(self, child: str, day: str, meal_type: str, school: str = '') -> dict:
        try:
            when = self._meal_args(day, meal_type)
        except EduPageError as exc:
            return {'status': exc.code, **exc.extra}

        def go(session: SchoolSession, edu: Any, found: dict | None) -> dict:
            self._meal(edu, session.school, when, meal_type).sign_off(edu)
            return {'ordered': False, 'date': when.isoformat(), 'meal_type': meal_type}
        return self.for_child(child, school, go)

    def rate_meal(self, child: str, day: str, meal_type: str, quality: int, quantity: int,
                  school: str = '') -> dict:
        # edupage-api always submits ratings for the lunch slot ("jedlo_dna": "2").
        if meal_type != 'lunch':
            return {'status': 'INVALID_MEAL_TYPE', 'allowed': ['lunch']}
        if not (1 <= quality <= 5 and 1 <= quantity <= 5):
            return {'status': 'INVALID_RATING'}
        try:
            when = parse_day(day, back=14, ahead=0)
        except EduPageError as exc:
            return {'status': exc.code}

        def go(session: SchoolSession, edu: Any, found: dict | None) -> dict:
            meal = self._meal(edu, session.school, when, meal_type)
            rating = next((m.rating for m in meal.menus or [] if m.rating is not None), None)
            if rating is None:
                raise EduPageError('RATING_NOT_AVAILABLE')
            rating.rate(edu, quantity, quality)
            return {'rated': True, 'date': when.isoformat(), 'meal_type': meal_type}
        return self.for_child(child, school, go)

    # -- messages: reading ---------------------------------------------------
    def list_messages(self, since: str = '', child: str = '', school: str = '') -> dict:
        """Who wrote what and when, with a snippet; the full text comes from get_message."""
        try:
            start = parse_since(since)
        except EduPageError as exc:
            return {'status': exc.code}

        def read_inbox(session: SchoolSession, edu: Any, only: dict | None) -> dict:
            family = session.children()
            box = self._inbox.setdefault(session.school, {})
            summary = []
            for item in edu.get_notification_history(start) or []:
                if plain(item.event_type) not in MESSAGE_TYPES:
                    continue
                view = event_data(item)
                kids = [k['first_name'] for k in family if recipient_matches(view, k)]
                if only is not None and kids and only['first_name'] not in kids:
                    continue
                data = item.additional_data if isinstance(item.additional_data, dict) else {}
                mid = str(item.event_id)
                files = message_files(session.host, mid, data)
                receipt = str(data.get('receipt', '')).strip().lower() not in ('', '0', 'false', 'none')
                box[mid] = Letter({'id': mid, 'sent': view['timestamp'], 'from': view['author'],
                                   'to': view['recipient'], 'for_children': kids,
                                   'text': view['text'], 'wants_receipt': receipt,
                                   'files': [{'file': fid, 'name': n} for fid, (n, _) in files.items()]},
                                  files)
                summary.append({'id': mid, 'sent': view['timestamp'], 'from': view['author'],
                                'to': view['recipient'], 'for_children': kids,
                                'snippet': (view['text'] or '')[:160], 'files': len(files),
                                'wants_receipt': receipt})
            summary.sort(key=lambda m: m['sent'] or '', reverse=True)
            return {'since': start.isoformat(), 'messages': summary[:500]}

        if child:
            return self.timeline_for_child(child, school, read_inbox)
        return self.scoped(school, lambda s, edu: read_inbox(s, edu, None))

    def _letter(self, school: str, message_id: str) -> tuple[SchoolSession, Letter]:
        session = self.school(school)
        letter = self._inbox.get(session.school, {}).get(str(message_id))
        if letter is None:
            raise EduPageError('MESSAGE_NOT_LISTED', hint='Call list_messages for that period first.')
        return session, letter

    def get_message(self, message_id: str, school: str = '') -> dict:
        try:
            session, letter = self._letter(school, message_id)
        except EduPageError as exc:
            return {'status': exc.code, **exc.extra}
        return {'status': 'READY', 'untrusted_content': True, 'school': session.school, **letter.view}

    def get_attachment(self, message_id: str, file: str, school: str = '') -> dict:
        try:
            session, letter = self._letter(school, message_id)
        except EduPageError as exc:
            return {'status': exc.code, **exc.extra}
        if file not in letter.files:
            return {'status': 'FILE_NOT_LISTED'}
        name, url = letter.files[file]

        def fetch(s: SchoolSession, edu: Any) -> dict:
            # Own session so the library's session never gains file-server hosts.
            with SchoolHttp({s.host} | set(self.download_hosts), budget=FILE_BUDGET) as http:
                http.cookies.update(edu.session.cookies)
                try:
                    reply = http.get(url)
                except EduPageError as exc:
                    if exc.code == 'RESPONSE_LIMIT':
                        raise EduPageError('FILE_TOO_LARGE') from None
                    if exc.code == 'HOST_NOT_ALLOWED':
                        raise EduPageError('REDIRECT_NOT_ALLOWED', hint=(
                            'The file is served from another EduPage host; allow it with '
                            '--download-host <host>.edupage.org.')) from None
                    raise
            if urlsplit(reply.url).path.startswith('/login'):
                raise EduPageError('AUTH_REQUIRED')
            if reply.status_code != 200:
                raise EduPageError('DOWNLOAD_FAILED')
            kind = sniff(reply.content)
            if kind is None:
                raise EduPageError('FILE_TYPE_NOT_ALLOWED')
            return {'name': name, 'content_type': kind, 'size': len(reply.content),
                    'sha256': hashlib.sha256(reply.content).hexdigest(),
                    'data_base64': base64.b64encode(reply.content).decode('ascii')}
        return self.scoped(school, fetch)

    def timeline_for_child(self, child: str, school: str,
                           fn: Callable[[SchoolSession, Any, dict | None], dict]) -> dict:
        """Run a timeline read filtered to one child (no session switching needed)."""
        try:
            session, found = self.child(child, school)
        except EduPageError as exc:
            return {'status': exc.code, **exc.extra}
        except Exception as exc:
            log_unexpected(exc, 'LOCAL_ERROR')
            return {'status': 'LOCAL_ERROR', 'error_type': type(exc).__name__}
        return self.run(session, lambda: {
            'school': session.school,
            'child': {k: found[k] for k in ('ref', 'name', 'class')} if found else None,
            **fn(session, session.connect(), found)})

    # -- messages: sending ---------------------------------------------------
    def send_message(self, recipient: str, body: str, school: str = '') -> dict:
        match = RECIPIENT_RE.fullmatch(recipient or '')
        if not match:
            return {'status': 'INVALID_RECIPIENT', 'hint': 'Use an ID like Teacher123 from get_teachers.'}
        if not body or not body.strip() or len(body) > MAX_MESSAGE_CHARS:
            return {'status': 'INVALID_BODY'}
        kind, person_id = match.groups()
        group = {'Teacher': 'teachers', 'Student': 'students', 'StudentOnly': 'students',
                 'Rodic': 'parents'}[kind]

        def go(session: SchoolSession, edu: Any) -> dict:
            dbi = edu.data.get('dbi') if isinstance(edu.data.get('dbi'), dict) else {}
            people = dbi.get(group) if isinstance(dbi.get(group), dict) else {}
            if person_id not in people:
                raise EduPageError('UNKNOWN_RECIPIENT')
            timeline_id = edu.send_message([recipient], body)
            return {'sent': True, 'timeline_id': timeline_id}
        return self.scoped(school, go)


