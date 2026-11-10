# Installing the Zimbra MCP server

[Български](INSTALL.bg.md)

The examples use the domain `example.com`, the Zimbra server `mail.example.com` and the connector address `mcp.example.com`. Replace them with your own.

This guide is for the **admin**: installing, configuring and maintaining the server. The steps for staff (enrollment, connecting Claude, the calendar) are in a separate guide that you send to everyone: [docs/USER-GUIDE.md](../docs/USER-GUIDE.md).

## 0. What you need

- A dedicated VM: **2 vCPU, 2 GB RAM, 20–40 GB disk**, **Debian 12** or **Ubuntu 24.04**. The server also runs on 1 vCPU and 1 GB, but `docker compose up --build` may then fail for lack of memory.
- If you use **Hyper-V**:
  - **Generation 2**, Secure Boot with the **"Microsoft UEFI Certificate Authority"** template, otherwise Linux will not boot from the ISO;
  - **static** RAM (no Dynamic Memory);
  - a **Dynamically expanding** VHDX disk, which only takes up the space actually used;
  - keep **Time synchronization** enabled under Integration Services, because passkeys, tokens and the certificate all depend on an accurate clock.
- A public DNS record `mcp.example.com` → your public IP, with ports **80 and 443** forwarded to the VM. Claude connects from Anthropic's servers, so the address must be reachable from the internet.
- **Internal (split) DNS:** inside the office, `mcp.example.com` must resolve to the VM's **internal** IP. Staff then reach it directly and the server sees their internal address, which the enrollment page requires.
- Network access from the VM to Zimbra: **443** (SOAP) and **389** (LDAP).

## 1. Preparing Zimbra

Run these commands on the Zimbra server as the `zimbra` user (`su - zimbra`).

### 1.1. Preauth key

First **check whether the domain already has a key**:

```bash
zmprov gd example.com zimbraPreAuthKey
```

- If it **has** a value, use that one. **Do not generate a new key**: a new key replaces the old one and breaks every other integration that uses it.
- If it **has none**, generate one:

```bash
zmprov generateDomainPreAuthKey example.com
```

Write the key down (a long hexadecimal string). It is the **master key** to every mailbox in the domain. Guard it like the root password.

### 1.2. Read-only LDAP account

```bash
zmlocalconfig -s ldap_postfix_password
zmlocalconfig ldap_url
```

The first command prints the password of `uid=zmpostfix,cn=appaccts,cn=zimbra`, the second the LDAP address.

### 1.3. Zimbra firewall

Allow port **389** only from the MCP machine's IP. Port 443 is probably open already. You can see the MCP machine's IP with `hostname -I` on it. Example with `ufw` (as root on Zimbra):

```bash
ufw allow from 192.168.10.20 to any port 389 proto tcp comment 'LDAP from MCP'
ufw status verbose
```

If Zimbra uses `firewalld` or `iptables`, add an equivalent rule there.

## 2. Preparing the VM

During the Ubuntu Server installation:

- **Hostname** `mcp`, domain `example.com`. The machine name is just for convenience; what matters are the DNS records and `.env`.
- With manual network configuration, the **Subnet** field is in CIDR format, e.g. `192.168.10.0/24`, not `255.255.255.0`. **Name servers** must be your internal DNS so that split DNS works.
- If the installer offers "Update to the new installer", you can decline. Updates are installed later with `apt upgrade`.

All commands from here on run as **root**. On Ubuntu, log in with your user and run `sudo -i` so you don't have to prefix every command with `sudo`.

```bash
apt update && apt install -y git ldap-utils ca-certificates curl
```

Install Docker following the official guide for your distribution:

- **Ubuntu:** https://docs.docker.com/engine/install/ubuntu/
- **Debian:** https://docs.docker.com/engine/install/debian/

Use the **"Install using the apt repository"** section. The distribution's `docker.io` package is older and often lacks `docker compose`. Once the `hello-world` test passes, Docker is ready. You don't need the rest of that page.

### 2.1. Checking LDAP from the VM

First check that the VM reaches Zimbra's **internal** address:

```bash
getent hosts mail.example.com
```

