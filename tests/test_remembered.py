# SPDX-License-Identifier: GPL-3.0-or-later
"""Signing in again from a Remembered Account, against the real library and a fake EduPage."""
import base64
import json
import zlib
from email.message import Message
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest
import requests

from conftest import COOKIE, HOST, SAVED, SCHOOL, FakeEdu, account_page, reply
from edupage_keyring_mcp import remembered as module
from edupage_keyring_mcp.client import SchoolSession
from edupage_keyring_mcp.credential_store import Cookie, RememberedAccount, SavedSession, SessionStore
from edupage_keyring_mcp.errors import EduPageError
from edupage_keyring_mcp.remembered import RETRY_SECONDS, RememberedSignIn, remembered_username
from edupage_keyring_mcp.tools import FullTools
from test_credential_store import MemoryCredentialStore

BASE = f'https://{HOST}'
RPC = f'{BASE}/login/?cmd=MainLogin'
LOGIN_NAME = 'parent@example.invalid'
TOKEN = 'syn-tok'  # what EduPage hands its page in place of the password
REMEMBER = (Cookie('edusrs', 'synthetic-remembering', '.edupage.org', '/', 4102444800.0),
            Cookie('egdid', 'synthetic-device', '.edupage.org', '/', 4102444800.0))
REMEMBERED = RememberedAccount(SCHOOL, 'Rodic100', LOGIN_NAME, REMEMBER)
LOGIN_FORM = b'<script>var props = {"csrftoken":"synthetic-csrf"};</script>'
TWO_FACTOR_FORM = (b'<input name="csrfauth" value="c"><input name="au" value="a">'
                   b'<input name="gu" value="g">')
LISTED = {'euid': 'synthetic', 'ep': SCHOOL, 'em': LOGIN_NAME, 'ph': TOKEN, 'fn': 'P', 'ln': 'V', 'ty': 'Rodic'}


def rpc_params(request):
    """The parameters of one of EduPage's RPC calls, as the library encoded them."""
    packed = parse_qs(request.body)['eqap'][0][len('dz:'):]
    form = zlib.decompress(base64.b64decode(packed), -15).decode()
    return json.loads(parse_qs(form)['rpcparams'][0])


def with_cookies(response, **cookies):
    headers = Message()
    for name, value in cookies.items():
        headers['Set-Cookie'] = f'{name}={value}; path=/; secure; HttpOnly'
    response.raw._original_response = SimpleNamespace(msg=headers, close=lambda: None, isclosed=lambda: True)
    return response


def edupage(web, listed=(LISTED,), after_sign_in='/user/', userid='Rodic100', reissue=None):
    """EduPage's sign-in RPC. It lists remembered accounts only to a browser sending the remember cookies."""
    seen = SimpleNamespace(sign_ins=[], cookie_headers=[])

    def get_data(request):
        seen.cookie_headers.append(request.headers.get('Cookie', ''))
        known = 'edusrs=synthetic-remembering' in request.headers.get('Cookie', '')
        return reply(request, 200, json.dumps({'school_id': SCHOOL, 'storedUsers': list(listed) if known else []}).encode())

    def sign_in(request):
        params = rpc_params(request)
        seen.sign_ins.append(params)
        if params['password'] != TOKEN or params['userToken'] != 'synthetic-csrf-token':
            return reply(request, 200, json.dumps({'status': 'fail', 'err': {'error_id': 'bad'}}).encode())
        answer = reply(request, 200, json.dumps({'status': 'OK', 'redirectUrl': after_sign_in}).encode())
        return with_cookies(answer, PHPSESSID='fresh-session', **(reissue or {}))
    web.routes.update({
        RPC: lambda r: reply(r, 200, LOGIN_FORM),
        RPC + '&akcia=getData': get_data,
        RPC + '&akcia=getToken': lambda r: reply(r, 200, b'{"token":"synthetic-csrf-token"}'),
        RPC + '&akcia=login': sign_in,
        f'{BASE}/login/edubarLogin.php': lambda r: reply(r, 302, headers={'Location': '/login/?bad=1'}),
        f'{BASE}/login/?bad=1': lambda r: reply(r, 200, LOGIN_FORM),
        f'{BASE}/login/twofactor': lambda r: reply(r, 200, TWO_FACTOR_FORM),
        f'{BASE}/user/': lambda r: reply(r, 200, account_page(userid)),
    })
    return seen


