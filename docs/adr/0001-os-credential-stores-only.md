# Saved Sessions live only in the OS Credential Store

To support Windows, we save Saved Sessions only in the operating system's own Credential Store: the macOS Keychain or Windows Credential Manager, through the built-in `keyring` backends. Any other `keyring` backend is refused, including third-party password-manager plugins (Bitwarden, KeePassXC, 1Password, `pass`) and file-based keyrings (`keyrings.alt`, `keyrings.cryptfile`). The project promises that the session is protected by the OS account and needs no extra unlock step or setup. Third-party plugins vary in quality and maintenance, often shell out to a CLI with its own unlock flow, and some store secrets as plain files. Linux Secret Service/KWallet are OS-native and fit this rule; they are left out for now only to limit scope.

The same rule covers Remembered Accounts (ADR 0002).
