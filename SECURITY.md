# Security policy

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** for this repository
(Security → Report a vulnerability). Do not open a public issue.

Never include real school data, messages, attachments, cookies, Keychain items,
passwords or two-factor codes in a report. Synthetic examples are enough.

## Scope

In scope: anything that lets a tool caller (including a model) reach a host
other than the configured `*.edupage.org` school hosts, read or set
credentials or cookies, bypass the write-tool checks, see server error bodies,
or act for the wrong child or account; and any way a password or session could
end up outside the Credential Store (the macOS Keychain or Windows Credential
Manager), or a Remembered Account could be kept or used without the person
having ticked "Remember password" in EduPage's own dialog.

Out of scope: EduPage's own behaviour and the security of the person's computer.

## Supported versions

Only the latest release receives fixes.