This should print Zimbra's internal IP. If it prints the public one, add a line to `/etc/hosts`, e.g. `192.168.10.10  mail.example.com`. Do not put an IP into `LDAP_URL` or `ZIMBRA_URL`: the TLS certificate is issued for the name, not the IP.

Then check that port 389 is open:

```bash
nc -zv -w 5 mail.example.com 389
```

- `succeeded` means the port is reachable.
- `timed out` means the Zimbra firewall is blocking the VM (section 1.3). In that case the `ldapsearch` below just hangs without asking for a password.

Finally, try a search with a real address:

```bash
ldapsearch -x -ZZ -H ldap://mail.example.com:389 \
  -D "uid=zmpostfix,cn=appaccts,cn=zimbra" -W \
  -b "ou=people,dc=example,dc=com" \
  "(zimbraMailDeliveryAddress=ivan@example.com)" \
  zimbraMailDeliveryAddress displayName zimbraAccountStatus zimbraMailStatus
```

Enter the password from section 1.2 at the `Enter LDAP Password:` prompt. You should see the account and `result: 0 Success`.

- `displayName::` with two colons is normal. When the name contains non-ASCII characters (e.g. Cyrillic), it is base64-encoded.
- If `zimbraAccountStatus` is not shown, that is normal too: `zmpostfix` is not allowed to read it. Suspended accounts are then filtered out at the Zimbra login instead.

## 3. Installation

### 3.1. Getting the code

The code goes into **`/opt/zimbra-mcp`**. Run `git clone` from `/opt`; it creates the `zimbra-mcp` folder itself:

```bash
cd /opt
git clone https://github.com/Dobrin77/zimbra-mcp.git
cd /opt/zimbra-mcp
```

If you deploy from your own private fork, add a read-only deploy key for the VM on GitHub (**Settings → Deploy keys**, with "Allow write access" unchecked) and clone over SSH.

**All commands from here on run from `/opt/zimbra-mcp`.**

### 3.2. Secrets

The `secrets/` and `data/` folders must be in `/opt/zimbra-mcp`, next to `docker-compose.yml`:

```bash
cd /opt/zimbra-mcp
mkdir -p secrets data
nano secrets/preauth_keys
nano secrets/ldap_password
```

- In `secrets/preauth_keys` write one line: the domain, a space, the key from section 1.1. For example `example.com 0123abcd...`. With several domains, write one line per domain.
- In `secrets/ldap_password` write only the password from section 1.2.

Use `nano`, not `echo`, otherwise the key ends up in the shell history (`~/.bash_history`). Don't paste them into a chat either.

Then restrict the permissions:

```bash
chown -R 10001 secrets data
chmod 700 secrets data
chmod 400 secrets/*
```

You should end up with:

```
/opt/zimbra-mcp/secrets/preauth_keys
/opt/zimbra-mcp/secrets/ldap_password
/opt/zimbra-mcp/data/
```

### 3.3. Settings

```bash
cp .env.example .env
nano .env
```

Fill in at least `PUBLIC_URL`, `MCP_HOSTNAME`, `ACME_EMAIL`, `ORG_NAME`, `ZIMBRA_URL`, `LDAP_URL` and **both `ENROLL_ALLOWED_NETWORKS` lines** with your office network (as narrow as possible, e.g. `192.168.10.0/24`). If staff are on several networks, list them all: comma-separated in `ENROLL_ALLOWED_NETWORKS`, space-separated in `ENROLL_ALLOWED_NETWORKS_CADDY`.

`TRUSTED_PROXIES` lists the reverse proxies whose `X-Real-IP` header the app believes; from any other address the header is ignored and the connection's own address counts. The default `127.0.0.1/32,::1/128,172.16.0.0/12` fits this setup, where Caddy reaches the container through Docker's bridge. Change it only if the proxy runs on another machine.

`UI_LANG` sets the language of the web pages (sign-in, enrollment, invites, calendar tokens) and of the code e-mails: `en` (default) for English, `bg` for Bulgarian.

`ZIMBRA_ENABLE_SEND` is `false` in the example: Claude only writes drafts, visible in the Zimbra web client. `true` lets Claude send mail after the user confirms it in the chat. You need this if staff use Outlook over POP3, because POP3 never shows server-side drafts. Keep `ZIMBRA_ALLOW_HARD_DELETE=false`.

