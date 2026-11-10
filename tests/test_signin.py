# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from conftest import HOST, LABEL, SCHOOL, FakeEdu, account_page, reply
from edupage_keyring_mcp import signin
from edupage_keyring_mcp.errors import EduPageError
from edupage_keyring_mcp.credential_store import Cookie, RememberedAccount, SessionStore
from test_credential_store import MemoryCredentialStore


class Chrome:
    """Just enough of Playwright's sync API for the sign-in flow."""

    def __init__(self, final_url, jar, fail=None):
        self.final_url, self.jar, self.fail = final_url, jar, fail
        self.visited, self.closed = [], False

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    chromium = property(lambda self: self)

    def launch(self, channel, headless):
        assert (channel, headless) == ('chrome', False)
        return self

    def new_context(self):
        return self

    def new_page(self):
        return self

    def goto(self, url, wait_until):
        self.visited.append(url)

    def wait_for_url(self, predicate, timeout):
        if self.fail:
            raise self.fail
        assert not predicate(f'https://{HOST}/login/twofactor')
        assert not predicate('https://example.com/user/')
        assert predicate(self.final_url)

    def cookies(self):
        return self.jar

    def close(self):
        self.closed = True


def jar_cookie(name, value, domain=HOST):
    return {'name': name, 'value': value, 'domain': domain, 'path': '/', 'expires': -1}


def test_the_session_cookie_is_taken_only_after_sign_in_and_only_for_the_school():
    jar = [{'name': 'PHPSESSID', 'value': 'abc', 'domain': HOST, 'path': '/', 'expires': -1, 'httpOnly': True},
           jar_cookie('lang', 'sk', '.edupage.org'),
           jar_cookie('_ga', '1', '.google.com'),
           jar_cookie('odd', 'bell\x07')]
    chrome = Chrome(f'https://{HOST}/user/', jar)
    assert signin.capture_cookies(SCHOOL, 60, chrome) == (Cookie('PHPSESSID', 'abc', HOST), ())
    assert chrome.visited == [f'https://{HOST}/'] and chrome.closed


def test_only_the_session_cookie_is_saved(web):
    jar = [jar_cookie('edusrs', 's1'), jar_cookie('PHPSESSID', 'abc'), jar_cookie('egdid', 'd1', '.edupage.org'),
           jar_cookie('hsid', 'h1'), jar_cookie('erfp', 'r1'),
           jar_cookie('PHPSESSID', 'elsewhere', 'other.edupage.org')]
    web.routes[f'https://{HOST}/user/'] = lambda r: reply(r, 200, account_page())
    ring = MemoryCredentialStore()
    signin.sign_in(SCHOOL, store=SessionStore(ring), playwright=Chrome(f'https://{HOST}/user/', jar))
    assert SessionStore(ring).load(SCHOOL, '').cookies == (Cookie('PHPSESSID', 'abc', HOST),)


def test_a_window_without_the_session_cookie_saves_nothing(web):
    jar = [jar_cookie('edusrs', 's1'), jar_cookie('hsid', 'h1'), jar_cookie('erfp', 'r1'),
           jar_cookie('PHPSESSID', 'shared', '.edupage.org'),
           jar_cookie('PHPSESSID', 'elsewhere', 'other.edupage.org')]
    ring = MemoryCredentialStore()
    with pytest.raises(EduPageError, match='^SCHEMA_CHANGED$'):
        signin.sign_in(SCHOOL, store=SessionStore(ring), playwright=Chrome(f'https://{HOST}/user/', jar))
    assert ring.secrets == {}


@pytest.mark.parametrize('failure, code', [(type('TimeoutError', (Exception,), {})(), 'SIGNIN_TIMEOUT'),
                                           (RuntimeError('Target closed'), 'SIGNIN_ABORTED')])
def test_browser_problems_become_codes(failure, code):
    chrome = Chrome('', [], fail=failure)
    with pytest.raises(EduPageError, match=f'^{code}$'):
        signin.capture_cookies(SCHOOL, 60, chrome)
    assert chrome.closed


