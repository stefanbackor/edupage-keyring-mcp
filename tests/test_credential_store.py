# SPDX-License-Identifier: GPL-3.0-or-later
import json

import pytest

from conftest import COOKIE, HOST, LABEL, SAVED, SCHOOL
from edupage_keyring_mcp import credential_store
from edupage_keyring_mcp.errors import EduPageError
from edupage_keyring_mcp.credential_store import (Cookie, RememberedAccount, SavedSession, SessionStore,
                                                  account_key, parse_cookie, session_cookies)

REMEMBER = (Cookie('edusrs', 'r' * 536, '.edupage.org', '/', 4102444800.0),
            Cookie('egdid', 'd' * 42, '.edupage.org', '/', 4102444800.0))
REMEMBERED = RememberedAccount(SCHOOL, 'Rodic100', 'parent@example.invalid', REMEMBER)


class MemoryCredentialStore:
    def __init__(self, broken=False):
        self.secrets, self.broken = {}, broken

    def set_password(self, service, account, secret):
        if self.broken:
            raise OSError('store locked')
        self.secrets[service, account] = secret

    def get_password(self, service, account):
        if self.broken:
            raise OSError('store locked')
        return self.secrets.get((service, account))

    def delete_password(self, service, account):
        self.secrets.pop((service, account))


def test_saved_session_round_trip():
    ring = MemoryCredentialStore()
    store = SessionStore(ring)
    store.save(LABEL, SAVED)
    [(service, account)] = ring.secrets
    assert service == 'edupage-keyring-mcp'
    assert account.startswith(SCHOOL + '/') and LABEL not in account
    assert store.load(SCHOOL, LABEL) == SAVED
    assert store.load(SCHOOL, LABEL.upper()) == SAVED  # label match ignores case
    assert store.delete(SCHOOL, LABEL) and not store.delete(SCHOOL, LABEL)


def test_one_account_per_school_needs_no_label():
    ring = MemoryCredentialStore()
    store = SessionStore(ring)
    store.save('', SAVED)
    assert list(ring.secrets) == [('edupage-keyring-mcp', SCHOOL)]
    assert store.load(SCHOOL, '') == SAVED
    with pytest.raises(EduPageError, match='^NOT_SIGNED_IN$'):
        store.load(SCHOOL, LABEL)  # a labelled entry is a different account
    store.save(LABEL, SAVED)
    assert store.load(SCHOOL, LABEL) == SAVED and len(ring.secrets) == 2


def test_a_failed_write_is_reported(capsys):
    with pytest.raises(EduPageError, match='^CREDENTIAL_STORE_WRITE_FAILED$'):
        SessionStore(MemoryCredentialStore(broken=True)).save(LABEL, SAVED)
    log = capsys.readouterr().err
    assert 'CREDENTIAL_STORE_WRITE_FAILED from OSError' in log and 'store locked' not in log


def test_load_failures(capsys):
    with pytest.raises(EduPageError, match='^NOT_SIGNED_IN$'):
        SessionStore(MemoryCredentialStore()).load(SCHOOL, LABEL)
    with pytest.raises(EduPageError, match='^CREDENTIAL_STORE_LOCKED$'):
        SessionStore(MemoryCredentialStore(broken=True)).load(SCHOOL, LABEL)
    log = capsys.readouterr().err
    assert 'CREDENTIAL_STORE_LOCKED from OSError' in log and 'in get_password' in log
    assert 'store locked' not in log
    ring = MemoryCredentialStore()
    for secret in ['not json', json.dumps({'format': 1}), json.dumps(
            {'format': 2, 'school': 'other', 'account': 'x', 'login': 'y', 'cookies': []}),
            json.dumps({'format': 2, 'school': SCHOOL, 'account': 'x', 'login': 'y', 'cookies': [['bad']]})]:
        ring.secrets['edupage-keyring-mcp', account_key(SCHOOL, LABEL)] = secret
        with pytest.raises(EduPageError, match='^SESSION_CORRUPT$'):
            SessionStore(ring).load(SCHOOL, LABEL)


@pytest.mark.parametrize('parts', [
    ('a b', 'v', HOST), ('n', 'x;y', HOST), ('n', 'v\n', HOST), ('n', 'v', HOST, 'rel'),
    ('n', 'v', HOST, '/', 'later'), ('n', 'v', HOST, '/', True), ('', 'v', HOST),
])
def test_malformed_cookie_parts(parts):
    with pytest.raises(EduPageError, match='^SESSION_CORRUPT$'):
        parse_cookie(*parts)


