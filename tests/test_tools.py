# SPDX-License-Identifier: GPL-3.0-or-later
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from conftest import FakeEdu, event, reply, tools_for
from edupage_keyring_mcp.data import missing_teacher_names, recipient_matches, tolerant_grades
from edupage_keyring_mcp.errors import EduPageError


def test_child_tools_name_the_child_and_mark_content_untrusted():
    edu = FakeEdu()
    result = tools_for(('demo-school', edu)).get_timetable('ema', '2026-11-03')
    assert result['status'] == 'READY' and result['untrusted_content'] is True
    assert result['child'] == {'ref': 'demo-school/12', 'name': 'Ema Vzorová', 'class': '1.Y'}
    assert result['lessons'][0]['subject'] == 'Matematika'
    assert edu.switched == [12]


def test_which_child_must_be_clear():
    tools = tools_for(('demo-school', FakeEdu()))
    assert tools.get_timetable() == {'status': 'CHILD_REQUIRED', 'children': ['Šimon Vzorový', 'Ema Vzorová']}
    assert tools.get_timetable('Jozef')['status'] == 'UNKNOWN_CHILD'


def test_library_failures_never_leak_text():
    edu = FakeEdu()
    edu.error = KeyError('<html>secret page</html>')
    assert tools_for(('demo-school', edu)).get_timetable('Ema') == {
        'status': 'SCHEMA_CHANGED', 'error_type': 'KeyError'}
    expired = type('NotLoggedInException', (Exception,), {})
    edu.error = expired('token=abc')
    tools = tools_for(('demo-school', edu))
    assert tools.get_timetable('Ema') == {'status': 'AUTH_REQUIRED', 'error_type': 'NotLoggedInException'}
    assert tools.sessions['demo-school'].edu is None


def test_unexpected_failures_are_logged_without_their_text(capsys):
    edu = FakeEdu()
    edu.error = OSError(5, 'secret strerror', '/Users/someone/child-name.pdf')
    assert tools_for(('demo-school', edu)).get_timetable('Ema') == {
        'status': 'EDUPAGE_ERROR', 'error_type': 'OSError'}
    log = capsys.readouterr().err
    assert log.startswith('edupage-keyring-mcp: EDUPAGE_ERROR from OSError (errno 5 EIO)\n')
    assert 'tools.py:' in log and ' in run' in log
    assert 'secret' not in log and 'child-name' not in log
    crashed = tools_for(('demo-school', RuntimeError('private keyring detail')))
    assert crashed.list_children()['unavailable_schools'][0]['status'] == 'LOCAL_ERROR'
    log = capsys.readouterr().err
    assert 'LOCAL_ERROR from RuntimeError' in log and 'private' not in log


def test_schools_are_independent():
    tools = tools_for(('demo-school', FakeEdu()), ('second-school', EduPageError('NOT_SIGNED_IN')))
    assert {s['school']: s['status'] for s in tools.list_schools()['schools']} == {
        'demo-school': 'READY', 'second-school': 'NOT_SIGNED_IN'}
    listing = tools.list_children()
    assert len(listing['children']) == 2
    assert listing['unavailable_schools'] == [{'school': 'second-school', 'status': 'NOT_SIGNED_IN'}]
    assert tools.get_timetable('Ema')['status'] == 'READY'
    assert tools.user_id('second-school') == {'status': 'NOT_SIGNED_IN'}
    assert tools.user_id('elsewhere')['status'] == 'UNKNOWN_SCHOOL'
    assert tools.list_children('elsewhere')['status'] == 'UNKNOWN_SCHOOL'
    crashed = tools_for(('demo-school', RuntimeError('private keyring detail')))
    assert crashed.get_timetable('Ema') == {'status': 'LOCAL_ERROR', 'error_type': 'RuntimeError'}


