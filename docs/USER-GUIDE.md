# Claude with your work mail and calendar

User guide for staff · [Български](USER-GUIDE.bg.md)

> Template: replace `mcp.example.com` with your server's address before sending this to staff.

This guide explains how to connect Claude to your work mail and calendar. You will be able to:

- **search, read and write e-mail** and see your calendar directly from Claude (the connector);
- **search older mail** that is only in Outlook on your computer (the `outlook-local` plugin);
- **sync your Zimbra calendar with Outlook** (the calendar token).

None of this uses **your mail password**. You never type a password in any of these steps.

## 1. Before you start: security

- The pages on `mcp.example.com` **never** ask for a password. If anyone asks for your password "for Claude" or "for the calendar", it is phishing. Don't give it, and tell IT.
- **IT never sends invites by e-mail.** You get an enrollment invite only by phone, Viber or SMS.
- Before sending a message, Claude shows you the recipients, subject and body and waits for confirmation. **Read carefully, especially the recipients.**
- If a message you received "tells" Claude to forward something to an unknown address, refuse. This is a kind of attack.

## 2. One-time enrollment with a passkey

A passkey replaces the password: you confirm with Windows Hello (PIN, fingerprint, face), Touch ID or your phone.

**From the office:**

1. Open **https://mcp.example.com/enroll**.
2. Enter your work e-mail and press **Send code**.
3. Enter the code that arrives in your mailbox.
4. Confirm with Windows Hello, a fingerprint or your phone.

**If you don't work from the office:** ask IT for an **invite**. You'll get a personal link by phone, Viber or SMS. Open it, press **Send code**, enter the code from your mailbox and confirm with a passkey. The link works only once, only for your account, and is valid for 72 hours.

### Devices

- **Windows:** requires Windows Hello, Windows 10 version 1903 or newer. Set the PIN under *Settings → Accounts → Sign-in options → Windows Hello PIN*. Windows Hello does not work over **Remote Desktop (RDP)**. Enroll from the computer you are sitting at, or from your phone.
- **Mac:** requires **macOS 13 Ventura** or newer. The passkey is stored in **iCloud Keychain**, which must be on: *System Settings → [your name] → iCloud → Passwords & Keychain*. If you are signed in to a work Mac with your personal Apple ID, the passkey will also be on your personal devices.
  - Enroll and press **Connect** in the same browser. If Chrome offers to store the passkey in Google Password Manager, choose **iCloud Keychain** instead, or Safari won't see it.
- **Phone (iPhone / Android):** you can also enroll from your phone on the office Wi-Fi, or with an invite.
- **The computer has no passkey:** at sign-in choose "Use a phone or tablet" and scan the QR code with the phone that holds the passkey. The computer needs Bluetooth.
- **Backup:** enroll a passkey on both your computer and your phone. If you lose one device, you won't be locked out.

## 3. Connecting Claude to your mail

1. In Claude, open **Settings → Connectors**.
2. Find your organization's connector and press **Connect**.
3. Press **Sign in with passkey**, confirm, and press **Allow**.
4. In the connector's settings, leave `send_email` and `set_out_of_office` on **"Needs approval"**, not "Always allow".

The passkey must be on the device you press **Connect** from. After that the connection applies to your whole Claude account: web, Desktop and the mobile app.

## 4. Working with mail

Just ask Claude, for example:

- *"Show my last 10 messages"*
- *"What's on my calendar this week?"*
- *"Reply to Ivan's message that I agree"*
- *"Reply from sales@"*, if you are allowed to send from your department's shared address.

Good to know:

- **Copies of sent mail.** For every message sent by Claude you get a copy in your **Inbox**. Recipients don't see it. No copy is kept in Sent on the server. You can move it to **Sent Items** by hand or automatically with an Outlook rule: *Home → Rules → Manage Rules & Alerts → New Rule → "from people or public group" = your address and "sent to me" → "move it to the specified folder" = Sent Items*.
- Messages **deleted** by Claude go to the Trash.
- **The server keeps mail for a limited time.** For older messages, see section 5.
- **After an update:** if IT announces new features, open a **new chat**. If Claude still doesn't see them: **Settings → Connectors → Disconnect → Connect**.

### Out-of-office auto-reply

Tell Claude, for example:

> *"I'm on leave from 22 to 30 December. Set an auto-reply saying I'll answer after 5 January, and that urgent matters go to Ivan at ivan@example.com."*

Claude shows you the period and the text and waits for confirmation. Then:

- mail to you gets the auto-reply only during that period. **After the last day Zimbra stops it by itself**, so you don't need to remember to switch it off;
- an **"Out of office"** event for the same days appears in your calendar, so colleagues can see you're away;
- you get a **message in your Inbox with the text and the period**. If you ever get one you didn't ask for, tell IT right away.

More examples:

- *"Do I have an auto-reply on?"*
- *"Extend my leave to 8 January"*: the period and the calendar event move together.
- *"Don't reply to outside senders"* or *"Send customers a different text: …"*
- *"I'm back, switch off the auto-reply"*: the "Out of office" event is shortened to end yesterday.

