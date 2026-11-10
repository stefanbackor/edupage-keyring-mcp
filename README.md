# EduPage Keyring MCP

<!-- mcp-name: io.github.stefanbackor/edupage-keyring-mcp -->

An [MCP](https://modelcontextprotocol.io) server that lets Claude (or any MCP
client) read your [EduPage](https://www.edupage.org) school account: timetables,
grades, homework, tests, absences, messages, menus and more, for every child
and school you have, **without ever storing your password**.

You sign in once per school in Chrome, on EduPage's own page, two-factor
included. Only EduPage's session cookie (`PHPSESSID`) is kept, in your
computer's **Credential Store**: the macOS Keychain, or Windows Credential
Manager (Windows support is **beta**). Built on [`edupage-api`](https://github.com/EdupageAPI/edupage-api).

## What you can ask

- "What does each kid have tomorrow, and what's due this week?"
- "Any tests coming up for Šimon? Which topics?"
- "How are Ema's grades in Slovak this term?"
- "Did Peter get to school on time today?"
- "Summarize what the school sent in the last two weeks and flag anything I must act on."
- "Which of today's lessons are substituted for 5.C?"
- "Tell the class teacher that Ema will be absent on Friday." (Claude Desktop asks before sending)

## Install

### Claude Desktop: one-click extension

1. Download
   [`edupage-keyring-mcp.mcpb`](https://github.com/stefanbackor/edupage-keyring-mcp/releases/latest/download/edupage-keyring-mcp.mcpb)
   from the latest release.
2. Double-click it (or drag it onto **Settings → Extensions** in Claude
   Desktop) and choose **Install**.
3. Enter your **School**: the name before `.edupage.org`, or your school's
   EduPage address. Several schools: separate them with commas
   (`first, second`); the first is the default. Two accounts at one school:
   `myschool=teacher` (see [Options](#options)).

You need macOS or Windows (**beta**, see [Windows](#windows-beta)) and Google
Chrome. Nothing else to install: when the extension is installed, Claude
Desktop downloads its own copy of uv, which sets up Python and the extension's
dependencies. The extension runs on your computer like any local MCP server,
with the same Saved Sessions in the Credential Store. Then go on with
[Ask something](#ask-something).

> **Updates are manual.** The extension does not update itself. To get a new
> version, download `edupage-keyring-mcp.mcpb` from the link above again and
> install it over the old one. Want updates on every restart instead? Use
> [uvx](#any-client-run-it-with-uvx).

### Any client: run it with uvx

#### 1. Get the requirements

You need macOS or Windows (**beta**), Google Chrome, and
[uv](https://docs.astral.sh/uv/):

```sh
brew install uv          # or: curl -LsSf https://astral.sh/uv/install.sh | sh
```

On Windows, see [Windows](#windows-beta).

Nothing else to install: `uvx` downloads this server from PyPI and runs it in
its own isolated environment.

#### 2. Connect your client

Desktop apps need the full path to `uvx`; get it with `which uvx` (Windows:
`where uvx`). It is usually `/opt/homebrew/bin/uvx` (Homebrew),
`/Users/YOU/.local/bin/uvx` (installer script) or
`C:\Users\YOU\.local\bin\uvx.exe` (Windows). Replace `myschool` with your school: the name before `.edupage.org`,
or simply your school's whole EduPage address, such as
`https://myschool.edupage.org`.

**Claude Desktop:** open **Settings → Local MCP servers → Edit Config** (older
versions: **Settings → Developer → Edit Config**) and add:

```json
{
  "mcpServers": {
    "edupage": {
      "command": "/opt/homebrew/bin/uvx",
      "args": ["edupage-keyring-mcp@latest", "--school", "myschool"]
    }
  }
}
```

Quit Claude Desktop (⌘Q on macOS; on Windows, quit it from the system tray)
and reopen it.

**Claude Code:**

```sh
claude mcp add edupage --scope user -- uvx edupage-keyring-mcp@latest --school myschool
```

**Codex CLI:**

```sh
codex mcp add edupage -- uvx edupage-keyring-mcp@latest --school myschool
```

**Cursor:** add the Claude Desktop JSON above to `~/.cursor/mcp.json`.

**VS Code:** `.vscode/mcp.json`, or **MCP: Open User Configuration**:

```json
{
  "servers": {
    "edupage": {
      "type": "stdio",
      "command": "/opt/homebrew/bin/uvx",
      "args": ["edupage-keyring-mcp@latest", "--school", "myschool"]
    }
  }
}
```

**Any other client:** run `uvx edupage-keyring-mcp@latest --school myschool` as
a stdio server.

### Ask something

Ask Claude about school, for example "What's on the timetable tomorrow?". The
first time, Claude opens your school's EduPage in a Chrome window (Claude
Desktop asks you first). Sign in there the way you always do: password,
two-factor or QR code. The window closes by itself, the session is saved to the
Credential Store, and Claude answers. If macOS asks whether Python may use the
Keychain, choose **Always Allow**.

Prefer to sign in before connecting a client? Run
`uvx edupage-keyring-mcp login myschool` (this works for the Claude Desktop
extension too: both use the same Saved Session).

### Options

Add these to the `args` list (or after `--school myschool` on the command line).
The Claude Desktop extension has only the School and Keep-alive fields, so of
these it takes several schools and account labels, written in the School field,
and the keep-alive.

- **Several schools:** repeat `--school`: `"--school", "first", "--school", "second"`,
  or separate them with commas: `"--school", "first,second"`. The first is
  the default. A school without a saved session reports `AUTH_REQUIRED` and
  does not affect the others.
- **Two accounts at one school** (say, as a parent and as a teacher): give each
  its own label, for example `"--school", "myschool=teacher"`, and sign in to each
  once. The label only names its Credential Store entry.
- **No sign-in windows from the server:** `"--no-browser-signin"`. `sign_in`
  then reports `SIGNIN_UNAVAILABLE`, and you sign in with `login` in a terminal
  (see [Command line](#command-line)).
- **Keep-alive (on by default):** EduPage signs a session out after about 2
  hours (sometimes under 2) without use. So every 60 minutes the server loads
  the EduPage home page of each school it has already used. Change the interval
  with `"--keep-alive", "90"` (5 to 120; smaller values count as 5), or turn it off with
  `"--keep-alive", "0"`. It never opens a school you haven't used since the
  client started and never opens a sign-in window. Each ping writes one line
  with the school and status code to the server log (`keep-alive myschool READY`).
  If EduPage has ended the session anyway, the server stops for that school
  until you next use it. It only runs
  while the client is open and the computer is awake: after a night asleep you
  may still need to sign in again. In the Claude Desktop extension, this is the
  **Keep-alive (minutes)** field.
- **Attachments on EduPage file servers:** if `get_attachment` reports
  `REDIRECT_NOT_ALLOWED`, allow that exact host with
  `"--download-host", "cloud-4.edupage.org"`. Only `*.edupage.org` hosts are accepted.

### Updates

- **uvx:** `@latest` makes `uvx` check PyPI each time the client starts the
  server, so a new release is picked up on the next client restart.
- **Claude Desktop extension:** updated by hand only. Download the new
  [`edupage-keyring-mcp.mcpb`](https://github.com/stefanbackor/edupage-keyring-mcp/releases/latest/download/edupage-keyring-mcp.mcpb)
  and install it over the old one.

What changed is on the [releases page](https://github.com/stefanbackor/edupage-keyring-mcp/releases).

### Windows (beta)

Windows support is **beta** until someone has done a full sign-in on a real
Windows machine; please [report](https://github.com/stefanbackor/edupage-keyring-mcp/issues)
how it goes.

- **Google Chrome** must be installed; sign-in opens it.
- **Claude Desktop extension:** nothing else to install, as on macOS.
- **uvx:** install [uv](https://docs.astral.sh/uv/getting-started/installation/)
  (for example `winget install --id=astral-sh.uv -e`, or
  `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`).
  uv fetches Python itself; an existing Python 3.10 or newer works too.
- **Where the session is kept:** Windows Credential Manager. To see or delete
  it, open **Control Panel → Credential Manager → Windows Credentials** and
  look for the entries under `edupage-keyring-mcp` (or run
  `uvx edupage-keyring-mcp logout myschool`).

## Signing in again

A session lasts until EduPage expires it, which it does after about 2 hours
without use. While your client is open, the [keep-alive](#options) prevents
that. Once it has expired anyway (say, after the computer slept), Claude does the same as the
first time: it gets `AUTH_REQUIRED` and asks whether it may open the sign-in.
If you agree, it calls `sign_in`, and a Chrome window opens on your school's
EduPage. While you sign in, Claude waits with
`wait_for_sign_in`, which returns as soon as you're done, and carries on with
your question. No restart is needed, and you don't have to tell Claude you're
done. The window waits up to 10 minutes.

Set up before version 0.2 with `EDUPAGE_USERNAME`? Keep it to go on using the
session you saved then, or remove it and sign in once more.

## Command line

```text
edupage-keyring-mcp [serve] --school NAME[=LOGIN] [--school ...] [--download-host HOST] [--no-browser-signin]
                    [--keep-alive MINUTES]
edupage-keyring-mcp login NAME [--username LOGIN] [--timeout SECONDS]
edupage-keyring-mcp logout NAME [--username LOGIN]
edupage-keyring-mcp status --school NAME [...]
edupage-keyring-mcp probe --school NAME [...]   # every read tool once; statuses and counts only
```

Run each with `uvx edupage-keyring-mcp@latest ...`, also when you use the
Claude Desktop extension: the commands work on the same Saved Sessions.
`logout` removes the local
session only; EduPage itself is not logged out. `probe` is handy when reporting
a problem: its output contains no school data.

## Features

36 tools. Each takes an optional `school`. Child tools take `child`: a first
name (accents optional, so `simon` finds Šimon), full name, student ID, or the
`ref` from `list_children`.

### Account and children

| Tool | Returns |
| --- | --- |
| `auth_status` | Whether the saved session works, and how a sign-in started by `sign_in` went |
| `sign_in` | Opens EduPage's sign-in page in Chrome when the session is missing or expired |
| `wait_for_sign_in` | Waits (up to 50 s per call) for that sign-in to finish, then reports the new session |
| `list_schools` | Schools, session status, role (parent, student, teacher), number of children |
| `list_children` | Your children: name, class, student ID, `ref` |
| `user_id`, `school_year` | Account ID and role; current school year |

### Timetables

| Tool | Returns |
| --- | --- |
| `get_timetable` | A child's lessons for a date, with substitutions applied: times, subject, teacher, room, group, topic, cancelled flag |
| `get_week_timetable` | Monday to Friday for a child |
| `get_timetable_for` | Timetable of any teacher, class, classroom or student |
| `get_periods`, `get_next_ringing_time` | Bell schedule; next lesson or break |
| `get_timetable_changes` | School-wide substitutions and cancellations for a date |
| `get_missing_teachers` | Teachers absent on a date |

### Grades and the timeline

| Tool | Returns |
| --- | --- |
| `get_grades` | Subject, title, grade, comment, weight, points and percent, class average, teacher; current or a past term |
| `get_homework` | Homework with full text, subject, author and due date |
| `get_assignments` | Homework, tests and exams |
| `get_upcoming_events` | Tests announced as events (topic and date), trips, meetings, holidays |
| `get_absences` | Absences, excuses, arrival and departure scans |
| `get_news` | School news |
| `get_notifications` | Any timeline item, optionally by EduPage type (`znamka`, `homework`, ...) |

Timeline tools look back up to 90 days (default 30). Every item has
`for_children`: which of your children it is addressed to, by name or class.
With `child` set, items addressed to a sibling are left out.

### Messages

| Tool | Returns |
| --- | --- |
| `list_messages` | Messages with sender, recipient, `for_children`, snippet, number of files |
| `get_message` | Full text and the files attached; no read receipt is sent |
| `get_attachment` | A PDF, PNG, JPEG or GIF attached to a message, up to 3 MB, base64 |

### People and school data

`get_students`, `get_all_students`, `find_student`, `get_teachers` (IDs for
`send_message`), `get_classes`, `get_classrooms`, `get_subjects`.

### Meals

`get_meals` returns a child's menus for a day (dishes,
allergens, portion weights), serving times, the current order and which menus
can be picked. It returns `MEALS_NOT_AVAILABLE` when the school has no canteen
module. The server reads the school's `/menu/` page directly, because
edupage-api 0.13.3 can't parse the page's current format.

`choose_meal`, `sign_off_meal` and `rate_meal` still go through edupage-api.
Until a release of it reads the current page, they send nothing and return
`MEAL_ACTIONS_UNAVAILABLE` with a short explanation; order, cancel and rate in
EduPage itself.

### Actions

| Tool | Does | Checks |
| --- | --- | --- |
| `send_message` | Sends an EduPage message as you | Recipient must be a real teacher, student or parent ID; 1 to 5,000 characters |
| `choose_meal` | Orders menu A to H for a child | Meal type, date within 30 days, menu must be choosable |
| `sign_off_meal` | Cancels a child's meal | Meal type, date |
| `rate_meal` | Rates a served lunch 1 to 5 | Lunch only, last 14 days |

These are marked `destructiveHint`; clients such as Claude Desktop ask before each call.

## Security model

- **No password anywhere.** It is typed only into EduPage's page in a throwaway
  Chrome window. The Credential Store entry (a Saved Session) holds only
  EduPage's session cookie, `PHPSESSID`, plus the account ID and the login name
  EduPage reports. Sessions saved by older versions with more cookies keep
  working until they expire.
  No username is needed either. The server refuses to start if any `EDUPAGE_*`
  environment variable other than the optional `EDUPAGE_USERNAME` label is set.
- **Sign-in from the server stays local.** `sign_in` takes only a configured
  school and runs the same `login` command in a separate process. It opens a
  window only when the saved session is missing or expired, at most one per
  school at a time, and returns only that command's status code. The window
  shows EduPage's real login page, so the most a misbehaving model can do is
  open it. Turn it off with `--no-browser-signin`; the Claude Desktop
  extension has no such setting, so use the uvx install if you need it.
- **OS Credential Store only.** Saved Sessions go only to the macOS Keychain
  or Windows Credential Manager, through `keyring`'s built-in backends. With
  any other `keyring` backend (third-party password managers, file-based
  keyrings) or on another platform the server refuses to run with
  `CREDENTIAL_STORE_REQUIRED`, so sessions never land in a plaintext keyring
  file. See [ADR 0001](docs/adr/0001-os-credential-stores-only.md). On Windows
  the entry is kept on this computer only and never roams with a domain
  profile.
- **Confined network.** `edupage-api` gets a replacement session that allows
  HTTPS to your configured school host only, checks every redirect hop, caps
  every response at 8 MB (attachments at 3 MB) and ignores proxy and `.netrc`
  settings. Cookies loaded from the Credential Store are validated for that host first.
- **Narrow inputs.** No tool accepts a URL, cookie, session ID, header or
  password. Dates are ISO and window-limited. `school` only picks a configured
  school. Attachments are fetched by file id; the model never sees their URLs.
- **Each child call picks its child.** EduPage remembers which child a parent
  session last selected, and some pages (grades, menus) show that child. So
  every child-scoped call selects its own child immediately before reading,
  under a per-school lock; if selecting fails, the session is dropped. Pages
  for the parent (timeline, messages, sending) are not affected by the
  selection: a message sent after selecting a child still comes from the
  parent.
- **Quiet errors.** Failures return fixed codes (and at most an exception class
  name), never server pages or messages.
- **Untrusted content.** All school text is labelled `untrusted_content`.
- **Pinned EduPage library.** `edupage-api` is pinned to an exact, reviewed
  version; other dependencies take compatible updates within their major version.

Reading can still be visible to EduPage, like opening a page in the browser.

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## Contributing

Development setup, tests and releases are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Credits

Built on [edupage-api](https://github.com/EdupageAPI/edupage-api), a community
Python library for EduPage accounts, licensed under GPL-3.0. It does the
EduPage requests and parsing; this project adds the Credential Store sessions, browser
sign-in, network confinement and the MCP tools. Its version is pinned exactly,
so a new `edupage-api` release reaches users only through a release of this
project.

## Disclaimer

This is an independent open-source project. It is not affiliated with,
endorsed by, or supported by aSc Applied Software Consultants, s.r.o., the
company that makes EduPage. The EduPage name is used only to say which service
this software works with. All EduPage access goes through
[`edupage-api`](https://github.com/EdupageAPI/edupage-api), an independent
community library that is not part of this project either. It relies on
EduPage's undocumented web endpoints, which can change without notice; when
they do, this server may stop working until `edupage-api` and then this project
are updated. Use it at your own risk.

## License

[GPL-3.0-or-later](LICENSE). Copyright © 2026 Stefan Backor.
