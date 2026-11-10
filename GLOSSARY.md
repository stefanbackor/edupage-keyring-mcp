# EduPage Keyring

An MCP server that reads and acts on EduPage school accounts using a browser sign-in session kept in the operating system's protected secret store, never a password.

## Language

**Saved Session**:
The EduPage session cookie from one browser sign-in, together with the EduPage account it belongs to, stored for one school (and optionally one account label).
_Avoid_: Credentials, token, login

**Remembered Account**:
What EduPage issues when a person ticks "Remember password" in its own sign-in dialog, kept for one school together with the EduPage account it is for. EduPage accepts it in place of the password, so it yields a new Saved Session.
_Avoid_: Saved password, credentials, token, remember-me

**Credential Store**:
The operating system's own protected secret store where Saved Sessions and Remembered Accounts live: the macOS Keychain or Windows Credential Manager. Third-party password managers and file-based keyrings are not Credential Stores.
_Avoid_: Keychain (when meaning any platform), keyring, password manager, vault
