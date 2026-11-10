# SPDX-License-Identifier: GPL-3.0-or-later
import asyncio
import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

from conftest import FakeEdu, tools_for
from edupage_keyring_mcp import __version__, cli, config
from edupage_keyring_mcp.errors import EduPageError
from edupage_keyring_mcp.server import create_server

ROOT = pathlib.Path(__file__).resolve().parents[1]
WRITE_TOOLS = {'send_message', 'choose_meal', 'sign_off_meal', 'rate_meal'}
LOCAL_TOOLS = {'sign_in'}  # acts on this computer only: opens a browser window
FORBIDDEN_PARAMETERS = {'password', 'passwd', 'cookie', 'cookies', 'session', 'session_id', 'token',
                        'url', 'link', 'headers', 'username', 'login'}


@pytest.mark.parametrize('name', ['EDUPAGE_PASSWORD', 'EDUPAGE_PWD', 'EDUPAGE_PHPSESSID', 'edupage_sid'])
def test_plaintext_settings_are_refused(name):
    with pytest.raises(EduPageError, match='^PLAINTEXT_SECRET_REFUSED$'):
        config.refuse_plaintext_secrets({name: 'x'})
    config.refuse_plaintext_secrets({'EDUPAGE_USERNAME': 'a', 'EDUPAGE_NOTHING': '', 'PATH': '/bin'})


def test_school_arguments():
    env = {'EDUPAGE_USERNAME': 'p@example.invalid'}
    assert config.parse_schools(['a', 'b=q@example.invalid'], environ=env) == [
        ('a', 'p@example.invalid'), ('b', 'q@example.invalid')]
    assert config.parse_schools(['https://c.edupage.org/user/?eqa=x', 'D.edupage.org=q@example.invalid'],
                                environ=env) == [('c', 'p@example.invalid'), ('d', 'q@example.invalid')]
    for values, code in [(['a', 'a'], 'DUPLICATE_SCHOOL'), ([], 'SCHOOL_REQUIRED'), (['x.com'], 'INVALID_SCHOOL')]:
        with pytest.raises(EduPageError, match=f'^{code}$'):
            config.parse_schools(values, environ=env)
    assert config.parse_schools(['a', 'b=q@example.invalid'], environ={}) == [
        ('a', ''), ('b', 'q@example.invalid')]  # no username needed
    assert config.parse_schools([' a , https://b.edupage.org,c=q@example.invalid,'], environ={}) == [
        ('a', ''), ('b', ''), ('c', 'q@example.invalid')]  # the extension's one School field
    with pytest.raises(EduPageError, match='^SCHOOL_REQUIRED$'):
        config.parse_schools([' , '], environ={})
    with pytest.raises(EduPageError, match='^INVALID_USERNAME$'):
        config.parse_schools(['a=x\ny'], environ={})


def test_cli_stops_early_without_a_safe_environment(monkeypatch, capsys):
    monkeypatch.setenv('EDUPAGE_PASSWORD', 'x')
    assert cli.main(['status', '--school', 'a']) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'PLAINTEXT_SECRET_REFUSED'
    monkeypatch.delenv('EDUPAGE_PASSWORD')
    monkeypatch.setattr(sys, 'platform', 'linux')
    assert cli.main(['--school', 'a']) == 1  # bare options run the server
    assert capsys.readouterr().err.strip() == 'CREDENTIAL_STORE_REQUIRED'


def test_probe_output_has_no_school_content():
    rows = cli.run_probe(tools_for(('demo-school', FakeEdu())))
    dumped = json.dumps(rows, ensure_ascii=False)
    for private in ('Matematika', 'Šimon', 'Ema', 'Hana', 'Vzor'):
        assert private not in dumped
    assert all(set(row) <= {'tool', 'status', 'count', 'error_type', 'school', 'child'} for row in rows)