def test_sign_in_stores_the_login_edupage_reports(web):
    chrome = Chrome(f'https://{HOST}/user/', [jar_cookie('PHPSESSID', 'abc')])
    web.routes[f'https://{HOST}/user/'] = lambda r: reply(r, 200, account_page(login='parent.real@example.invalid'))
    ring = MemoryCredentialStore()
    result = signin.sign_in(SCHOOL, store=SessionStore(ring), playwright=chrome)
    assert result == {'status': 'SAVED', 'school': SCHOOL, 'role': 'parent', 'remembered': False}
    saved = SessionStore(ring).load(SCHOOL, '')
    assert saved.account == 'Rodic100' and saved.login == 'parent.real@example.invalid'


REMEMBER_JAR = [jar_cookie('PHPSESSID', 'abc'),
                {'name': 'edusrs', 'value': 'synthetic-remembering', 'domain': '.edupage.org', 'path': '/',
                 'expires': 4102444800.5, 'httpOnly': True},
                {'name': 'egdid', 'value': 'synthetic-device', 'domain': '.edupage.org', 'path': '/',
                 'expires': 4102444800.5, 'httpOnly': True},
                jar_cookie('edusrs', 'other-school', 'other.edupage.org'), jar_cookie('edusrs', 'x', '.example.com'),
                jar_cookie('hsid', 'h1'), jar_cookie('erfp', 'r1')]
REMEMBER = (Cookie('edusrs', 'synthetic-remembering', '.edupage.org', '/', 4102444800.5),
            Cookie('egdid', 'synthetic-device', '.edupage.org', '/', 4102444800.5))


def signed_in(web, jar, confirm, ring=None):
    web.routes[f'https://{HOST}/user/'] = lambda r: reply(r, 200, account_page())
    ring = MemoryCredentialStore() if ring is None else ring
    asked = []

    def confirming(school, cookies):
        asked.append((school, cookies))
        return confirm
    result = signin.sign_in(SCHOOL, store=SessionStore(ring), confirm=confirming,
                            playwright=Chrome(f'https://{HOST}/user/', jar))
    return result, SessionStore(ring), asked


def test_edupages_remember_cookies_are_taken_from_the_window_and_nothing_else():
    session, remember = signin.capture_cookies(SCHOOL, 60, Chrome(f'https://{HOST}/user/', REMEMBER_JAR))
    assert session == Cookie('PHPSESSID', 'abc', HOST) and remember == REMEMBER


def test_a_ticked_remember_password_keeps_a_remembered_account(web):
    result, store, asked = signed_in(web, REMEMBER_JAR, confirm='parent.real@example.invalid')
    assert result == {'status': 'SAVED', 'school': SCHOOL, 'role': 'parent', 'remembered': True}
    assert asked == [(SCHOOL, REMEMBER)]  # EduPage is asked with the remember cookies alone
    assert store.load_remembered(SCHOOL, '') == RememberedAccount(
        SCHOOL, 'Rodic100', 'parent.real@example.invalid', REMEMBER)
    assert store.load(SCHOOL, '').cookies == (Cookie('PHPSESSID', 'abc', HOST),)  # the session stays as small


def test_without_the_tick_nothing_is_kept_and_an_earlier_one_is_forgotten(web):
    ring = MemoryCredentialStore()
    SessionStore(ring).save_remembered('', RememberedAccount(SCHOOL, 'Rodic100', 'u', REMEMBER))
    result, store, _ = signed_in(web, REMEMBER_JAR, confirm=None, ring=ring)  # EduPage lists no remembered password
    assert result['remembered'] is False and store.load_remembered(SCHOOL, '') is None
    assert list(ring.secrets) == [('edupage-keyring-mcp', SCHOOL)]


