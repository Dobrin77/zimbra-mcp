# Zimbra MCP server for Claude

[Български](README.bg.md)

A remote, multi-user MCP server that lets staff connect **Claude** (claude.ai, Desktop, mobile) to their own **Zimbra** mail, calendar and contacts as a custom connector.

Based on [jeremie-lesage/zimbra-mcp](https://github.com/jeremie-lesage/zimbra-mcp) (MIT). Added: multi-user mode, passkey sign-in, Zimbra preauth, LDAP account checks and a calendar-only CalDAV gateway for Outlook.

## How it works

```
Claude ──HTTPS + OAuth──▶ Caddy (mcp.example.com:443) ──▶ zimbra-mcp ──SOAP (preauth)──▶ Zimbra
                                                              └────LDAP (read-only)───────▶ Zimbra LDAP
```

- **No passwords.** Users never enter their mail password anywhere. They identify themselves to Claude with a **passkey** (Windows Hello, fingerprint, phone). A passkey is bound to the server's address and does not work on a phishing site.
- **Preauth.** The server signs in to Zimbra on the user's behalf with the domain preauth key, which stays on the server only.
- **LDAP.** Every active Zimbra account can use it. A suspended or deleted account loses access within 5 minutes.
- **Sending only after confirmation.** Claude shows the message (recipients, subject, body) in the chat and sends only after the user confirms. No copy is kept in Sent, which saves space; instead the sender gets a BCC copy in their Inbox (useful when Outlook uses POP3). Recipients and subject are written to the server log. With `ZIMBRA_ENABLE_SEND=false` Claude only writes drafts. Permanent deletion is disabled; deleted messages go to the Trash.
- **Passkey enrollment** is only possible from the internal network, with a one-time code delivered straight to the user's Inbox. Staff outside the office get a personal one-time invite from the admin by phone or SMS. It replaces the internal-network requirement, but not the code from the mailbox.
- **Web UI language.** The sign-in, enrollment, invite and calendar pages are available in English (default) and Bulgarian, selected with `UI_LANG`.

## Tools

| Mail | Calendar | Contacts | Tags |
|---|---|---|---|
| search_emails, get_email, get_attachment, list_folders, search_folder, move_emails, mark_as_read, delete_emails (to Trash), create_draft, send_email, list_send_identities (send from a shared address with the Zimbra Send As right) | get_calendar_events, get_event_details, create_event, get_free_busy | search_contacts, get_contact, create_contact, update_contact, delete_contact | list_tags, create_tag, delete_tag, add_tag_to_emails, remove_tag_from_emails |

## Calendar in Outlook (CalDAV gateway)

The server also includes a calendar-only CalDAV gateway at `/dav/`, so staff can sync their Zimbra calendar with classic Outlook through the free [CalDav Synchronizer](https://caldavsynchronizer.org) add-in **without entering their mail password**. Each user creates a per-device, calendar-only token on the `/calendar` page (after passkey sign-in). Tokens give no access to mail, contacts or Briefcase, and can be revoked by the user or by the admin (`zimbra-mcp-admin dav-tokens`, `dav-revoke`). See section 8 of the install guide.

## Local Outlook mail

This server only sees mail that Zimbra still keeps. For older mail that exists only in classic Outlook on the computer (PST files), see the separate project [outlook-local-mcp](https://github.com/Dobrin77/outlook-local-mcp), which runs locally with Claude Desktop.

## Installation

See **[deploy/INSTALL.md](deploy/INSTALL.md)** (Bulgarian: [deploy/INSTALL.bg.md](deploy/INSTALL.bg.md)). Deployment uses Docker Compose with Caddy (automatic Let's Encrypt certificates). Configuration is in `.env` (see `.env.example`); secrets live in `./secrets/`.

## Development

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[test]"
.venv/Scripts/pytest
```

On Linux/macOS use `.venv/bin/` instead of `.venv/Scripts/`.

## Status and support

This project was built for one organization's needs and is shared as-is in the hope that it helps others running Zimbra. There are no support guarantees. Issues and pull requests are welcome, but they are answered on a best-effort basis.

## Security

Please do not report security problems in public issues. Use GitHub's private vulnerability reporting instead: the **Security** tab of this repository → **Report a vulnerability**.

## Acknowledgements

Written by Dobrin Tassev together with [Claude Code](https://claude.com/claude-code) (Anthropic), which did most of the coding, testing and the security review.

## License

MIT, see [LICENSE](LICENSE).