def test_every_tool_is_safe_to_expose():
    server = create_server(tools_for(('demo-school', FakeEdu())))
    listed = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert len(listed) == 36
    assert not [n for n in listed if 'login' in n or 'request' in n or 'session' in n]
    for name, tool in listed.items():
        assert not FORBIDDEN_PARAMETERS & set(tool.input_schema.get('properties', {})), name
        assert tool.output_schema, name
        writes = name in WRITE_TOOLS
        assert bool(tool.annotations.destructive_hint) is writes, name
        assert bool(tool.annotations.read_only_hint) is not (writes or name in LOCAL_TOOLS), name
    assert set(listed['sign_in'].input_schema['properties']) == {'school'}


def test_tools_answer_over_mcp():
    server = create_server(tools_for(('demo-school', FakeEdu())))
    children = asyncio.run(server.call_tool('list_children', {})).structured_content
    assert children['status'] == 'READY' and len(children['children']) == 2
    stranger = asyncio.run(server.call_tool('school_year', {'school': 'elsewhere'})).structured_content
    assert stranger['status'] == 'UNKNOWN_SCHOOL' and 'next' not in stranger
    expired = create_server(tools_for(('demo-school', EduPageError('SESSION_EXPIRED'))))
    failed = asyncio.run(expired.call_tool('school_year', {})).structured_content
    assert failed['status'] == 'SESSION_EXPIRED' and '`edupage-keyring-mcp login demo-school`' in failed['next']
    both = create_server(tools_for(('demo-school', FakeEdu()), ('second-school', EduPageError('NOT_SIGNED_IN'))))
    failed = asyncio.run(both.call_tool('school_year', {'school': 'second-school'})).structured_content
    assert 'login second-school' in failed['next']


def test_release_metadata_agrees():
    tomllib = pytest.importorskip('tomllib')
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    manifest = json.loads((ROOT / 'server.json').read_text(encoding='utf-8'))
    assert project['version'] == __version__ == manifest['version'] == manifest['packages'][0]['version']
    assert manifest['packages'][0]['identifier'] == project['name']
    assert f"mcp-name: {manifest['name']}" in (ROOT / 'README.md').read_text(encoding='utf-8')
    assert len(manifest['description']) <= 100
    bundle = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))
    assert bundle['version'] == __version__ and bundle['name'] == project['name']
    assert bundle['description'] == manifest['description']
    assert set(bundle['compatibility']['platforms']) == {'darwin', 'win32'}


def test_bundle_manifest_lists_every_tool():
    server = create_server(tools_for(('demo-school', FakeEdu())))
    served = {tool.name: tool.description for tool in asyncio.run(server.list_tools())}
    bundle = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))
    assert {tool['name']: tool['description'] for tool in bundle['tools']} == served
    assert (ROOT / bundle['server']['entry_point']).is_file()
    tomllib = pytest.importorskip('tomllib')
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    assert bundle['compatibility']['runtimes']['python'] == project['requires-python']


def test_every_source_file_carries_the_license_tag():
    for path in [*ROOT.glob('src/**/*.py'), *ROOT.glob('tests/*.py')]:
        assert path.read_text(encoding='utf-8').startswith('# SPDX-License-Identifier: GPL-3.0-or-later'), path


def serve_with(monkeypatch, *flags):
    """Run `serve` up to the point the MCP server would take over stdio; return the live threads then."""
    import threading
    from edupage_keyring_mcp import server
    seen = {}

    class Server:
        def run(self):
            seen['threads'] = [t for t in threading.enumerate() if t.name == 'edupage-keep-alive']
    monkeypatch.setattr(cli, 'open_credential_store', lambda: SimpleNamespace(load=None))
    monkeypatch.setattr(server, 'create_server', lambda tools: Server())
    seen['exit'] = cli.main(['serve', '--school', 'demo-school', *flags])
    return seen


@pytest.mark.parametrize('flags', [('--keep-alive', '0'), ('--keep-alive=0.0',)])
def test_keep_alive_can_be_turned_off(monkeypatch, flags):
    seen = serve_with(monkeypatch, *flags)
    assert seen == {'threads': [], 'exit': 0}


@pytest.mark.parametrize('flags', [(), ('--keep-alive', ''), ('--keep-alive=60',), ('--keep-alive=60.0',),
                                   ('--keep-alive=3',), ('--keep-alive=${user_config.keep_alive}',)])
