---
name: certipy
description: Certipy CLI for AD CS discovery and abuse; sandbox-safe invocation and output handling.
---

# Certipy

Installed at `/opt/osint-venv` (isolated venv) and linked as `certipy`.

```bash
certipy find -u user@domain -p 'password' -dc-ip <dc> -vulnerable -stdout
certipy req  -u user@domain -p 'password' -ca <CA-NAME> -template <TEMPLATE> -dc-ip <dc>
certipy auth -pfx user.pfx -dc-ip <dc>
```

Rules:
- Run `find` before any exploitation; it enumerates templates and CA
  settings, and prints ESC identifiers (`ESC1`, `ESC8`, ...). Do not assert a
  vulnerability without that evidence.
- `-stdout` keeps output in the transcript instead of writing JSON only.
- Certificates and hashes are credentials: keep them out of reports.