def remembering(remembered=REMEMBERED):
    store = SessionStore(MemoryCredentialStore())
    store.save('', SAVED)
    if remembered is not None:
        store.save_remembered('', remembered)
    return store


def test_a_remembered_account_opens_a_new_saved_session_without_a_password(web, capsys):
    seen = edupage(web)
    store = remembering()
    renewed = RememberedSignIn(store)(SCHOOL, '')
    assert renewed == SavedSession(SCHOOL, 'Rodic100', LOGIN_NAME, (Cookie('PHPSESSID', 'fresh-session', HOST),))
    assert store.load(SCHOOL, '') == renewed and store.load_remembered(SCHOOL, '') == REMEMBERED
    [sent] = seen.sign_ins  # what EduPage's own page sends for a remembered account
    assert (sent['username'], sent['password']) == (LOGIN_NAME, TOKEN)
    assert all(url.startswith(BASE + '/') for url in web.calls)  # the cookies go to the school only
    assert 'PHPSESSID' not in seen.cookie_headers[0]  # asked as a signed-out browser
    log = capsys.readouterr()
    assert log.err == f'edupage-keyring-mcp: remembered-sign-in {SCHOOL} READY\n' and log.out == ''
    assert TOKEN not in ''.join(store.backend.secrets.values())  # EduPage's token is used once, never kept


def test_cookies_edupage_reissues_replace_the_remembered_ones(web):
    edupage(web, reissue={'edusrs': 'synthetic-remembering-2'})
    store = remembering()
    assert RememberedSignIn(store)(SCHOOL, '') is not None
    kept = store.load_remembered(SCHOOL, '')
    assert [(c.name, c.value) for c in kept.cookies] == [('edusrs', 'synthetic-remembering-2'),
                                                        ('egdid', 'synthetic-device')]
    assert (kept.account, kept.username) == ('Rodic100', LOGIN_NAME)


@pytest.mark.parametrize('world, code', [
    (dict(listed=()), 'REMEMBERED_ACCOUNT_GONE'),                                   # removed at EduPage
    (dict(listed=({**LISTED, 'ph': ''},)), 'REMEMBERED_ACCOUNT_GONE'),              # username only
    (dict(listed=({**LISTED, 'ep': 'other-school'},)), 'REMEMBERED_ACCOUNT_GONE'),
    (dict(listed=({**LISTED, 'em': 'someone.else@example.invalid'},)), 'REMEMBERED_ACCOUNT_GONE'),
    (dict(listed=({**LISTED, 'ph': 'stale'},)), 'REMEMBERED_ACCOUNT_REJECTED'),
    (dict(after_sign_in='/login/twofactor'), 'TWO_FACTOR_REQUIRED'),
    (dict(userid='Ucitel7'), 'ACCOUNT_MISMATCH'),
])
def test_a_remembered_account_edupage_no_longer_honours_is_forgotten(web, capsys, world, code):
    edupage(web, **world)
    store = remembering()
    assert RememberedSignIn(store)(SCHOOL, '') is None
    assert store.load(SCHOOL, '') == SAVED and store.load_remembered(SCHOOL, '') is None
    assert capsys.readouterr().err == f'edupage-keyring-mcp: remembered-sign-in {SCHOOL} {code}\n'


def test_expired_remember_cookies_are_forgotten_without_asking_edupage(web):
    old = tuple(Cookie(c.name, c.value, c.domain, c.path, 1000.0) for c in REMEMBER)
    store = remembering(RememberedAccount(SCHOOL, 'Rodic100', LOGIN_NAME, old))
    assert RememberedSignIn(store)(SCHOOL, '') is None
    assert web.calls == [] and store.load_remembered(SCHOOL, '') is None


def test_an_answer_that_cannot_be_read_keeps_it_and_waits(web, capsys):
    seen = edupage(web)
    web.routes[RPC + '&akcia=getData'] = lambda r: reply(r, 500, b'oops')
    now = [1000.0]
    store = remembering()
    renew = RememberedSignIn(store, clock=lambda: now[0])
    assert renew(SCHOOL, '') is None and store.load_remembered(SCHOOL, '') == REMEMBERED
    assert f'remembered-sign-in {SCHOOL} SCHEMA_CHANGED' in capsys.readouterr().err
    asked = len(web.calls)
    now[0] += RETRY_SECONDS - 1
    assert renew(SCHOOL, '') is None and len(web.calls) == asked  # not asked again yet
    now[0] += 2
    web.routes[RPC + '&akcia=getData'] = lambda r: reply(r, 200, json.dumps({'storedUsers': [LISTED]}).encode())
    assert renew(SCHOOL, '') is not None and len(seen.sign_ins) == 1