def test_timeline_tags_children_and_filters_siblings():
    edu = FakeEdu()
    edu.events = [event(1, 'homework', '9.Z · Fyzika', '', {'nazov': 'Workbook p. 10', 'date': '2026-11-05'}),
                  event(2, 'homework', '1.Y · Čítanie', 'Read p. 4'),
                  event(3, 'pipnutie', 'Ema Vzorová', 'Arrived 07:55'),
                  event(4, 'news', '*', 'School news'),
                  event(5, 'homework', '9.ZA · Chémia', 'Not our class')]
    tools = tools_for(('demo-school', edu))
    every = tools.timeline(None)
    assert {i['id']: i['for_children'] for i in every['items']} == {
        1: ['Šimon'], 2: ['Ema'], 3: ['Ema'], 4: [], 5: []}
    homework = tools.timeline({'homework'}, child='Šimon')
    assert [(i['id'], i['text']) for i in homework['items']] == [
        (5, 'Not our class'), (1, 'Workbook p. 10')]
    assert edu.switched == []  # filtering needs no account switch
    old = (date.today() - timedelta(days=91)).isoformat()
    assert tools.timeline(None, since=old)['status'] == 'DATE_OUT_OF_RANGE'


def test_recipient_matching_uses_whole_class_tokens():
    child = {'name': 'Ema Vzorová', 'class': '1.Y'}
    assert recipient_matches({'recipient': 'Rodičia 1.Y'}, child)
    assert recipient_matches({'recipient': 'ema vzorova'}, child)
    assert not recipient_matches({'recipient': '1.YZ · Fyzika'}, child)
    assert not recipient_matches({'recipient': '*'}, child)


def test_grades_survive_rows_the_library_rejects(monkeypatch):
    raw = {'vsetkyZnamky': [{'udalostid': '1', 'datum': '2026-11-02 10:00:00', 'data': '2 (diktát)'},
                            {'udalostid': '2', 'datum': '2026-11-01 10:00:00', 'data': '1'}, None],
           'vsetkyUdalosti': {'edupage': {'1': {'PredmetID': '9', 'UcitelID': '55', 'p_meno': 'Diktát',
                                                'p_typ_udalosti': '1', 'p_vaha': '20'}}}}
    userhome = {'dbi': {'subjects': {'9': {'name': 'Slovenčina'}},
                        'teachers': {'55': {'firstname': 'Hana', 'lastname': 'Učiteľka'}}}}
    rows, skipped = tolerant_grades(userhome, raw)
    assert skipped == 2 and rows[0]['subject'] == 'Slovenčina' and rows[0]['grade'] == 2.0
    assert rows[0]['comment'] == 'diktát' and rows[0]['teacher'] == 'Hana Učiteľka' and rows[0]['weight'] == 1.0

    import edupage_api.grades as library
    edu = FakeEdu()
    edu.get_grades = lambda: None.missing  # the library's own crash
    monkeypatch.setattr(library.Grades, '_Grades__get_grade_data', lambda self: raw, raising=False)
    result = tools_for(('demo-school', edu)).get_grades('Ema')
    assert result['status'] == 'READY' and result['parser'] == 'tolerant' and len(result['grades']) == 1


def test_missing_teachers_without_roster_lookup():
    page = ('<span class="print-font-resizable">Chýbajúci: Eva Malá, Jana  Veľká  (1 - 3), '
            'Ivan Horný + Mária Dolná</span>')
    assert missing_teacher_names(page) == ['Eva Malá', 'Jana Veľká', 'Ivan Horný', 'Mária Dolná']
    assert missing_teacher_names(None) == []


def test_send_message_checks_recipient_and_body():
    edu = FakeEdu()
    tools = tools_for(('demo-school', edu))
    for recipient, body, status in [('https://x', 'hi', 'INVALID_RECIPIENT'), ('Teacher56', 'hi', 'UNKNOWN_RECIPIENT'),
                                    ('Teacher55', ' ', 'INVALID_BODY'), ('Teacher55', 'x' * 5001, 'INVALID_BODY')]:
        assert tools.send_message(recipient, body)['status'] == status
    assert edu.sent == []
    assert tools.send_message('Teacher55', 'Dobrý deň')['timeline_id'] == 999
    assert edu.sent == [(['Teacher55'], 'Dobrý deň')]