Mail sent by Claude is **never saved to Sent**. Instead, the sender automatically gets a copy (BCC) in their Inbox, which Outlook downloads over POP3. The server also logs the sender, recipients and subject, without the message body. View it with `docker compose logs zimbra-mcp | grep "Sent mail"`.

### 3.4. Branding (optional)

The sign-in and enrollment pages can show your logo, colours and texts. Everything lives in `data/themes/`, outside the code, so `git pull` never overwrites it. Every file is optional. Without the folder the pages look neutral.

```bash
cd /opt/zimbra-mcp
mkdir -p data/themes
cp deploy/themes.example/* data/themes/
nano data/themes/theme.json
```

| File | What it is |
|---|---|
| `logo.svg` (or `.png`, `.jpg`, `.webp`) | the logo at the top of the card. Shown on a white background; SVG works best. |
| `favicon.png` (or `.ico`, `.svg`) | the browser tab icon |
| `theme.json` | `org_name` (the name used in texts, in the passkey and as the sender of the code e-mails; falls back to `ORG_NAME`), `welcome` (the greeting above the card), `support` (a help line below the card), `copyright` (the bottom line, e.g. `© {year} Example Ltd. All rights reserved.`; `{year}` becomes the current year) |
| `theme.css` | the colours. The example sets a primary colour for light mode and a lighter variant for dark mode. |

Each file must be at most 1 MB. After changing the theme:

```bash
chown -R 10001 data/themes
docker compose restart zimbra-mcp
```

### 3.5. Starting

```bash
docker compose up -d --build
docker compose logs -f zimbra-mcp
```

Caddy obtains a Let's Encrypt certificate by itself on first start.

## 4. Checks

1. `curl https://mcp.example.com/health` must return `{"status":"ok"}`.
2. `curl https://mcp.example.com/.well-known/oauth-authorization-server` must return JSON.
3. **From a phone on mobile data, not on the office Wi-Fi**, open `https://mcp.example.com/enroll`. You must see a message that **enrollment is only possible from the internal network**. If the page opens, your router rewrites source addresses (SNAT) and the restriction does not work. **Do not continue** until you have fixed it.
4. From the office, open `https://mcp.example.com/enroll` and enroll a passkey for yourself. The code must arrive in your Inbox.
5. Once everything works, take a **snapshot (checkpoint)** of the VM. If an update goes wrong, you can roll back to it.

## 5. Connecting in Claude

> **The connector address ends in `/mcp`:** `https://mcp.example.com/mcp`. Plain `https://mcp.example.com` does not work: there is nothing at `/` and the server returns 404.

### For the Claude Team or Enterprise admin

The connector is added once for the whole organization, from an account with admin rights:

1. **Admin settings → Connectors → Add custom connector**. Menu names change from time to time.
2. **Name:** a name staff will recognize, e.g. "Example Mail". The enrollment page tells them to look for the organization's connector, so plain "Zimbra" is not a good choice.
3. **URL:** `https://mcp.example.com/mcp`.
4. Keep what Claude detects by itself (**Detected**):
   - **Authentication:** **Sign in now**.
   - **OAuth client:** **Register automatically** (DCR). Don't fill in a Client ID or Secret.
   - **Request headers:** add nothing.
5. Click **Add**.

This makes the connector **available** to everyone, but nobody is connected automatically. Each person connects on their own with their passkey, so the admin cannot see anyone's mail. If the admin wants to use the connector, they too have to enroll and click **Connect** from their own account.

### For staff

Enrollment, connecting, devices (Windows Hello, Mac, phone) and the security rules are in the [user guide](../docs/USER-GUIDE.md), sections 1–4. Send it to everyone before you tell them the connector is ready.

With a personal Claude account (no Team), the user first adds the connector themselves: **Add custom connector** with the URL `https://mcp.example.com/mcp`.

### Staff outside the office (invites)

Staff who don't come to the office and have no VPN enroll with an **invite**: a personal one-time link that works from any network. Besides the invite they still need the code from their mailbox. So neither a stolen mail password alone nor an intercepted link alone is enough.

