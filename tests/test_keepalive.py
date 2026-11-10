# SPDX-License-Identifier: GPL-3.0-or-later
from conftest import HOST, SAVED, SCHOOL, account_page, reply
from edupage_keyring_mcp.client import SchoolSession
from edupage_keyring_mcp.keepalive import KeepAlive

USERHOME = f'https://{HOST}/user/'


def counting_session(school=SCHOOL):
    reads = []

    def load(name, label):
        reads.append(name)
        return SAVED

    return SchoolSession(school, '', load), reads


def waits(rounds):
    """A wait() that lets the loop run `rounds` times, recording each requested delay."""
    asked = []

    def wait(seconds):
        asked.append(seconds)
        return len(asked) > rounds  # True: stop
    return wait, asked


def test_a_connected_session_is_pinged_once_per_interval(web, capsys):
    web.routes[USERHOME] = lambda r: reply(r, 200, account_page())
    session, reads = counting_session()
    session.connect()
    web.calls.clear()
    wait, asked = waits(3)
    KeepAlive([session], 60, wait=wait).run()
    assert web.calls == [USERHOME] * 3
    assert asked == [3600] * 4
    assert reads == [SCHOOL] and session.edu is not None
    assert capsys.readouterr().err == f'edupage-keyring-mcp: keep-alive {SCHOOL} READY\n' * 3


def test_a_session_never_opened_gets_no_request_and_no_store_read(web, capsys):
    session, reads = counting_session()
    wait, _ = waits(2)
    KeepAlive([session], 60, wait=wait).run()
    assert web.calls == [] and reads == [] and session.edu is None
    assert capsys.readouterr().err == ''


def test_a_ping_waits_for_a_tool_call_holding_the_lock(web):
    import threading
    order = []
    web.routes[USERHOME] = lambda r: (order.append('ping'), reply(r, 200, account_page()))[1]
    session, _ = counting_session()
    session.connect()
    keeper = KeepAlive([session], 60)
    with session.lock:
        order.append('tool starts')
        pinger = threading.Thread(target=keeper.tick)
        pinger.start()
        pinger.join(0.2)
        order.append('tool ends')
    pinger.join(5)
    assert order[-3:] == ['tool starts', 'tool ends', 'ping']


def test_a_rejected_session_is_dropped_logged_once_and_left_alone(web, capsys):
    web.routes[USERHOME] = lambda r: reply(r, 200, account_page())
    session, reads = counting_session()
    session.connect()
    web.routes[USERHOME] = lambda r: reply(r, 302, headers={'Location': '/login/?secret=page-text'})
    web.routes[f'https://{HOST}/login/'] = lambda r: reply(r, 200, b'<form>page-text</form>')
    web.calls.clear()
    wait, _ = waits(3)
    KeepAlive([session], 60, wait=wait).run()
    assert session.edu is None
    assert len([url for url in web.calls if url == USERHOME]) == 1  # no more pings once dropped
    assert reads == [SCHOOL]  # never reopened from the Credential Store
    err = capsys.readouterr().err
    assert err == f'edupage-keyring-mcp: keep-alive {SCHOOL} AUTH_REQUIRED\n'

    web.routes[USERHOME] = lambda r: reply(r, 200, account_page())
    session.connect()  # a tool call signs the school back in
    web.calls.clear()
    wait, _ = waits(1)
    KeepAlive([session], 60, wait=wait).run()
    assert web.calls == [USERHOME]


def test_a_network_error_is_logged_and_retried_next_time(web, capsys):
    import requests
    web.routes[USERHOME] = lambda r: reply(r, 200, account_page())
    session, _ = counting_session()
    session.connect()

    def offline(request):
        raise requests.ConnectionError('no route to synthetic-session')
    web.routes[USERHOME] = offline
    web.calls.clear()
    wait, _ = waits(2)
    KeepAlive([session], 60, wait=wait).run()
    assert web.calls == [USERHOME] * 2 and session.edu is not None
    assert capsys.readouterr().err == f'edupage-keyring-mcp: keep-alive {SCHOOL} TEMPORARY_ERROR\n' * 2


def test_an_unexpected_error_does_not_stop_the_keep_alive(capsys):
    from conftest import FakeEdu, school_session
    first, second = school_session(FakeEdu(), 'first-school'), school_session(FakeEdu(), 'second-school')
    first.connect()
    second.connect()
    first.edu.session = None  # .get() raises AttributeError
    pinged = []
    second.edu.session.get = lambda url: pinged.append(url) or (_ for _ in ()).throw(KeyError('page text from EduPage'))
    KeepAlive([first, second], 60).tick()
    assert pinged == ['https://second-school.edupage.org/user/']
    err = capsys.readouterr().err
    assert 'keep-alive first-school LOCAL_ERROR' in err and 'keep-alive second-school LOCAL_ERROR' in err
    assert 'LOCAL_ERROR from AttributeError' in err and 'LOCAL_ERROR from KeyError' in err
    assert 'page text' not in err  # the message never reaches the log
    assert first.edu is not None and second.edu is not None