def test_without_a_remembered_account_edupage_is_not_contacted(web):
    assert RememberedSignIn(remembering(remembered=None))(SCHOOL, '') is None
    assert web.calls == []


def test_a_network_failure_is_temporary_and_keeps_everything(web):
    def offline(request):
        raise requests.ConnectionError('unreachable')
    web.routes[f'{BASE}/login/'] = offline
    store = remembering()
    renew = RememberedSignIn(store)
    for _ in range(2):  # no waiting period: the next call may try again
        with pytest.raises(EduPageError, match='^TEMPORARY_ERROR$'):
            renew(SCHOOL, '')
    assert store.load_remembered(SCHOOL, '') == REMEMBERED


def test_a_sign_in_that_leads_off_the_school_host_is_not_followed(web, capsys):
    edupage(web, after_sign_in='https://login1.edupage.org/user/')
    store = remembering()
    assert RememberedSignIn(store)(SCHOOL, '') is None
    assert not any('login1' in url for url in web.calls)
    assert store.load_remembered(SCHOOL, '') == REMEMBERED
    assert f'remembered-sign-in {SCHOOL} HOST_NOT_ALLOWED' in capsys.readouterr().err


def test_an_unexpected_library_failure_keeps_the_remembered_account(capsys):
    def broken(remembered):
        raise RuntimeError('synthetic-remembering in a message')
    store = remembering()
    assert RememberedSignIn(store, sign_in=broken)(SCHOOL, '') is None
    log = capsys.readouterr().err
    assert 'EDUPAGE_ERROR from RuntimeError' in log and 'synthetic-remembering' not in log
    assert store.load_remembered(SCHOOL, '') == REMEMBERED


# -- right after the sign-in window: did the person tick "Remember password"? --
def test_edupage_confirms_a_remembered_password_for_the_school(web):
    seen = edupage(web)
    assert remembered_username(SCHOOL, REMEMBER) == LOGIN_NAME
    assert seen.sign_ins == [] and all(url.startswith(RPC) for url in web.calls)  # asking signs nobody in


@pytest.mark.parametrize('listed', [(), ({**LISTED, 'ph': ''},), ({**LISTED, 'ep': 'other-school'},),
                                    ('junk',), ({**LISTED, 'em': 7},)])
def test_without_a_remembered_password_nothing_is_confirmed(web, listed):
    edupage(web, listed=listed)
    assert remembered_username(SCHOOL, REMEMBER) is None


def test_confirming_never_fails_a_sign_in(web, capsys):
    assert remembered_username(SCHOOL, ()) is None and web.calls == []
    assert remembered_username(SCHOOL, REMEMBER) is None  # EduPage answers 404
    assert remembered_username(SCHOOL, (Cookie('edusrs', 'x', 'example.com'),)) is None
    web.routes[RPC] = lambda r: (_ for _ in ()).throw(requests.ConnectionError('unreachable'))
    assert remembered_username(SCHOOL, REMEMBER) is None
    assert capsys.readouterr().err == ''


def test_only_edupages_two_remember_cookies_are_recognised():
    jar = requests.cookies.RequestsCookieJar()
    jar.set('edusrs', 'a', domain='.edupage.org', path='/')
    jar.set('egdid', 'b', domain=HOST, path='/')
    jar.set('PHPSESSID', 'c', domain=HOST, path='/')
    jar.set('edusrs', 'd', domain='other.edupage.org', path='/')
    jar.set('hsid', 'e', domain=HOST, path='/')
    assert module.remember_cookies(HOST, jar) == (Cookie('edusrs', 'a', '.edupage.org'), Cookie('egdid', 'b', HOST))


# -- the school session and the tools ---------------------------------------
def expiring_session(renew, edu=None):
    """A school whose Saved Session EduPage no longer accepts, until `renew` provides a new one."""
    edu = edu or FakeEdu()
    opened = []

    def opener(saved):
        opened.append(saved.cookies[0].value)
        if saved.cookies[0].value != 'fresh-session':
            raise EduPageError('AUTH_REQUIRED')
        return edu
    session = SchoolSession(SCHOOL, '', lambda school, label: SAVED, opener=opener, renew=renew)
    return session, opened