def test_meal_tools(web):
    web.routes['https://demo-school.edupage.org/menu/'] = lambda r: reply(r, 200, b'<html></html>')  # no canteen
    edu = FakeEdu()
    tools = tools_for(('demo-school', edu))
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    assert tools.choose_meal('Ema', tomorrow, 'lunch', 1)['status'] == 'MEALS_NOT_AVAILABLE'
    edu.get_meals = lambda day: None.canteen
    assert tools.choose_meal('Ema', tomorrow, 'lunch', 1)['status'] == 'MEALS_NOT_AVAILABLE'
    picked = []
    lunch = SimpleNamespace(chooseable_menus=['A', 'B'], menus=[], title='Obed', served_from=None,
                            served_to=None, ordered_meal=None, can_be_changed_until=None,
                            choose=lambda e, n: picked.append(n), sign_off=lambda e: picked.append('off'))
    edu.get_meals = lambda day: SimpleNamespace(snack=None, lunch=lunch, afternoon_snack=None)
    assert tools.choose_meal('Ema', tomorrow, 'dinner', 1)['status'] == 'INVALID_MEAL_TYPE'
    assert tools.choose_meal('Ema', '2020-01-01', 'lunch', 1)['status'] == 'DATE_OUT_OF_RANGE'
    assert tools.choose_meal('Ema', tomorrow, 'lunch', 3)['status'] == 'INVALID_MENU_NUMBER'
    assert tools.choose_meal('Ema', tomorrow, 'lunch', 2)['menu'] == 'B'
    assert tools.sign_off_meal('Ema', tomorrow, 'lunch')['ordered'] is False
    assert picked == [2, 'off']
    assert tools.rate_meal('Ema', date.today().isoformat(), 'snack', 3, 3)['status'] == 'INVALID_MEAL_TYPE'
    assert tools.rate_meal('Ema', date.today().isoformat(), 'lunch', 6, 1)['status'] == 'INVALID_RATING'


def test_bell_tools_guard_against_bad_data():
    edu = FakeEdu()
    tools = tools_for(('demo-school', edu))
    assert tools.get_next_ringing_time('2026-11-03T07:30:00Z')['status'] == 'READY'
    assert tools.get_next_ringing_time('1990-01-01T08:00')['status'] == 'DATE_OUT_OF_RANGE'
    edu.data['zvonenia'] = []
    assert tools.get_next_ringing_time()['status'] == 'DATA_NOT_AVAILABLE'


class Browser:
    """Records sign-in launches instead of opening Chrome."""

    def __init__(self):
        self.started, self.active, self.last = [], set(), None

    def running(self, school):
        return school in self.active

    def outcome(self, school):
        return self.last

    def wait(self, school, seconds):
        self.waited = seconds
        return school not in self.active

    def finish(self, outcome):
        self.on_exit()
        self.active.clear()
        self.last = outcome

    def start(self, school, label, on_exit):
        self.started.append(school)
        self.active.add(school)
        self.on_exit = on_exit
        return 'SIGNIN_STARTED'


def test_sign_in_opens_a_window_only_when_the_session_is_gone():
    from conftest import school_session
    from edupage_keyring_mcp.tools import FullTools
    browser = Browser()
    working = FakeEdu()
    tools = FullTools([school_session(working, 'demo-school'),
                       school_session(EduPageError('NOT_SIGNED_IN'), 'second-school'),
                       school_session(EduPageError('CREDENTIAL_STORE_LOCKED'), 'third-school')], signin=browser)
    assert tools.sign_in() == {'status': 'ALREADY_SIGNED_IN', 'school': 'demo-school'}
    assert tools.sign_in('third-school')['status'] == 'CREDENTIAL_STORE_LOCKED'
    assert tools.sign_in('elsewhere')['status'] == 'UNKNOWN_SCHOOL'
    assert browser.started == []
    result = tools.sign_in('second-school')
    assert result['status'] == 'SIGNIN_STARTED' and 'wait_for_sign_in' in result['next']
    assert 'ticking "Remember password"' in result['next']  # EduPage's own dialog; nothing to type here
    assert tools.sign_in('second-school')['status'] == 'SIGNIN_IN_PROGRESS'
    assert tools.auth_status('second-school')['signin'] == 'IN_PROGRESS'
    assert browser.started == ['second-school']


