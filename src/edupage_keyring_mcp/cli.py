# SPDX-License-Identifier: GPL-3.0-or-later
"""Command line: run the MCP server, sign in, sign out, check status, probe tools."""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import nullcontext
from datetime import date, timedelta
from typing import Any

from . import __version__
from .client import SchoolSession
from .config import login_name, parse_schools, refuse_plaintext_secrets
from .errors import EduPageError
from .credential_store import SessionStore, open_credential_store
from .keepalive import DEFAULT_MINUTES, MAX_MINUTES, MIN_MINUTES, KeepAlive, keep_alive_minutes
from .net import is_edupage_host, school_name

COMMANDS = ('serve', 'login', 'logout', 'status', 'probe')


def _schools_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--school', action='append', default=[], metavar='NAME[=LOGIN]',
                        help='School name (NAME.edupage.org) or its EduPage address. Repeat for more schools; '
                             'NAME=LOGIN if that school uses a different EduPage login.')
    parser.add_argument('--username', help='Only to keep two accounts at one school apart '
                                           '(default: $EDUPAGE_USERNAME, usually unset).')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='edupage-keyring-mcp',
        description='EduPage MCP server whose session lives in the Credential Store '
                    '(macOS Keychain or Windows Credential Manager).')
    parser.add_argument('--version', action='version', version=__version__)
    sub = parser.add_subparsers(dest='command')

    serve = sub.add_parser('serve', help='Run the MCP server over stdio (default).')
    _schools_parser(serve)
    serve.add_argument('--download-host', action='append', default=[], metavar='HOST',
                       help='Extra *.edupage.org host allowed for attachment downloads.')
    serve.add_argument('--no-browser-signin', action='store_true',
                       help='Do not let the sign_in tool open Chrome; use the login command instead.')
    serve.add_argument('--keep-alive', default='', metavar='MINUTES',
                       help=f'Load each open school\'s home page every MINUTES '
                            f'({MIN_MINUTES}-{MAX_MINUTES}; smaller values count as {MIN_MINUTES}) so EduPage does not end an idle session. '
                            f'Default {DEFAULT_MINUTES}; 0 turns it off.')

    for name, text in [('login', 'Sign in to a school in Chrome and save the session.'),
                       ('logout', 'Delete a school\'s saved session, and its remembered account, '
                                  'from the Credential Store.')]:
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument('school', help='School name, or its EduPage address (e.g. https://myschool.edupage.org).')
        cmd.add_argument('--username', help='Only to keep two accounts at one school apart '
                                            '(default: $EDUPAGE_USERNAME, usually unset).')
        if name == 'login':
            cmd.add_argument('--timeout', type=int, default=600, help='Seconds to wait for sign-in.')

    status = sub.add_parser('status', help='Check saved sessions (prints statuses only).')
    _schools_parser(status)
    probe = sub.add_parser('probe', help='Call every read tool once; prints statuses and counts only.')
    _schools_parser(probe)
    return parser


def _sessions(args: argparse.Namespace, store: SessionStore) -> list[SchoolSession]:
    from .remembered import RememberedSignIn
    pairs = parse_schools(args.school, args.username)
    renew = RememberedSignIn(store)  # used only for a school whose person ticked "Remember password"
    return [SchoolSession(school, username, store.load, renew=renew) for school, username in pairs]


def _print(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False))


def summary(name: str, data: dict) -> dict:
    """Reduce a tool result to its status and the size of its main list."""
    row: dict[str, Any] = {'tool': name, 'status': data.get('status')}
    if data.get('error_type'):
        row['error_type'] = data['error_type']
    for key, value in data.items():
        if isinstance(value, list) and key not in ('children_names',):
            row['count'] = len(value)
            break
    return row


def run_probe(tools: Any) -> list[dict]:
    """Call each read tool once. Never calls a write tool, never returns content."""
    rows = [summary('list_schools', tools.list_schools())]
    listing = tools.list_children()
    rows.append(summary('list_children', listing))
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    for school in tools.sessions:
        for name, call in [
            ('user_id', lambda: tools.user_id(school)), ('school_year', lambda: tools.school_year(school)),
            ('get_periods', lambda: tools.get_periods(school)),
            ('get_next_ringing_time', lambda: tools.get_next_ringing_time('', school)),
            ('get_timetable_changes', lambda: tools.get_timetable_changes('', school)),
            ('get_missing_teachers', lambda: tools.get_missing_teachers('', school)),
            ('get_upcoming_events', lambda: tools.timeline(None, '', week_ago, school)),
            ('list_messages', lambda: tools.list_messages(week_ago, '', school)),
            ('get_teachers', lambda: tools.roster('teachers', school)),
            ('get_subjects', lambda: tools.roster('subjects', school)),
        ]:
            rows.append({**summary(name, call()), 'school': school})
    for number, child in enumerate(listing.get('children', []), 1):
        ref, school = child['ref'], child['school']
        for name, call in [
            ('get_timetable', lambda: tools.get_timetable(ref, '', school)),
            ('get_week_timetable', lambda: tools.get_week_timetable(ref, '', school)),
            ('get_grades', lambda: tools.get_grades(ref, 0, '', school)),
            ('get_homework', lambda: tools.timeline({'homework'}, ref, '', school)),
            ('get_absences', lambda: tools.timeline({'pipnutie', 'student_absent'}, ref, '', school)),
            ('get_meals', lambda: tools.get_meals(ref, '', school)),
        ]:
            rows.append({**summary(name, call()), 'child': number})  # a number, never a name
    return rows


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ('-h', '--help', '--version')):
        argv.insert(0, 'serve')
    args = build_parser().parse_args(argv)
    try:
        minutes = keep_alive_minutes(args.keep_alive) if args.command == 'serve' else 0
        refuse_plaintext_secrets()
        store = open_credential_store()
        if args.command == 'login':
            from .signin import sign_in
            args.school = school_name(args.school)
            print(f'Opening Chrome for {args.school}.edupage.org. Sign in there; '
                  'this window waits until EduPage shows your account. Tick "Remember password" in '
                  'EduPage\'s dialog if this computer should sign in again by itself.', file=sys.stderr)
            _print(sign_in(args.school, login_name(args.username), store=store,
                           wait_seconds=args.timeout))
        elif args.command == 'logout':
            args.school = school_name(args.school)
            label = login_name(args.username)
            removed = [store.delete(args.school, label), store.delete_remembered(args.school, label)]
            _print({'status': 'REMOVED' if any(removed) else 'NOTHING_SAVED', 'school': args.school})
        elif args.command == 'status':
            from .tools import FullTools
            _print(FullTools(_sessions(args, store)).list_schools())
        elif args.command == 'probe':
            from .tools import FullTools
            rows = run_probe(FullTools(_sessions(args, store)))
            for row in rows:
                _print(row)
            bad = [r for r in rows if r['status'] not in ('READY', 'MEALS_NOT_AVAILABLE')]
            _print({'tools_called': len(rows), 'not_ready': len(bad)})
            return 1 if bad else 0
        else:
            if not all(is_edupage_host(h) for h in args.download_host):
                raise EduPageError("HOST_NOT_ALLOWED")
            from .server import create_server
            from .signin import BrowserSignIn
            from .tools import FullTools
            tools = FullTools(_sessions(args, store), set(args.download_host),
                              None if args.no_browser_signin else BrowserSignIn())
            with KeepAlive(tools.sessions.values(), minutes) if minutes else nullcontext():
                create_server(tools).run()
        return 0
    except EduPageError as exc:
        _print({'status': exc.code, **exc.extra}) if args.command != 'serve' else None
        print(exc.code, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