1. Create the invite on the MCP machine (from `/opt/zimbra-mcp`):

   ```bash
   docker compose exec zimbra-mcp zimbra-mcp-admin invite ivan@example.com
   ```

   For several people at once, list the addresses separated by spaces:

   ```bash
   docker compose exec zimbra-mcp zimbra-mcp-admin invite ivan@example.com maria@example.com
   ```

   Or put them in a file, one address per line (blank lines and anything after `#` are ignored):

   ```
   # remote staff
   ivan@example.com
   maria@example.com    # sales
   ```

   and pass it like this (note the `-T` and the `-`):

   ```bash
   docker compose exec -T zimbra-mcp zimbra-mcp-admin invite --file - < /root/invites.txt
   ```

2. The command checks every address in LDAP and prints one link per active account:

   ```
   2 invite(s), valid until 2026-11-04 21:30.
   Send each link to its owner by phone, Viber or SMS, NOT by e-mail. Each works once.

     ivan@example.com (Ivan Petrov)
       https://mcp.example.com/invite#k3J9...

     maria@example.com (Maria Ivanova)
       https://mcp.example.com/invite#Qx7a...
   ```

   A `SKIPPED` line means **no** invite was created for that address: it is misspelled, the account is suspended, or LDAP did not respond.

3. **Send each link to its owner by phone, Viber or SMS, never by e-mail.** If the link travels through the mailbox, a thief who has broken into the mailbox has both the link and the code.
4. The person opens the link, requests a code, enters the code from their mailbox and confirms with a passkey. The invite works only for their account and only once. They receive a notification e-mail that the passkey was enrolled through an admin invite.

Invites are valid for **72 hours**. For a different period add `--hours 24` (1 to 336). A new invite for the same person cancels the old one.

```bash
# Invites not used yet
docker compose exec zimbra-mcp zimbra-mcp-admin invites

# Cancel an invite (e.g. sent to the wrong person)
docker compose exec zimbra-mcp zimbra-mcp-admin cancel-invite ivan@example.com
```

### How sending works

For every message sent by Claude, the sender gets a **copy (BCC) in their Inbox**. No copy is kept in Sent on the server. Messages deleted by Claude go to the Trash. The user guide, section 4, explains how staff can move the copies to Sent Items.

## 6. Administration

```bash
# Who has passkeys and active sessions
docker compose exec zimbra-mcp zimbra-mcp-admin list

# Someone leaves or loses a laptop: removes all passkeys, sessions and invites
docker compose exec zimbra-mcp zimbra-mcp-admin revoke ivan@example.com

# Only ends their Claude sessions (passkeys stay)
docker compose exec zimbra-mcp zimbra-mcp-admin logout ivan@example.com
```

**Suspending an account in Zimbra** also cuts off access through Claude within 5 minutes, with nothing to do here.

### Sending from a shared department address

Staff can also send through Claude from a shared address, e.g. `sales@example.com`, if they have the **Send As** right in Zimbra. Rights are granted only in Zimbra and the connector reads them from there. Zimbra checks them again on every send.

On the Zimbra server, as the `zimbra` user:

```bash
# Grant the right to several people at once
for u in ivan maria petar; do zmprov grantRight account sales@example.com usr $u@example.com sendAs; done

# Or to a group, so you don't touch the rights when people in the department change
zmprov cdl sales-team@example.com zimbraHideInGal TRUE
zmprov adlm sales-team@example.com ivan@example.com maria@example.com
zmprov grantRight account sales@example.com grp sales-team@example.com sendAs

# Who has the right
zmprov getGrants -t account sales@example.com

# Revoke
zmprov revokeRight account sales@example.com usr ivan@example.com sendAs
```

- **`sendAs`:** the recipient sees only the shared address.
- **`sendOnBehalfOf`:** the recipient sees "Ivan on behalf of sales@".

The person tells Claude, for example, *"reply from sales@"*. Claude shows the message with the sender address and sends after confirmation. **Both the person and the shared mailbox** get a copy (BCC), so the rest of the department sees the message too.

With this right, staff don't need to know the shared address's password. If everyone's Outlook is also switched to sending through their own accounts, the password of `sales@` can be changed so that nobody knows it.

### Out-of-office auto-reply

