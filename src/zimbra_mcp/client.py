"""Zimbra SOAP Client."""

import logging
import time
from typing import Any

from pythonzimbra.communication import Communication
from pythonzimbra.exceptions.auth import AuthenticationFailed
from pythonzimbra.tools import auth

from zimbra_mcp.config import ZimbraConfig
from zimbra_mcp.errors import (
    ZimbraAuthError,
    ZimbraConnectionError,
    ZimbraNotFoundError,
    ZimbraOperationError,
)

logger = logging.getLogger(__name__)

# Re-authenticate (via preauth) well before Zimbra's default 2-day token lifetime.
TOKEN_REFRESH_SECONDS = 30 * 60

# Fault codes after which a fresh preauth token is worth one retry.
_AUTH_FAULTS = ("service.AUTH_EXPIRED", "service.AUTH_REQUIRED")

# Largest attachment returned to the model, in bytes.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


def _as_list(value: Any) -> list:
    """Zimbra's JSON gives a single child as an object and several as a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _gal_person(cn: dict[str, Any]) -> dict[str, str] | None:
    """Email and display name of a GAL entry; its fields come as an "_attrs" object."""
    attrs = cn.get("_attrs") or {}
    email = attrs.get("email")
    if isinstance(email, list):
        email = email[0] if email else None
    if not email:
        return None
    name = attrs.get("fullName") or " ".join(filter(None, [attrs.get("firstName"), attrs.get("lastName")]))
    return {"email": str(email), "name": str(name or "")}


class ZimbraClient:
    """Client for the Zimbra SOAP API, acting as one user via domain preauth."""

    def __init__(self, config: ZimbraConfig):
        """Initialize the Zimbra client.

        Args:
            config: Zimbra connection configuration
        """
        self.config = config
        self._comm: Communication | None = None
        self._token: str | None = None
        self._token_issued_at = 0.0

    def connect(self) -> None:
        """Establish connection and authenticate as the user with the domain preauth key."""
        try:
            self._comm = Communication(self.config.url, self.config.timeout, self.config.ssl_context)

            self._token = auth.authenticate(
                self.config.url,
                self.config.user,
                self.config.preauth_key,
                by="name",
                timeout=self.config.timeout,
                context=self.config.ssl_context,
                raise_on_error=True,
            )

            if not self._token:
                raise ZimbraAuthError("Zimbra authentication failed")
            self._token_issued_at = time.monotonic()

        except AuthenticationFailed as e:
            self._token = None
            raise ZimbraAuthError(f"Zimbra preauth failed for {self.config.user}: {e}")
        except ZimbraAuthError:
            raise
        except Exception as e:
            raise ZimbraConnectionError(f"Unable to connect to Zimbra: {e}")

    def dav_auth_token(self, refresh: bool = False) -> str:
        """The user's Zimbra session token, for the CalDAV gateway's ZM_AUTH_TOKEN cookie."""
        if refresh:
            self.connect()
        else:
            self._ensure_connected()
        return self._token

    def disconnect(self) -> None:
        """Close the connection."""
        self._comm = None
        self._token = None

    @property
    def is_connected(self) -> bool:
        """Check if the client is connected."""
        return self._token is not None

    def _ensure_connected(self) -> None:
        """Ensure the client is connected with a reasonably fresh token."""
        if not self.is_connected or time.monotonic() - self._token_issued_at > TOKEN_REFRESH_SECONDS:
            self.connect()

    def request(self, request_name: str, namespace: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a SOAP request to Zimbra.

        Args:
            request_name: Request name (e.g., SearchRequest)
            namespace: Request namespace (e.g., urn:zimbraMail)
            params: Request parameters

        Returns:
            Zimbra response as a dictionary
        """
        self._ensure_connected()

        if self._comm is None:
            raise ZimbraConnectionError("Client not initialized")

        try:
            response = self._send(request_name, namespace, params)
            if response.is_fault() and response.get_fault_code() in _AUTH_FAULTS:
                self.connect()
                response = self._send(request_name, namespace, params)

            if response.is_fault():
                # Get error details from the response
                fault_response = response.get_response()
                fault = fault_response.get("Fault", fault_response)
                reason = fault.get("Reason", {})
                error_msg = reason.get("Text", str(fault)) if isinstance(reason, dict) else str(fault)
                if "no such" in error_msg.lower() or "not found" in error_msg.lower():
                    raise ZimbraNotFoundError(error_msg)
                raise ZimbraOperationError(error_msg)

            response_name = request_name.replace("Request", "Response")
            return response.get_response().get(response_name, {})

        except (ZimbraNotFoundError, ZimbraOperationError, ZimbraAuthError):
            raise
        except Exception as e:
            raise ZimbraOperationError(f"Error during request {request_name}: {e}")

    def _send(self, request_name: str, namespace: str, params: dict[str, Any] | None):
        request = self._comm.gen_request(token=self._token)
        request.add_request(request_name, params or {}, namespace)
        return self._comm.send_request(request)

    def search_messages(
        self,
        query: str,
        limit: int = 50,
        offset: int = 0,
        types: str = "message",
    ) -> dict[str, Any]:
        """Search for messages.

        Args:
            query: Zimbra search query (e.g., "in:inbox from:john")
            limit: Maximum number of results
            offset: Offset for pagination
            types: Types of items to search

        Returns:
            Search results
        """
        params = {
            "query": query,
            "limit": limit,
            "offset": offset,
            "types": types,
            "fetch": "all",
        }
        return self.request("SearchRequest", "urn:zimbraMail", params)

    def get_message(self, msg_id: str, raw: bool = False) -> dict[str, Any]:
        """Retrieve a message by its ID.

        Args:
            msg_id: Message ID
            raw: If True, retrieve the raw message (MIME)

        Returns:
            Message details
        """
        params = {
            "m": {"id": msg_id, "html": 1, "raw": 1 if raw else 0},
        }
        return self.request("GetMsgRequest", "urn:zimbraMail", params)

    def get_folder(self, folder_path: str = "/") -> dict[str, Any]:
        """Retrieve a folder and its subfolders.

        Args:
            folder_path: Folder path

        Returns:
            Folder information
        """
        params = {"folder": {"path": folder_path}}
        return self.request("GetFolderRequest", "urn:zimbraMail", params)

    def move_messages(self, msg_ids: list[str], folder_id: str) -> dict[str, Any]:
        """Move messages to a folder.

        Args:
            msg_ids: List of message IDs
            folder_id: Destination folder ID

        Returns:
            Operation result
        """
        params = {
            "action": {
                "id": ",".join(msg_ids),
                "op": "move",
                "l": folder_id,
            }
        }
        return self.request("MsgActionRequest", "urn:zimbraMail", params)

    def mark_as_read(self, msg_ids: list[str], read: bool = True) -> dict[str, Any]:
        """Mark messages as read or unread.

        Args:
            msg_ids: List of message IDs
            read: True to mark as read, False for unread

        Returns:
            Operation result
        """
        op = "read" if read else "!read"
        params = {
            "action": {
                "id": ",".join(msg_ids),
                "op": op,
            }
        }
        return self.request("MsgActionRequest", "urn:zimbraMail", params)

    def delete_messages(self, msg_ids: list[str], hard_delete: bool = False) -> dict[str, Any]:
        """Delete messages.

        Args:
            msg_ids: List of message IDs
            hard_delete: If True, permanently delete; otherwise move to Trash

        Returns:
            Operation result
        """
        op = "delete" if hard_delete else "trash"
        params = {
            "action": {
                "id": ",".join(msg_ids),
                "op": op,
            }
        }
        return self.request("MsgActionRequest", "urn:zimbraMail", params)

    def send_identities(self) -> list[dict[str, Any]]:
        """Addresses the user may send from: their own, plus every mailbox whose owner or
        administrator granted them sendAs or sendOnBehalfOf in Zimbra (e.g. a department's
        shared address). Zimbra itself enforces these rights again when the mail is sent.
        """
        identities: list[dict[str, Any]] = [{"address": self.config.user, "mode": "own"}]
        seen = {self.config.user.lower(): identities[0]}
        response = self.request(
            "DiscoverRightsRequest", "urn:zimbraAccount",
            {"right": [{"_content": "sendAs"}, {"_content": "sendOnBehalfOf"}]},
        )
        for group in _as_list(response.get("targets")):
            mode = "send_as" if group.get("right") == "sendAs" else "on_behalf"
            for target in _as_list(group.get("target")):
                if target.get("type") != "account":
                    continue
                addresses = [e.get("addr") for e in _as_list(target.get("email"))] or [target.get("name")]
                for address in filter(None, addresses):
                    known = seen.get(address.lower())
                    if known is None:
                        identity = {"address": address, "mode": mode}
                        if target.get("d"):
                            identity["name"] = target["d"]
                        identities.append(identity)
                        seen[address.lower()] = identity
                    elif known["mode"] == "on_behalf" and mode == "send_as":
                        known["mode"] = mode  # sendAs is the stronger right
        return identities

    def _sender(self, from_address: str | None) -> tuple[list[dict[str, str]], str]:
        """The From (and, on behalf of someone, Sender) entries for a message.

        Returns the address entries and the address the message is sent from.
        """
        if not from_address or from_address.lower() == self.config.user.lower():
            return [{"t": "f", "a": self.config.user}], self.config.user
        for identity in self.send_identities():
            if identity["address"].lower() == from_address.lower():
                address = identity["address"]
                if identity["mode"] == "on_behalf":
                    # Recipients see "<user> on behalf of <address>".
                    return [{"t": "f", "a": address}, {"t": "s", "a": self.config.user}], address
                return [{"t": "f", "a": address}], address
        raise ZimbraOperationError(
            f"{self.config.user} may not send from {from_address}. An administrator can allow it with: "
            f"zmprov grantRight account {from_address} usr {self.config.user} sendAs"
        )

    def create_draft(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        orig_msg_id: str | None = None,
        reply_type: str | None = None,
        attach_msg_id: str | None = None,
        from_address: str | None = None,
    ) -> dict[str, Any]:
        """Create an email draft.

        Args:
            to: Recipients
            subject: Subject
            body: Message body
            cc: CC recipients
            bcc: BCC recipients
            orig_msg_id: Original message ID (for reply/forward)
            reply_type: "r" for reply, "w" for forward
            attach_msg_id: Message ID to attach as .eml (RFC 822)
            from_address: Send from this address instead of the user's own (see send_identities)

        Returns:
            Information about the created draft
        """
        addresses, _ = self._sender(from_address)
        addresses.extend([{"t": "t", "a": addr} for addr in to])
        if cc:
            addresses.extend([{"t": "c", "a": addr} for addr in cc])
        if bcc:
            addresses.extend([{"t": "b", "a": addr} for addr in bcc])

        params = {
            "m": {
                "e": addresses,
                "su": subject,
                "mp": {
                    "ct": "text/plain",
                    "content": body,
                },
            }
        }
        if orig_msg_id:
            params["m"]["origid"] = orig_msg_id
        if reply_type:
            params["m"]["rt"] = reply_type
        if attach_msg_id:
            params["m"]["attach"] = {"m": {"id": attach_msg_id}}
        return self.request("SaveDraftRequest", "urn:zimbraMail", params)

    def send_message(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        orig_msg_id: str | None = None,
        reply_type: str | None = None,
        attach_msg_id: str | None = None,
        draft_id: str | None = None,
        from_address: str | None = None,
    ) -> dict[str, Any]:
        """Send an email message.

        Args:
            to: Recipients
            subject: Subject
            body: Message body
            cc: CC recipients
            bcc: BCC recipients
            orig_msg_id: Original message ID (for reply/forward)
            reply_type: "r" for reply, "w" for forward
            attach_msg_id: Message ID to attach as .eml (RFC 822)
            draft_id: Draft ID to send (marks draft as sent)
            from_address: Send from this address instead of the user's own (see send_identities)

        Returns:
            Information about the sent message
        """
        addresses, sent_from = self._sender(from_address)
        addresses.extend([{"t": "t", "a": addr} for addr in to])
        if cc:
            addresses.extend([{"t": "c", "a": addr} for addr in cc])
        if bcc:
            addresses.extend([{"t": "b", "a": addr} for addr in bcc])
        # Instead of a Sent-folder copy on the server, the sender always gets a
        # BCC copy in the Inbox, which POP3 clients such as Outlook download.
        # Mail from a shared address also goes to that mailbox, so the whole team sees it.
        recipients = {addr.lower() for addr in to + (cc or []) + (bcc or [])}
        for copy_to in dict.fromkeys([self.config.user, sent_from]):
            if copy_to.lower() not in recipients:
                addresses.append({"t": "b", "a": copy_to})

        params = {
            "m": {
                "e": addresses,
                "su": subject,
                "mp": {
                    "ct": "text/plain",
                    "content": body,
                },
            }
        }
        if orig_msg_id:
            params["m"]["origid"] = orig_msg_id
        if reply_type:
            params["m"]["rt"] = reply_type
        if attach_msg_id:
            params["m"]["attach"] = {"m": {"id": attach_msg_id}}
        if draft_id:
            params["m"]["did"] = draft_id
        # Never keep a copy in the Sent folder (saves mailbox space), whatever the account setting.
        params["noSave"] = 1
        result = self.request("SendMsgRequest", "urn:zimbraMail", params)
        # Audit trail in the server log (recipients and subject, never the body).
        logger.info(
            "Sent mail as %s%s to %s subject=%r",
            self.config.user,
            f" from {sent_from}" if sent_from != self.config.user else "",
            ", ".join(to + (cc or []) + (bcc or [])), subject,
        )
        return result

    def get_all_tags(self) -> dict[str, Any]:
        """Retrieve all tags."""
        return self.request("GetTagRequest", "urn:zimbraMail")

    def create_tag(self, name: str, color: int | None = None) -> dict[str, Any]:
        """Create a new tag.

        Args:
            name: Tag name
            color: Tag color (0-127)

        Returns:
            Information about the created tag
        """
        tag_params: dict[str, Any] = {"name": name}
        if color is not None:
            tag_params["color"] = color

        params = {"tag": tag_params}
        return self.request("CreateTagRequest", "urn:zimbraMail", params)

    def delete_tag(self, tag_id: str) -> dict[str, Any]:
        """Delete a tag.

        Args:
            tag_id: Tag ID

        Returns:
            Operation result
        """
        params = {"action": {"op": "delete", "id": tag_id}}
        return self.request("TagActionRequest", "urn:zimbraMail", params)

    def tag_messages(self, msg_ids: list[str], tag_name: str, untag: bool = False) -> dict[str, Any]:
        """Add or remove a tag on messages.

        Args:
            msg_ids: List of message IDs
            tag_name: Tag name
            untag: If True, remove the tag instead of adding it

        Returns:
            Operation result
        """
        op = "!tag" if untag else "tag"
        params = {
            "action": {
                "id": ",".join(msg_ids),
                "op": op,
                "tn": tag_name,
            }
        }
        return self.request("MsgActionRequest", "urn:zimbraMail", params)

    def search_calendar(
        self,
        start_time: int,
        end_time: int,
        folder_id: str = "10",
    ) -> dict[str, Any]:
        """Search for calendar events.

        Args:
            start_time: Start timestamp (ms)
            end_time: End timestamp (ms)
            folder_id: Calendar folder ID (10 by default)

        Returns:
            List of events
        """
        params = {
            "query": f"inid:{folder_id}",
            "types": "appointment",
            "calExpandInstStart": start_time,
            "calExpandInstEnd": end_time,
        }
        return self.request("SearchRequest", "urn:zimbraMail", params)

    def get_appointment(self, appt_id: str) -> dict[str, Any]:
        """Retrieve event details.

        Args:
            appt_id: Event ID

        Returns:
            Event details
        """
        params = {"id": appt_id}
        return self.request("GetAppointmentRequest", "urn:zimbraMail", params)

    def create_appointment(
        self,
        subject: str,
        start_time: int,
        end_time: int,
        location: str | None = None,
        description: str | None = None,
        attendees: list[str] | None = None,
        all_day: bool = False,
    ) -> dict[str, Any]:
        """Create a calendar event.

        Args:
            subject: Event title
            start_time: Start timestamp (ms)
            end_time: End timestamp (ms)
            location: Location
            description: Description
            attendees: List of attendees
            all_day: All-day event

        Returns:
            Information about the created event
        """
        from datetime import datetime

        # Convert timestamps to Zimbra format (YYYYMMDD'T'HHMMSS or YYYYMMDD)
        start_dt = datetime.fromtimestamp(start_time / 1000)
        end_dt = datetime.fromtimestamp(end_time / 1000)

        if all_day:
            start_str = start_dt.strftime("%Y%m%d")
            end_str = end_dt.strftime("%Y%m%d")
        else:
            start_str = start_dt.strftime("%Y%m%dT%H%M%S")
            end_str = end_dt.strftime("%Y%m%dT%H%M%S")

        inv = {
            "comp": {
                "name": subject,
                "s": {"d": start_str},
                "e": {"d": end_str},
            }
        }

        if location:
            inv["comp"]["loc"] = location

        if description:
            inv["comp"]["desc"] = description

        if attendees:
            inv["comp"]["at"] = [{"a": addr, "role": "REQ"} for addr in attendees]

        if all_day:
            inv["comp"]["allDay"] = "1"

        params = {"m": {"inv": inv, "su": subject}}
        return self.request("CreateAppointmentRequest", "urn:zimbraMail", params)

    def get_prefs(self, names: list[str]) -> dict[str, str]:
        """The user's own preferences (zimbraPref*), by name; unset ones are left out.

        In Zimbra's JSON protocol name/value pairs travel as an "_attrs" object.
        """
        response = self.request("GetPrefsRequest", "urn:zimbraAccount", {"pref": [{"name": n} for n in names]})
        attrs = response.get("_attrs") or {}
        wanted = set(names)
        prefs = {}
        for name, value in attrs.items():
            if name in wanted:
                prefs[name] = value[0] if isinstance(value, list) else str(value)
        return prefs

    def modify_prefs(self, prefs: dict[str, str]) -> dict[str, Any]:
        """Change the user's own preferences; Zimbra allows only user-modifiable ones.

        They must go as an "_attrs" object: a list of <pref> elements is silently ignored in JSON.
        """
        self.request("ModifyPrefsRequest", "urn:zimbraAccount", {"_attrs": dict(prefs)})
        stored = self.get_prefs(list(prefs))
        def same(a: str, b: str) -> bool:
            return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()

        lost = sorted(name for name, value in prefs.items() if not same(stored.get(name, ""), value))
        if lost:
            raise ZimbraOperationError(f"Zimbra did not save the preferences: {', '.join(lost)}")
        return stored

    def create_all_day_event(
        self,
        subject: str,
        first_day: str,
        last_day: str,
        uid: str,
        description: str = "",
        free_busy: str = "B",
    ) -> dict[str, Any]:
        """Create an all-day event in the user's calendar, without attendees.

        Args:
            subject: Event title
            first_day: First day, YYYYMMDD
            last_day: Last day (inclusive), YYYYMMDD
            uid: iCalendar UID, so the event can be found again
            description: Plain-text description
            free_busy: B (busy), O (out of office), T (tentative) or F (free)
        """
        comp = {
            "name": subject,
            "uid": uid,
            "allDay": "1",
            "fb": free_busy,
            "fba": free_busy,
            "transp": "O",
            "class": "PUB",
            "s": {"d": first_day},
            "e": {"d": last_day},
        }
        message: dict[str, Any] = {"inv": {"comp": comp}, "su": subject}
        if description:
            message["mp"] = {"ct": "text/plain", "content": description}
        return self.request("CreateAppointmentRequest", "urn:zimbraMail", {"m": message})

    def trash_item(self, item_id: str) -> dict[str, Any]:
        """Move a calendar item (or any item) to the Trash."""
        return self.request("ItemActionRequest", "urn:zimbraMail", {"action": {"id": item_id, "op": "trash"}})

    def get_free_busy(
        self,
        email: str,
        start_time: int,
        end_time: int,
    ) -> dict[str, Any]:
        """Retrieve user availability.

        Args:
            email: User email
            start_time: Start timestamp (ms)
            end_time: End timestamp (ms)

        Returns:
            Availability information
        """
        params = {
            "s": start_time,
            "e": end_time,
            "uid": email,
        }
        return self.request("GetFreeBusyRequest", "urn:zimbraMail", params)

    def get_free_busy_many(self, emails: list[str], start_time: int, end_time: int) -> list[dict[str, Any]]:
        """Free/busy of several users in one request; one "usr" entry per address."""
        params = {"s": start_time, "e": end_time, "uid": ",".join(emails)}
        return _as_list(self.request("GetFreeBusyRequest", "urn:zimbraMail", params).get("usr"))

    def search_gal(self, name: str = ".", limit: int = 500, offset: int = 0) -> tuple[list[dict[str, str]], bool]:
        """People in the organisation's address book (GAL), as the user sees it.

        "." matches everyone. Returns [{"email", "name"}] and whether more entries remain.
        """
        params = {"name": name, "type": "account", "limit": limit, "offset": offset}
        response = self.request("SearchGalRequest", "urn:zimbraAccount", params)
        people = [_gal_person(cn) for cn in _as_list(response.get("cn"))]
        return [p for p in people if p], bool(response.get("more"))

    def get_group_members(self, group: str) -> list[str]:
        """Addresses in a distribution list (department group) the user may see."""
        params = {"dl": {"_content": group}, "limit": 1000}
        response = self.request("GetDistributionListMembersRequest", "urn:zimbraAccount", params)
        members = []
        for m in _as_list(response.get("dlm")):
            value = m.get("_content") if isinstance(m, dict) else m
            if value:
                members.append(str(value))
        return members

    def search_contacts(
        self,
        query: str,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Search for contacts.

        Args:
            query: Zimbra search query
            limit: Maximum number of results
            offset: Offset for pagination

        Returns:
            Search results
        """
        params = {
            "query": query,
            "limit": limit,
            "offset": offset,
            "types": "contact",
            "fetch": "all",
        }
        return self.request("SearchRequest", "urn:zimbraMail", params)

    def get_contact(self, contact_id: str) -> dict[str, Any]:
        """Retrieve a contact by its ID.

        Args:
            contact_id: Contact ID

        Returns:
            Contact details
        """
        params = {
            "cn": {"id": contact_id},
        }
        return self.request("GetContactsRequest", "urn:zimbraMail", params)

    def create_contact(self, folder_id: str | None, attributes: list[dict[str, str]]) -> dict[str, Any]:
        """Create a contact.

        Args:
            folder_id: Folder ID (None for default Contacts folder)
            attributes: List of {"n": name, "_content": value} dicts

        Returns:
            Information about the created contact
        """
        cn: dict[str, Any] = {"a": attributes}
        if folder_id:
            cn["l"] = folder_id

        params = {"cn": cn}
        return self.request("CreateContactRequest", "urn:zimbraMail", params)

    def modify_contact(self, contact_id: str, attributes: list[dict[str, str]]) -> dict[str, Any]:
        """Modify a contact.

        Args:
            contact_id: Contact ID
            attributes: List of {"n": name, "_content": value} dicts

        Returns:
            Updated contact information
        """
        params = {
            "cn": {
                "id": contact_id,
                "a": attributes,
            },
        }
        return self.request("ModifyContactRequest", "urn:zimbraMail", params)

    def delete_contacts(self, contact_ids: list[str]) -> dict[str, Any]:
        """Delete contacts.

        Args:
            contact_ids: List of contact IDs

        Returns:
            Operation result
        """
        params = {
            "action": {
                "id": ",".join(contact_ids),
                "op": "delete",
            }
        }
        return self.request("ContactActionRequest", "urn:zimbraMail", params)

    def get_attachment_content(self, msg_id: str, part_id: str) -> tuple[bytes, str, str]:
        """Retrieve attachment content via REST API.

        Args:
            msg_id: Message ID
            part_id: Part ID of the attachment

        Returns:
            Tuple of (content_bytes, filename, content_type)
        """
        import urllib.request
        import urllib.error
        from urllib.parse import urlencode

        self._ensure_connected()

        # Build REST URL for attachment download; the token travels in the
        # ZM_AUTH_TOKEN cookie rather than the query string so it never lands in access logs.
        base_url = self.config.url.replace("/service/soap", "")
        params = urlencode({
            "id": msg_id,
            "part": part_id,
            "auth": "co",
        })
        url = f"{base_url}/service/home/~/?{params}"

        try:
            req = urllib.request.Request(url, headers={"Cookie": f"ZM_AUTH_TOKEN={self._token}"})
            with urllib.request.urlopen(
                req, timeout=self.config.timeout, context=self.config.ssl_context
            ) as response:
                content = response.read(MAX_ATTACHMENT_BYTES + 1)
                if len(content) > MAX_ATTACHMENT_BYTES:
                    raise ZimbraOperationError(
                        f"Attachment is larger than {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB"
                    )
                content_type = response.headers.get("Content-Type", "application/octet-stream")

                # Extract filename from Content-Disposition header
                content_disp = response.headers.get("Content-Disposition", "")
                filename = "attachment"
                if "filename=" in content_disp:
                    import re
                    match = re.search(r'filename[*]?=["\']?([^"\';\n]+)', content_disp)
                    if match:
                        filename = match.group(1).strip()

                return content, filename, content_type

        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise ZimbraNotFoundError(f"Attachment not found: msg_id={msg_id}, part_id={part_id}")
            raise ZimbraOperationError(f"Failed to download attachment: {e}")
        except ZimbraOperationError:
            raise
        except Exception as e:
            raise ZimbraOperationError(f"Failed to download attachment: {e}")

    def add_message(self, raw_mime: str, folder_id: str = "2") -> dict[str, Any]:
        """Deliver a message straight into one of the user's folders (Inbox by default).

        Used for enrollment codes and security notices: the message never leaves
        the Zimbra server, so it cannot be intercepted or spoofed from outside.
        """
        # AddMsgRequest needs <content> as a child element; a plain string would be sent as an attribute.
        params = {"m": {"l": folder_id, "f": "u", "content": {"_content": raw_mime}}}
        return self.request("AddMsgRequest", "urn:zimbraMail", params)
