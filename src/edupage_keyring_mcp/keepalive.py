# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep-alive: load `/user/` now and then so EduPage doesn't end an idle session.

EduPage signs a session out after about 110-180 minutes without requests. While
the server runs, this loads each open school's home page once per interval (every
DEFAULT_MINUTES unless `--keep-alive` says otherwise; 0 turns it off). It
never opens a session that isn't open yet, so it never reads the Credential Store
and never starts a sign-in. Each ping writes one line to the server log, e.g.
`keep-alive <school> READY`.
"""
from __future__ import annotations

import threading
from typing import Callable, Iterable

from .client import SchoolSession
from .errors import EduPageError, log_line

# EduPage's idle timeout can be as short as about 110 minutes.
MIN_MINUTES, MAX_MINUTES = 5, 120
# On by default: an overnight run at this interval kept a session alive for 19 hours.
DEFAULT_MINUTES = 60


def keep_alive_minutes(value: str | None) -> int:
    """The `--keep-alive` value as whole minutes: DEFAULT_MINUTES when empty, 0 for off.

    1 to 4 count as MIN_MINUTES, so a small value in the extension's field still starts.

    Raises:
        EduPageError: INVALID_KEEP_ALIVE for anything but a whole number up to MAX_MINUTES.
    """
    text = (value or '').strip()
    if not text or text.startswith('${'):  # an unset .mcpb field may arrive unsubstituted
        return DEFAULT_MINUTES
    whole, _, fraction = text.partition('.')  # the .mcpb number field may arrive as "60.0"
    if not whole.isdigit() or fraction.strip('0') or int(whole) > MAX_MINUTES:
        raise EduPageError('INVALID_KEEP_ALIVE', minimum=MIN_MINUTES, maximum=MAX_MINUTES)
    minutes = int(whole)
    return max(minutes, MIN_MINUTES) if minutes else 0


class KeepAlive:
    """Pings every already connected school once per `minutes`, in a daemon thread.

    Use as a context manager around the server's run: the thread starts on entry and
    stops on exit, and being a daemon it never keeps the process alive on its own.
    """

    def __init__(self, sessions: Iterable[SchoolSession], minutes: int, *,
                 wait: Callable[[float], bool] | None = None) -> None:
        self.sessions = list(sessions)
        self.seconds = minutes * 60
        self._stopped = threading.Event()
        self._wait = wait or self._stopped.wait  # returns True to stop

    def tick(self) -> None:
        for session in self.sessions:
            code = session.keep_alive()
            if code is not None:  # one line per ping sent; none for a school not open
                log_line(f'keep-alive {session.school} {code}')

    def run(self) -> None:
        while not self._wait(self.seconds):
            self.tick()

    def __enter__(self) -> 'KeepAlive':
        threading.Thread(target=self.run, name='edupage-keep-alive', daemon=True).start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stopped.set()