Staff set it themselves through Claude (the `get_out_of_office`, `set_out_of_office` and `disable_out_of_office` tools). The connector fills in the same settings as Preferences → Out of Office in Zimbra, with a start and end date, so Zimbra stops the reply by itself after the end date.

- Together with the reply, an all-day "Out of office" event with the out-of-office free/busy status is created in the user's calendar. When the period changes it moves with it; when the reply is switched off early it is shortened to end yesterday.
- On every change the user gets a notice in their Inbox.
- The text may only contain links and addresses from the connector's domains. It is limited to 2000 characters and the period to one year.

To check one user, on the Zimbra server as the `zimbra` user:

```bash
zmprov ga ivan@example.com zimbraPrefOutOfOfficeReplyEnabled zimbraPrefOutOfOfficeFromDate zimbraPrefOutOfOfficeUntilDate zimbraPrefOutOfOfficeReply
```

To switch it off as the administrator:

```bash
zmprov ma ivan@example.com zimbraPrefOutOfOfficeReplyEnabled FALSE
```

### Backups

Back up `data/` (the passkey database) and `secrets/`. If `data/` is lost, staff simply enroll their passkeys again.

### Updating

```bash
git pull
docker compose up -d --build
```

Take a VM snapshot before updating. After updating, remove old images and build cache so they don't fill the disk:

```bash
docker system prune -f
```

Container logs are capped automatically at 50 MB per service.

Checking that the container runs the new code:

```bash
docker compose exec zimbra-mcp python -c "import zimbra_mcp; print(zimbra_mcp.__file__)"
git log --oneline -1
```

**New tools or fields:** claude.ai keeps the tool list from the moment of connecting. After an update that adds a tool or a field, staff need to open a **new chat**. If Claude still sees the old tools, use **Settings → Connectors → Disconnect → Connect**.

### Rotating the preauth key

If you suspect the key has leaked:
1. `zmprov generateDomainPreAuthKey example.com` on Zimbra.
2. Replace it in `secrets/preauth_keys`.
3. `docker compose restart zimbra-mcp`.

## 7. Local Outlook mail

The connector only sees mail that Zimbra still keeps on the server. For older mail that exists only in classic Outlook on the computer (PST files), there is a separate project: **outlook-local-mcp** (https://github.com/Dobrin77/outlook-local-mcp). It runs locally with Claude Desktop, needs no passwords and does not talk to this server. Installation and setup are described in its own repository.

## 8. Calendar in Outlook (CalDAV gateway)

Staff sync their Zimbra calendar with classic Outlook **without entering their password**. Instead of the password they get a separate **calendar-only token**. The token gives no access to mail, contacts or Briefcase.

The gateway is part of the MCP server and runs at `https://mcp.example.com/dav/`. Outlook talks to it, and it talks to Zimbra on the user's behalf, just like the connector.

### For the admin

- Install the free **CalDav Synchronizer** add-in for Outlook (https://caldavsynchronizer.org) on the computers, by hand or centrally.
- The page `https://mcp.example.com/calendar`, where staff create tokens, opens **only from the internal network**.
- Each user can have at most 5 tokens, one per computer. They get a notification e-mail for every new token.

### For staff

Getting a token and setting up Outlook are in the [user guide](../docs/USER-GUIDE.md), section 6.

### Revoking a token

- **By the user:** on https://mcp.example.com/calendar, the **Revoke** button next to the token.
- **By the admin:**

```bash
# All calendar tokens
docker compose exec zimbra-mcp zimbra-mcp-admin dav-tokens

# Revoke one token by number, or all tokens of an account
docker compose exec zimbra-mcp zimbra-mcp-admin dav-revoke 12
docker compose exec zimbra-mcp zimbra-mcp-admin dav-revoke ivan@example.com
```

`revoke <email>` from section 6 also revokes calendar tokens.

### Troubleshooting

- **"Unauthorized" or 401.** Check two things. The user name must be the full e-mail address. The token must be copied without spaces and must still be listed on the page.
- **"400 Bad Request" / "not a calendar resource".** The DAV URL points at the home folder. It must end in `/Calendar/`.
- **Contacts don't show up.** That is intentional: the gateway only exposes calendars.
- **Sync stopped after switching computers.** Create a new token for the new computer and revoke the old one.