def test_session_cookies_scope_and_expiry():
    fresh, stale = COOKIE, Cookie('old', 'v', '.' + HOST, '/', 50)
    assert session_cookies(HOST, [fresh, stale], now=100) == [fresh]
    with pytest.raises(EduPageError, match='^SESSION_EXPIRED$'):
        session_cookies(HOST, [stale], now=100)
    with pytest.raises(EduPageError, match='^SESSION_CORRUPT$'):
        session_cookies(HOST, [fresh, Cookie('x', 'v', 'example.com')])
    assert session_cookies(HOST, [Cookie('lang', 'sk', '.edupage.org')]) != []


def backend_from(module):
    return type('Backend', (), {'__module__': module})()


@pytest.mark.parametrize('platform, module', [
    ('darwin', 'keyring.backends.macOS'), ('win32', 'keyring.backends.Windows'),
])
def test_the_os_credential_store_is_accepted(platform, module):
    backend = backend_from(module)
    store = credential_store.open_credential_store(platform, backend)
    assert isinstance(store, SessionStore) and store.backend is backend


@pytest.mark.parametrize('platform, module', [
    ('darwin', 'keyring.backends.Windows'), ('win32', 'keyring.backends.macOS'),
    ('darwin', 'keyrings.alt.file'), ('win32', 'keyrings.alt.Windows'),
    ('darwin', 'keyring.backends.chainer'), ('win32', 'keyring.backends.fail'),
    ('darwin', 'keyring.backends.null'), ('darwin', 'keyring_pass'),
    ('darwin', 'keyring.backends.macOSfake'), ('linux', 'keyring.backends.SecretService'),
    ('linux', 'keyring.backends.macOS'),
])
def test_other_platforms_and_backends_are_refused(platform, module):
    with pytest.raises(EduPageError, match='^CREDENTIAL_STORE_REQUIRED$'):
        credential_store.open_credential_store(platform, backend_from(module))


def test_windows_saved_sessions_stay_on_this_machine():
    backend = backend_from('keyring.backends.Windows')
    credential_store.open_credential_store('win32', backend)
    assert backend.persist == 'local machine'


def test_macos_backend_has_no_persistence_setting():
    backend = backend_from('keyring.backends.macOS')
    credential_store.open_credential_store('darwin', backend)
    assert not hasattr(backend, 'persist')


def test_saved_session_is_immutable():
    with pytest.raises(Exception):
        SAVED.account = 'x'  # type: ignore[misc]
    assert isinstance(SAVED, SavedSession)


def test_cookies_with_the_same_name_are_kept_apart():
    pair = (Cookie('PHPSESSID', 'school', HOST), Cookie('PHPSESSID', 'portal', '.edupage.org'))
    ring = MemoryCredentialStore()
    SessionStore(ring).save(LABEL, SavedSession(SCHOOL, 'Rodic100', LABEL, pair))
    assert SessionStore(ring).load(SCHOOL, LABEL).cookies == pair


def test_a_secret_over_2_kb_is_refused_when_saving():
    ring = MemoryCredentialStore()
    big = SavedSession(SCHOOL, 'Rodic100', LABEL, (Cookie('PHPSESSID', 'x' * 2048, HOST),))
    with pytest.raises(EduPageError, match='^SESSION_CORRUPT$'):
        SessionStore(ring).save(LABEL, big)
    assert ring.secrets == {}


def test_an_older_saved_session_with_several_cookies_still_loads():
    secret = ('{"format":2,"school":"%s","account":"Rodic100","login":"%s","saved":1761955200,'
              '"cookies":[["PHPSESSID","abc","%s","/",-1.0],["edusrs","s1","%s","/",-1.0],'
              '["egdid","d1",".edupage.org","/",4102444800.0],["hsid","h1","%s","/",-1.0],'
              '["erfp","r1","%s","/",-1.0]]}') % (SCHOOL, LABEL, HOST, HOST, HOST, HOST)
    ring = MemoryCredentialStore()
    ring.set_password('edupage-keyring-mcp', account_key(SCHOOL, LABEL), secret)
    saved = SessionStore(ring).load(SCHOOL, LABEL)
    assert saved.account == 'Rodic100' and saved.login == LABEL
    assert [c.name for c in saved.cookies] == ['PHPSESSID', 'edusrs', 'egdid', 'hsid', 'erfp']
    assert saved.cookies[2] == Cookie('egdid', 'd1', '.edupage.org', '/', 4102444800.0)