def test_a_window_without_remember_cookies_does_not_ask_edupage(web):
    web.routes[f'https://{HOST}/user/'] = lambda r: reply(r, 200, account_page())
    ring = MemoryCredentialStore()
    result = signin.sign_in(SCHOOL, store=SessionStore(ring),
                            playwright=Chrome(f'https://{HOST}/user/', [jar_cookie('PHPSESSID', 'abc')]))
    assert result['remembered'] is False
    assert set(web.calls) == {f'https://{HOST}/user/'}  # only the check of the new session


def test_a_remembered_account_that_cannot_be_stored_does_not_fail_the_sign_in(web):
    class SessionsOnly(MemoryCredentialStore):
        def set_password(self, service, account, secret):
            if account.endswith(':remembered'):
                raise OSError('store full')
            super().set_password(service, account, secret)
    result, store, _ = signed_in(web, REMEMBER_JAR, confirm='u', ring=SessionsOnly())
    assert result['status'] == 'SAVED' and result['remembered'] is False
    assert store.load(SCHOOL, '').account == 'Rodic100'


class Child:
    """A finished-on-demand stand-in for the `login` child process."""

    def __init__(self, output=b'', returncode=0):
        import threading
        self.output, self.returncode, self.done = output, returncode, threading.Event()

    def communicate(self):
        assert self.done.wait(5)
        return self.output, None


@pytest.mark.parametrize('output, returncode, code', [
    (b'Opening...\n{"status": "SAVED", "school": "x"}\n', 0, 'SAVED'),
    (b'{"status": "SIGNIN_TIMEOUT"}\n', 1, 'SIGNIN_TIMEOUT'),
    (b'{"status": "SAVED"}\n', 1, 'SIGNIN_FAILED'),
    (b'{"status": "<html>page</html>"}\n', 1, 'SIGNIN_FAILED'),
    (b'Traceback ...\n', 1, 'SIGNIN_FAILED'),
    (b'', -9, 'SIGNIN_FAILED'),
])
def test_only_a_status_code_comes_back_from_login(output, returncode, code):
    assert signin.login_outcome(output, returncode) == code


def test_one_browser_sign_in_per_school_at_a_time():
    started, finished = [], []
    child = Child(b'{"status": "SAVED"}\n')

    def spawn(school, label):
        started.append((school, label))
        return child
    browser = signin.BrowserSignIn(spawn)
    assert browser.start(SCHOOL, LABEL, lambda: finished.append(SCHOOL)) == 'SIGNIN_STARTED'
    assert browser.start(SCHOOL, LABEL, lambda: None) == 'SIGNIN_IN_PROGRESS'
    assert browser.running(SCHOOL) and started == [(SCHOOL, LABEL)]
    assert browser.wait(SCHOOL, 0.05) is False  # still signing in
    child.done.set()
    assert browser.wait(SCHOOL, 5) is True
    assert finished == [SCHOOL] and browser.outcome(SCHOOL) == 'SAVED'


def test_a_launch_failure_is_a_code():
    def spawn(school, label):
        raise FileNotFoundError('/secret/path')
    browser = signin.BrowserSignIn(spawn)
    assert browser.start(SCHOOL, LABEL, lambda: None) == 'SIGNIN_START_FAILED'
    assert not browser.running(SCHOOL)


def test_the_login_child_never_shares_the_mcp_channel(monkeypatch):
    seen = {}
    monkeypatch.setattr(signin.subprocess, 'Popen', lambda argv, **kw: seen.update(argv=argv, **kw))
    signin.spawn_login(SCHOOL, LABEL)
    assert seen['argv'][1:] == ['-m', 'edupage_keyring_mcp', 'login', SCHOOL]
    assert seen['stdout'] == signin.subprocess.PIPE and seen['stdin'] == signin.subprocess.DEVNULL
    assert seen['env']['EDUPAGE_USERNAME'] == LABEL and LABEL not in seen['argv']
    monkeypatch.setenv('EDUPAGE_USERNAME', 'someone@example.invalid')
    signin.spawn_login(SCHOOL, '')
    assert 'EDUPAGE_USERNAME' not in seen['env']  # the child uses the same Credential Store entry