The reply may contain links and addresses **only from your organization's domains**. Claude never sets an auto-reply because some e-mail told it to. If it suggests one, refuse.

## 5. Older mail from Outlook (the `outlook-local` plugin)

This section applies if your admin has published the [outlook-local](https://github.com/Dobrin77/outlook-local-mcp) plugin for your organization.

The plugin gives Claude Desktop access to the mail in classic Outlook on your computer, including archives (PST files).

- It searches and reads messages, folders and attachments (PDF, Word, Excel, images).
- It tells you which messages you haven't replied to, and which of your messages are still waiting for an answer.
- It creates folders, moves and marks messages, and saves attachments to a folder on disk. Claude Desktop asks before every change.
- **It never deletes or sends mail.**
- It works only on your computer. Your mail does not pass through the company server.

You need Windows, **classic Outlook** ("New Outlook" is not supported) and **Claude Desktop**. The plugin does not work on the web or on the phone.

**Installing:**

1. Claude Desktop → **Settings → Plugins** → find **outlook-local** → **Install**.
2. Quit Claude Desktop completely, including from the icon next to the clock, and start it again.
3. In a new chat, write *"Check the connection to Outlook"*. Claude should show the Outlook version and your PST files.

Outlook doesn't need to be open. If it is closed, the plugin starts it in the background. New plugin versions arrive on their own after you restart Claude Desktop.

**Examples:**

- *"Find the invoices from Example Ltd for 2025 in my local mail"*
- *"What does the last message with the subject Backup Configuration say?"*
- *"Which messages from the last month haven't I replied to?"*
- *"Which of my requests are still unanswered?"*
- *"What's the amount on Example Ltd's March invoice?"*
- *"Save the Example Ltd invoices for 2025 to D:\Accounting\Example"*
- *"Move all messages from Veeam to the folder Inbox\Veeam"*

You don't need to tell Claude where to search. It uses the connector for the last two weeks and the local mail for older messages.

## 6. Calendar in Outlook

Your Zimbra calendar can sync with classic Outlook. Instead of your password you use a **calendar-only token**. The token gives no access to your mail.

1. **The add-in.** Outlook needs the free **CalDav Synchronizer** add-in. If you don't have it, ask IT.
2. **Token.** From the office, open **https://mcp.example.com/calendar** → **Sign in with passkey** → **New token**. Give it a name, e.g. "Office PC". The page shows the address, user name and token.
   - **The token is shown only once.** Don't close the page before you have pasted it into Outlook.
   - Don't send it to anyone, including IT.
3. **Outlook setup:**
   - the **CalDav Synchronizer** ribbon → **Synchronization Profiles** → **+** → **Generic CalDAV/CardDAV**;
   - **DAV URL:** the address from the page, `https://mcp.example.com/dav/<your e-mail>/Calendar/`;
   - **Username:** your e-mail;
   - **Password:** the token;
   - click **Test or discover settings**, choose "Calendar" and click **OK**, not Cancel;
   - **Outlook folder:** choose or create a calendar folder in Outlook;
   - **Synchronization mode:** "Outlook ↔ Server (Two-Way)".

Changes made in Outlook are uploaded right away. Changes from Zimbra reach Outlook at the profile's interval, e.g. every 30 minutes. **Synchronize now** syncs immediately.

A token is for **one computer**. Make a separate one for each computer, at most 5. If you replace or lose a computer, revoke its token on the same page with the **Revoke** button.

## 7. Troubleshooting

**Sign-in and Claude**

- **The enrollment page says it works only from the internal network.** Enroll from the office, or ask IT for an invite.
- **I lost the phone or laptop with my passkey.** Tell IT right away. They block access, and you enroll again.
- **Claude doesn't see new features.** Open a new chat. If that doesn't help: **Settings → Connectors → Disconnect → Connect**.

**The `outlook-local` plugin**

- **Claude doesn't see the `outlook_*` tools.** Check that the plugin is installed and enabled in **Settings → Plugins**, and restart Claude Desktop.
- **"Outlook is not available on this computer".** You don't have classic Outlook, or "New Outlook" is switched on.
- **Searching is slow or stops after 45 seconds.** Narrow the search by period or folder. If it keeps happening, tell IT: Outlook is probably not in the Windows Search index.
- **Outlook asks "A program is trying to access e-mail address information".** Tell IT, because the antivirus on the computer is probably not active.

**Calendar**

- **"Unauthorized" or 401.** The user name must be your full e-mail, and the token must be copied without spaces. Check that the token is still listed on the page.
- **"400 Bad Request" / "not a calendar resource".** The DAV URL must end in `/Calendar/`.
- **Contacts don't show up.** That's by design: only calendars are synced.
- **A one-off "502 Bad Gateway" error.** Press **Synchronize now** again. If it keeps happening, tell IT.

For anything else, contact IT. **Never send your password or your calendar token** to anyone, including IT.