def test_a_finished_sign_in_drops_the_old_session():
    from conftest import school_session
    from edupage_keyring_mcp.tools import FullTools
    browser = Browser()
    session = school_session(EduPageError('SESSION_EXPIRED'))
    tools = FullTools([session], signin=browser)
    assert tools.sign_in()['status'] == 'SIGNIN_STARTED'
    session.edu = object()  # whatever was connected before the new sign-in
    browser.on_exit()
    assert session.edu is None  # the next call loads the new Saved Session from the Credential Store


def test_without_a_browser_sign_in_is_unavailable():
    tools = tools_for(('demo-school', EduPageError('NOT_SIGNED_IN')))
    assert tools.sign_in() == {'status': 'SIGNIN_UNAVAILABLE', 'school': 'demo-school'}


def test_wait_for_sign_in_reports_progress_then_the_new_session():
    from conftest import school_session
    from edupage_keyring_mcp.tools import FullTools
    browser, edu = Browser(), FakeEdu()
    session = school_session(EduPageError('SESSION_EXPIRED'))
    tools = FullTools([session], signin=browser)
    assert tools.sign_in()['status'] == 'SIGNIN_STARTED'
    pending = tools.wait_for_sign_in(seconds=999)
    assert pending['status'] == 'SIGNIN_IN_PROGRESS' and browser.waited == 50
    session._loader = lambda name, label: school_session(edu)._loader(name, label)
    session._opener = lambda saved: edu
    browser.finish('SAVED')
    done = tools.wait_for_sign_in()
    assert done['status'] == 'READY' and done['signin'] == 'SAVED' and done['role'] == 'parent'


def test_wait_for_sign_in_passes_on_a_failed_sign_in():
    from conftest import school_session
    from edupage_keyring_mcp.tools import FullTools
    browser = Browser()
    tools = FullTools([school_session(EduPageError('NOT_SIGNED_IN'))], signin=browser)
    tools.sign_in()
    browser.finish('SIGNIN_TIMEOUT')
    assert tools.wait_for_sign_in() == {'status': 'NOT_SIGNED_IN', 'signin': 'SIGNIN_TIMEOUT'}


def test_session_failures_ask_before_opening_a_sign_in():
    from conftest import school_session
    from edupage_keyring_mcp.tools import FullTools
    browser = Browser()
    tools = FullTools([school_session(FakeEdu(), 'demo-school'),
                       school_session(EduPageError('NOT_SIGNED_IN'), 'second-school')], signin=browser)
    failed = tools.with_next_step(tools.user_id('second-school'), 'second-school')
    assert failed['status'] == 'NOT_SIGNED_IN'
    assert 'Ask them whether' in failed['next'] and 'Only if they agree' in failed['next']
    assert 'sign_in for that school (second-school)' in failed['next']
    listing = tools.with_next_step(tools.list_children())
    assert listing['status'] == 'READY' and '(second-school)' in listing['next']
    assert 'next' not in tools.with_next_step(tools.user_id('demo-school'))
    assert 'next' not in tools.with_next_step(tools.user_id('elsewhere'))
    assert browser.started == []  # asking never opens a window


def test_without_a_browser_the_next_step_is_the_terminal_login():
    tools = tools_for(('demo-school', EduPageError('SESSION_EXPIRED')))
    result = tools.with_next_step(tools.user_id())
    assert '`edupage-keyring-mcp login demo-school`' in result['next'] and 'sign_in' not in result['next']