FRESH = SavedSession(SCHOOL, 'Rodic100', LOGIN_NAME, (Cookie('PHPSESSID', 'fresh-session', HOST),))


def test_an_expired_session_is_renewed_when_connecting():
    asked = []
    session, opened = expiring_session(lambda school, label: asked.append((school, label)) or FRESH)
    assert session.connect().get_user_id() == 'Rodic100'
    assert asked == [(SCHOOL, '')] and opened == [COOKIE.value, 'fresh-session']
    assert session.saved == FRESH and session.renews


def test_without_a_remembered_account_the_session_failure_stands():
    session, opened = expiring_session(lambda school, label: None)
    with pytest.raises(EduPageError, match='^AUTH_REQUIRED$'):
        session.connect()
    assert opened == [COOKIE.value]
    plain, _ = expiring_session(None)
    with pytest.raises(EduPageError, match='^AUTH_REQUIRED$'):
        plain.connect()
    assert not plain.renews


@pytest.mark.parametrize('code', ['NOT_SIGNED_IN', 'SESSION_CORRUPT', 'CREDENTIAL_STORE_LOCKED', 'TEMPORARY_ERROR'])
def test_only_an_ended_session_is_renewed(code):
    asked = []

    def load(school, label):
        raise EduPageError(code)
    session = SchoolSession(SCHOOL, '', load, renew=lambda school, label: asked.append(school))
    with pytest.raises(EduPageError, match=f'^{code}$'):
        session.connect()
    assert asked == []  # after `logout` nothing signs in by itself


def test_a_renewed_session_for_another_account_is_refused():
    other = SavedSession(SCHOOL, 'Ucitel7', 'x', FRESH.cookies)
    session, _ = expiring_session(lambda school, label: other)
    with pytest.raises(EduPageError, match='^ACCOUNT_MISMATCH$'):
        session.connect()


def test_a_tool_call_that_finds_its_session_ended_runs_again_after_renewal():
    edu, renewals = FakeEdu(), []
    session = SchoolSession(SCHOOL, '', lambda school, label: SAVED, opener=lambda saved: edu,
                            renew=lambda school, label: renewals.append(school) or FRESH)
    tools = FullTools([session])
    assert tools.user_id()['status'] == 'READY'  # open, as after a morning's use
    answers = iter([EduPageError('AUTH_REQUIRED'), None])  # overnight EduPage ended it

    def user_id():
        failure = next(answers)
        if failure:
            session._opener = expiring_session(None, edu)[0]._opener  # the saved cookie is dead too
            raise failure
        return 'Rodic100'
    edu.get_user_id = user_id
    assert tools.user_id() == {'status': 'READY', 'untrusted_content': True, 'school': SCHOOL,
                               'user_id': 'Rodic100', 'role': 'parent'}
    assert renewals == [SCHOOL]


def test_a_school_that_cannot_renew_never_repeats_a_tool_call():
    edu, calls = FakeEdu(), []
    session = SchoolSession(SCHOOL, '', lambda school, label: SAVED, opener=lambda saved: edu)
    tools = FullTools([session])
    tools.user_id()

    def user_id():
        calls.append(1)
        raise EduPageError('AUTH_REQUIRED')
    edu.get_user_id = user_id
    assert tools.user_id()['status'] == 'AUTH_REQUIRED' and calls == [1]


def test_a_call_that_fails_while_connecting_is_not_repeated():
    asked = []
    session, opened = expiring_session(lambda school, label: asked.append(school))
    assert FullTools([session]).user_id()['status'] == 'AUTH_REQUIRED'
    assert asked == [SCHOOL] and opened == [COOKIE.value]  # one attempt, not two


def test_the_keep_alive_never_uses_a_remembered_account():
    asked = []
    session, _ = expiring_session(lambda school, label: asked.append(school) or FRESH)
    session.edu = FakeEdu()  # open, but EduPage has ended it
    session.edu.session = SimpleNamespace(get=lambda url: (_ for _ in ()).throw(EduPageError('HOST_NOT_ALLOWED')),
                                          close=lambda: None)
    assert session.keep_alive() == 'AUTH_REQUIRED' and session.edu is None and asked == []