def test_an_existing_saved_session_over_2_kb_still_loads():
    cookies = [['c%d' % i, 'v' * 1000, HOST, '/', -1.0] for i in range(10)]
    secret = json.dumps({'format': 2, 'school': SCHOOL, 'account': 'Rodic100', 'login': LABEL,
                         'saved': 1761955200, 'cookies': cookies})
    assert 2 * 1024 < len(secret) < 64 * 1024
    ring = MemoryCredentialStore()
    ring.set_password('edupage-keyring-mcp', account_key(SCHOOL, LABEL), secret)
    assert len(SessionStore(ring).load(SCHOOL, LABEL).cookies) == 10


def test_a_secret_over_64_kb_is_refused_when_loading():
    secret = json.dumps({'format': 2, 'school': SCHOOL, 'account': 'Rodic100', 'login': LABEL,
                         'cookies': [['PHPSESSID', 'v', HOST, '/', -1.0]], 'pad': 'x' * 64 * 1024})
    ring = MemoryCredentialStore()
    ring.set_password('edupage-keyring-mcp', account_key(SCHOOL, LABEL), secret)
    with pytest.raises(EduPageError, match='^SESSION_CORRUPT$'):
        SessionStore(ring).load(SCHOOL, LABEL)


def test_a_session_store_needs_an_explicit_backend():
    with pytest.raises(TypeError):
        SessionStore()  # type: ignore[call-arg]  # no silent fallback past the ADR 0001 allowlist


def test_a_remembered_account_is_its_own_entry_beside_the_saved_session():
    ring = MemoryCredentialStore()
    store = SessionStore(ring)
    assert store.load_remembered(SCHOOL, '') is None
    store.save('', SAVED)
    store.save_remembered('', REMEMBERED)
    assert sorted(account for _, account in ring.secrets) == [SCHOOL, SCHOOL + ':remembered']
    assert store.load_remembered(SCHOOL, '') == REMEMBERED and store.load(SCHOOL, '') == SAVED
    assert 'edusrs' not in ring.secrets['edupage-keyring-mcp', SCHOOL]
    assert len(ring.secrets['edupage-keyring-mcp', SCHOOL + ':remembered']) < 1024  # at EduPage's real sizes
    assert store.load_remembered(SCHOOL, LABEL) is None  # a labelled account has its own
    store.save_remembered(LABEL, REMEMBERED)
    assert store.load_remembered(SCHOOL, LABEL.upper()) == REMEMBERED
    assert store.delete_remembered(SCHOOL, '') and not store.delete_remembered(SCHOOL, '')
    assert store.load(SCHOOL, '') == SAVED  # forgetting it keeps the session


def test_an_unreadable_remembered_account_counts_as_none(capsys):
    assert SessionStore(MemoryCredentialStore(broken=True)).load_remembered(SCHOOL, '') is None
    assert 'store locked' not in capsys.readouterr().err
    ring = MemoryCredentialStore()
    cookie = ['edusrs', 'v', '.edupage.org', '/', -1.0]
    good = {'format': 1, 'school': SCHOOL, 'account': 'Rodic100', 'username': 'u', 'cookies': [cookie]}
    for secret in ['not json', '[]', json.dumps({**good, 'format': 2}), json.dumps({**good, 'school': 'other'}),
                   json.dumps({**good, 'cookies': []}), json.dumps({**good, 'cookies': [['bad']]}),
                   json.dumps({**good, 'cookies': [cookie] * 5}), json.dumps({**good, 'username': 7}),
                   json.dumps({**good, 'account': ''}), json.dumps({**good, 'pad': 'x' * 2048})]:
        ring.secrets['edupage-keyring-mcp', SCHOOL + ':remembered'] = secret
        assert SessionStore(ring).load_remembered(SCHOOL, '') is None
    ring.secrets['edupage-keyring-mcp', SCHOOL + ':remembered'] = json.dumps(good)
    assert SessionStore(ring).load_remembered(SCHOOL, '') == RememberedAccount(
        SCHOOL, 'Rodic100', 'u', (Cookie('edusrs', 'v', '.edupage.org'),))


def test_a_remembered_account_that_does_not_fit_is_refused():
    ring = MemoryCredentialStore()
    for cookies in [(), (Cookie('edusrs', 'x' * 2048, '.edupage.org'),)]:
        with pytest.raises(EduPageError, match='^SESSION_CORRUPT$'):
            SessionStore(ring).save_remembered('', RememberedAccount(SCHOOL, 'Rodic100', 'u', cookies))
    with pytest.raises(EduPageError, match='^CREDENTIAL_STORE_WRITE_FAILED$'):
        SessionStore(MemoryCredentialStore(broken=True)).save_remembered('', REMEMBERED)
    assert ring.secrets == {}
