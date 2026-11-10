# Signing in again uses EduPage's own "Remember password", never a stored password

EduPage ends a session after about two hours without use, and the keep-alive cannot run while the computer sleeps, so with only a Saved Session the person signs in again most mornings. To spare them that, we keep what EduPage itself issues when the person ticks "Remember password" in the dialog it shows after signing in: two long-lived cookies (`edusrs`, `egdid`) by which EduPage lists the account as remembered and hands out a short token that signs it in. We store those cookies as a Remembered Account and replay EduPage's own remembered sign-in when a tool call finds the session ended. The person's tick in EduPage's dialog is the only switch; there is no setting.

## Considered Options

- **Keep the password typed in the sign-in window.** Built first, behind a `--remember-password` flag, then dropped: it stores a secret that works everywhere the person reuses it and until they change it, it needs code that reads EduPage's password field, and it cannot sign in an account with two-factor.
- **Read the password the browser already saved.** Not possible for Safari and the Passwords app (macOS releases those items to Apple's AutoFill only), and for Chrome it means unlocking every password Chrome holds.
- **Let Chrome fill the sign-in window from a profile of its own.** A window still opens after every expiry.

## Consequences

- The project still never reads, stores or sends a password. A Remembered Account is nevertheless a lasting secret (EduPage sets it for 400 days), where a Saved Session dies within hours; the person accepts that in EduPage's dialog, under EduPage's own warning about shared computers.
- Each sign-in window decides: the cookies are kept only if EduPage, asked with them alone, lists a remembered password for that school, and a sign-in without the tick deletes an earlier Remembered Account. So does `logout`, and so does any answer from EduPage that the account is no longer remembered, refused, or needs a two-factor step.
- It is used only on demand, never by the keep-alive, and sent only to the school's own host. The Credential Store rule of ADR 0001 applies to it unchanged.
- It rests on EduPage's undocumented sign-in RPC (`getData`, `storedUsers`), checked against a live account on 2026-11-10. If that changes, nothing is remembered and the sign-in window works as before.
