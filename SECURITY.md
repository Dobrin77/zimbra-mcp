# Security Policy

## Supported versions

Only the latest commit on `main` receives security fixes. There are no
separate release branches.

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report privately through GitHub: open the **Security** tab of this repository
and click **Report a vulnerability**. Include what you found, how to reproduce
it, and which component is affected (OAuth/passkey login, preauth, LDAP lookup,
CalDAV gateway, MCP tools, deployment files).

What to expect:

- an acknowledgement within 7 days;
- a fix or a decision within 30 days for confirmed issues;
- credit in the fix commit, if you want it.

This is a small project maintained in spare time, so timelines are best effort.

## Scope notes

The server holds a Zimbra **preauth key**, which can sign in as any user of
the domain. Findings that could expose that key, bypass passkey sign-in, or let
one user reach another user's mailbox or calendar are treated as critical.