def test_keep_alive_runs_in_a_daemon_thread_until_the_server_stops(monkeypatch, flags):
    seen = serve_with(monkeypatch, *flags)  # on by default
    assert seen['exit'] == 0 and len(seen['threads']) == 1 and seen['threads'][0].daemon
    seen['threads'][0].join(5)
    assert not seen['threads'][0].is_alive()


@pytest.mark.parametrize('value', ['121', '-5', 'hourly', '7.5', '.', '1e2', '120.5'])
def test_keep_alive_out_of_range_is_refused_at_startup(monkeypatch, capsys, value):
    seen = serve_with(monkeypatch, f'--keep-alive={value}')
    assert seen == {'exit': 1}
    assert capsys.readouterr().err.strip() == 'INVALID_KEEP_ALIVE'


def test_bundle_keep_alive_setting_reaches_serve(monkeypatch):
    bundle = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))
    setting = bundle['user_config']['keep_alive']
    assert (setting['default'], setting['min'], setting['max']) == (60, 0, 120)
    args = bundle['server']['mcp_config']['args']
    flags = [a.replace('${user_config.keep_alive}', str(setting['default'])) for a in args if 'keep_alive' in a]
    assert flags == ['--keep-alive=60']
    seen = serve_with(monkeypatch, *flags)
    assert seen['exit'] == 0 and len(seen['threads']) == 1
    seen['threads'][0].join(5)


@pytest.mark.parametrize('value, minutes', [('', 60), ('${user_config.keep_alive}', 60), ('0', 0), ('0.0', 0),
                                            ('1', 5), ('4.0', 5), ('5', 5), ('90', 90), ('120', 120)])
def test_keep_alive_minutes(value, minutes):
    from edupage_keyring_mcp.keepalive import keep_alive_minutes
    assert keep_alive_minutes(value) == minutes


def test_every_school_can_sign_in_again_from_a_remembered_account(monkeypatch):
    from edupage_keyring_mcp import server
    from edupage_keyring_mcp.remembered import RememberedSignIn
    seen = {}
    store = SimpleNamespace(load=None)
    monkeypatch.setattr(cli, 'open_credential_store', lambda: store)
    monkeypatch.setattr(server, 'create_server',
                        lambda tools: seen.update(tools=tools) or SimpleNamespace(run=lambda: None))
    assert cli.main(['serve', '--school', 'demo-school,second-school', '--keep-alive=0']) == 0
    renewers = {session._renew for session in seen['tools'].sessions.values()}
    assert len(renewers) == 1 and all(s.renews for s in seen['tools'].sessions.values())
    [renew] = renewers
    assert isinstance(renew, RememberedSignIn) and renew.store is store


def test_there_is_no_setting_that_takes_or_keeps_a_password():
    bundle = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))
    assert set(bundle['user_config']) == {'school', 'keep_alive'}
    options = {option for action in cli.build_parser()._subparsers._group_actions[0].choices.values()
               for held in action._actions for option in held.option_strings}
    assert not [option for option in options if 'password' in option or 'remember' in option]


def test_logout_removes_the_remembered_account_too(monkeypatch, capsys):
    from conftest import SAVED, SCHOOL
    from edupage_keyring_mcp.credential_store import SessionStore
    from test_credential_store import REMEMBERED, MemoryCredentialStore
    ring = MemoryCredentialStore()
    store = SessionStore(ring)
    monkeypatch.setattr(cli, 'open_credential_store', lambda: store)
    store.save('', SAVED)
    store.save_remembered('', REMEMBERED)
    assert cli.main(['logout', SCHOOL]) == 0 and ring.secrets == {}
    assert json.loads(capsys.readouterr().out) == {'status': 'REMOVED', 'school': SCHOOL}
    store.save_remembered('', REMEMBERED)
    assert cli.main(['logout', SCHOOL]) == 0 and ring.secrets == {}
    assert json.loads(capsys.readouterr().out)['status'] == 'REMOVED'
    assert cli.main(['logout', SCHOOL]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'NOTHING_SAVED'
