"""Texts shown to people: web pages, JSON error messages and Inbox notices.

The language is chosen once per installation with UI_LANG (en or bg).
Entries whose key starts with "js." are handed to webauthn.js through a JSON
data block in the page; "{name}" placeholders are filled in by the caller.
Page texts (keys with a "page." prefix) may contain HTML; values inserted into
them are escaped.
"""

from collections.abc import Callable

from markupsafe import Markup

DEFAULT_LANGUAGE = "en"

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        # Errors and message pages
        "link_expired_title": "The link has expired",
        "link_expired_text": "This sign-in request is no longer valid. Go back to Claude and try connecting the connector again.",
        "error_title": "Error",
        "opened_in_other_browser_page": "The request is already open in another browser.",
        "opened_in_other_browser": "The request is open in another browser.",
        "too_many_wait_minute": "Too many attempts. Wait a minute.",
        "restart_login": "Start signing in again.",
        "bad_browser_response": "Invalid response from the browser.",
        "passkey_not_registered_login": "This passkey is not registered. Register first from the internal network (the /enroll page).",
        "passkey_not_registered_calendar": "This passkey is not registered. Register first (the /enroll page).",
        "passkey_check_failed": "Passkey verification failed.",
        "account_inactive": "The account is not active in Zimbra.",
        "account_inactive_contact_it": "The account is not active in Zimbra. Contact the IT department.",
        "sign_in_first": "Sign in with your passkey first.",
        "bad_request": "Invalid request.",
        "request_expired": "The request has expired. Go back to Claude and try again.",
        "unavailable_title": "Unavailable",
        "enroll_internal_only": "Passkey registration is only possible from the organisation's internal network.",
        "calendar_internal_only": "This page is only available from the internal network.",
        "internal_only": "Only from the internal network.",
        "enter_email": "Enter an e-mail address.",
        "too_many_wait_15": "Too many attempts. Try again in 15 minutes.",
        "code_sent_invite": "The code has been sent to the Inbox of {email}.",
        "code_sent": "If the address is an active account, the code has been sent to your Inbox.",
        "no_enrollment": "There is no active registration for this address. Request a new code.",
        "too_many_wrong_codes": "Too many wrong attempts. Request a new code.",
        "wrong_code": "Wrong code.",
        "enroll_expired": "The registration has expired. Start again.",
        "enroll_failed": "Passkey registration failed.",
        "invite_invalid": "The invitation is invalid, used or expired. Ask the IT department for a new one.",
        "reload_page": "Reload the page.",
        "token_limit": "You have {max} tokens. Revoke an old one before creating a new one.",
        "no_such_token": "No such token.",
        # Inbox notices
        "mail_enroll_subject": "Passkey registration code for Claude",
        "mail_enroll_text": (
            "Hello, {name}!\n\n"
            "Someone (from IP address {ip}) started registering a passkey for the Claude connector to your mailbox.\n\n"
            "Registration code: {code}\n\n"
            "The code is valid for 15 minutes. Enter it only on the registration page at {url}\n"
            "No one from the IT department will ever ask you for this code.\n\n"
            "If you did not start the registration yourself, do NOT use the code and notify the IT department.\n"
        ),
        "mail_passkey_subject": "A new passkey for Claude was registered",
        "mail_passkey_text": (
            "A new passkey{label} for the Claude connector was just registered for your account {email}{invite}.\n\n"
            "If this was not you, notify the IT department immediately.\n"
        ),
        "mail_passkey_invite": " through an invitation from the IT department",
        "mail_token_subject": "A calendar token was created",
        "mail_token_text": (
            "A calendar sync token ({label}) was created for the account {email}.\n\n"
            "If this was not you, tell the IT department immediately."
        ),
        "ooo_event_name": "Out of office",
        "ooo_date_format": "%Y-%m-%d",
        "ooo_on_subject": "Your out-of-office auto-reply is on",
        "ooo_on_text": (
            "An out-of-office auto-reply was set through Claude for the account {email}.\n\n"
            "Period: {period}\n"
            "After the last day Zimbra stops replying by itself.\n\n"
            "Reply text:\n{reply}\n\n"
            "Senders outside the organisation: {external}\n\n"
            "An \"Out of office\" event for the same days was added to your calendar.\n\n"
            "To change or switch off the auto-reply, ask Claude.\n"
            "If this was not you, tell the IT department immediately.\n"
        ),
        "ooo_external_same": "get the same reply.",
        "ooo_external_none": "get no reply.",
        "ooo_external_different": "get this reply:\n{text}",
        "ooo_off_subject": "Your out-of-office auto-reply is off",
        "ooo_off_text": (
            "The out-of-office auto-reply for the account {email} was switched off through Claude.\n\n"
            "If this was not you, tell the IT department immediately.\n"
        ),
        "unnamed": "unnamed",
        "send_note_enabled": "Claude will be able to send mail on your behalf, but only after showing you the text and you confirm it.",
        "send_note_disabled": "No mail will be sent automatically – drafts only.",
        # Pages (may contain HTML)
        "page.welcome": "Claude mail connector for {org}",
        "page.never_password": "This page never asks for your mail password.",
        "page.steps": "Steps",
        "page.passkey_login": "Sign in with passkey",
        "page.no_password": "<strong>Do not enter a password</strong>.",
        "page.login_title": "Sign in",
        "page.login_step_login": "1. Sign in with passkey",
        "page.login_step_consent": "2. Permission",
        "page.login_heading": "Connect Claude to your mail",
        "page.login_intro": "Sign in with a passkey (Windows Hello, fingerprint or phone).",
        "page.consent_heading": "Confirmation",
        "page.signed_in_as": 'Signed in as <strong id="{id}"></strong>.',
        "page.consent_access": '<strong id="client"></strong> wants access to your mail, calendar and contacts at {org}. {send_note}',
        "page.consent_only_you": "Allow only if <strong>you</strong> just connected the connector in Claude.",
        "page.allow": "Allow",
        "page.deny": "Deny",
        "page.enroll_title": "Passkey registration",
        "page.enroll_step_email": "1. E-mail",
        "page.enroll_step_code": "2. Code and passkey",
        "page.enroll_step_done": "3. Done",
        "page.enroll_heading": "Register a passkey for Claude",
        "page.enroll_intro": "The passkey replaces the password when you connect Claude to your {org} mail. You will receive a one-time code in your Inbox.",
        "page.enroll_invite_note": "You are using a personal invitation from the IT department. It is valid only for your account and only once.",
        "page.enroll_email": "Work e-mail",
        "page.enroll_send_code": "Send code",
        "page.enroll_code_heading": "Enter the code",
        "page.enroll_code_intro": "Open your mail (Outlook) and look for the message “Passkey registration code for Claude”.",
        "page.enroll_code": "Code from the message",
        "page.enroll_device": "Device name (optional)",
        "page.enroll_device_placeholder": "e.g. Work laptop",
        "page.enroll_create": "Create passkey",
        "page.enroll_done_heading": "Done",
        "page.enroll_done_intro": "The passkey is registered. Now in Claude:",
        "page.enroll_done_1": "Open <strong>Settings → Connectors</strong> and find the {org} connector. If it is not there, ask the IT department.",
        "page.enroll_done_2": "Press <strong>Connect</strong>.",
        "page.enroll_done_3": "Choose <strong>Sign in with passkey</strong> and confirm.",
        "page.cal_title": "Calendar in Outlook",
        "page.cal_heading": "Your calendar in Outlook",
        "page.cal_intro": (
            "Here you get a token for syncing the {org} calendar with Outlook"
            " (the CalDav Synchronizer add-in). The token gives access <strong>to the calendar only</strong>,"
            " not to your mail."
        ),
        "page.cal_tokens_heading": "Calendar tokens",
        "page.cal_new_token": "New token",
        "page.cal_new_intro": "Enter these details in CalDav Synchronizer. The token is shown <strong>only now</strong>.",
        "page.cal_url": "Address (DAV URL)",
        "page.cal_user": "Username",
        "page.cal_password": "Password (the token)",
        "page.cal_your_tokens": "Your tokens",
        "page.cal_label": "Computer name (optional)",
        "page.cal_label_placeholder": "e.g. Office computer",
        # webauthn.js
        "js.locale": "en-GB",
        "js.error_status": "Error {status}",
        "js.not_allowed": "The operation was cancelled or timed out. Try again.",
        "js.invalid_state": "This device already has a passkey for your account.",
        "js.no_webauthn": "This browser does not support passkeys. Use an up-to-date Chrome, Edge, Safari or Firefox.",
        "js.waiting_passkey": "Waiting for the passkey…",
        "js.redirecting": "Redirecting to Claude…",
        "js.denied": "Denied.",
        "js.incomplete_link": "The link is incomplete. Open the full address from the invitation again.",
        "js.sending_code": "Sending the code…",
        "js.checking_code": "Checking the code…",
        "js.confirm_device": "Confirm with Windows Hello, fingerprint or phone…",
        "js.never": "never",
        "js.no_tokens": "You have no tokens.",
        "js.unnamed": "unnamed",
        "js.token_line": "{label} – created {created}, last used {last_used} ",
        "js.revoke": "Revoke",
        "js.revoke_confirm": "Revoke this token? Outlook on that computer will stop syncing.",
        "js.revoked": "The token has been revoked.",
    },
    "bg": {
        "link_expired_title": "Връзката е изтекла",
        "link_expired_text": "Тази заявка за вход вече не е валидна. Върнете се в Claude и опитайте да свържете конектора отново.",
        "error_title": "Грешка",
        "opened_in_other_browser_page": "Заявката вече е отворена в друг браузър.",
        "opened_in_other_browser": "Заявката е отворена в друг браузър.",
        "too_many_wait_minute": "Твърде много опити. Изчакайте минута.",
        "restart_login": "Започнете входа отначало.",
        "bad_browser_response": "Невалиден отговор от браузъра.",
        "passkey_not_registered_login": "Този passkey не е регистриран. Регистрирайте се първо от вътрешната мрежа (страница /enroll).",
        "passkey_not_registered_calendar": "Този passkey не е регистриран. Регистрирайте се първо (страница /enroll).",
        "passkey_check_failed": "Проверката на passkey не успя.",
        "account_inactive": "Акаунтът не е активен в Zimbra.",
        "account_inactive_contact_it": "Акаунтът не е активен в Zimbra. Свържете се с ИТ отдела.",
        "sign_in_first": "Първо влезте с passkey.",
        "bad_request": "Невалидна заявка.",
        "request_expired": "Заявката е изтекла. Върнете се в Claude и опитайте отново.",
        "unavailable_title": "Недостъпно",
        "enroll_internal_only": "Регистрацията на passkey е възможна само от вътрешната мрежа на организацията.",
        "calendar_internal_only": "Страницата е достъпна само от вътрешната мрежа.",
        "internal_only": "Само от вътрешната мрежа.",
        "enter_email": "Въведете имейл адрес.",
        "too_many_wait_15": "Твърде много опити. Опитайте отново след 15 минути.",
        "code_sent_invite": "Кодът е изпратен във входящата поща на {email}.",
        "code_sent": "Ако адресът е активен акаунт, кодът е изпратен във входящата ви поща.",
        "no_enrollment": "Няма активна регистрация за този адрес. Поискайте нов код.",
        "too_many_wrong_codes": "Твърде много грешни опити. Поискайте нов код.",
        "wrong_code": "Грешен код.",
        "enroll_expired": "Регистрацията е изтекла. Започнете отначало.",
        "enroll_failed": "Регистрацията на passkey не успя.",
        "invite_invalid": "Поканата е невалидна, използвана или изтекла. Поискайте нова от ИТ отдела.",
        "reload_page": "Презаредете страницата.",
        "token_limit": "Имате {max} токена. Спрете стар, преди да създадете нов.",
        "no_such_token": "Няма такъв токен.",
        "mail_enroll_subject": "Код за регистрация на passkey за Claude",
        "mail_enroll_text": (
            "Здравейте, {name}!\n\n"
            "Някой (от IP адрес {ip}) започна регистрация на passkey за Claude конектора към пощата ви.\n\n"
            "Код за регистрация: {code}\n\n"
            "Кодът е валиден 15 минути. Въведете го само на страницата за регистрация на {url}\n"
            "Никой от ИТ отдела няма да ви поиска този код.\n\n"
            "Ако не сте започнали регистрация вие, НЕ използвайте кода и уведомете ИТ отдела.\n"
        ),
        "mail_passkey_subject": "Регистриран е нов passkey за Claude",
        "mail_passkey_text": (
            "За вашия акаунт {email} току-що беше регистриран нов passkey{label} за Claude конектора{invite}.\n\n"
            "Ако не сте били вие, уведомете веднага ИТ отдела.\n"
        ),
        "mail_passkey_invite": " чрез покана от ИТ отдела",
        "mail_token_subject": "Създаден е токен за календара",
        "mail_token_text": (
            "За акаунта {email} е създаден токен за синхронизиране на календара ({label}).\n\n"
            "Ако не сте били вие, съобщете веднага на ИТ отдела."
        ),
        "ooo_event_name": "Отпуск",
        "ooo_date_format": "%d.%m.%Y",
        "ooo_on_subject": "Автоматичният отговор при отсъствие е включен",
        "ooo_on_text": (
            "За акаунта {email} чрез Claude е зададен автоматичен отговор при отсъствие.\n\n"
            "Период: {period}\n"
            "След последния ден Zimbra спира да отговаря сама.\n\n"
            "Текст на отговора:\n{reply}\n\n"
            "Податели извън организацията: {external}\n\n"
            "В календара ви е добавено събитие „Отпуск“ за същите дни.\n\n"
            "За да го промените или изключите, кажете на Claude.\n"
            "Ако не сте били вие, съобщете веднага на ИТ отдела.\n"
        ),
        "ooo_external_same": "получават същия отговор.",
        "ooo_external_none": "не получават отговор.",
        "ooo_external_different": "получават този отговор:\n{text}",
        "ooo_off_subject": "Автоматичният отговор при отсъствие е изключен",
        "ooo_off_text": (
            "Автоматичният отговор при отсъствие за акаунта {email} беше изключен чрез Claude.\n\n"
            "Ако не сте били вие, съобщете веднага на ИТ отдела.\n"
        ),
        "unnamed": "без име",
        "send_note_enabled": "Claude ще може да изпраща писма от ваше име, но само след като ви покаже текста и го потвърдите.",
        "send_note_disabled": "Писма няма да се изпращат автоматично – само чернови.",
        "page.welcome": "Claude конектор за пощата на {org}",
        "page.never_password": "Тази страница никога не иска паролата ви за пощата.",
        "page.steps": "Стъпки",
        "page.passkey_login": "Вход с passkey",
        "page.no_password": "<strong>Не въвеждайте парола</strong>.",
        "page.login_title": "Вход",
        "page.login_step_login": "1. Вход с passkey",
        "page.login_step_consent": "2. Разрешение",
        "page.login_heading": "Свързване на Claude с пощата",
        "page.login_intro": "Влезте с passkey (Windows Hello, пръстов отпечатък или телефон).",
        "page.consent_heading": "Потвърждение",
        "page.signed_in_as": 'Влязохте като <strong id="{id}"></strong>.',
        "page.consent_access": '<strong id="client"></strong> иска достъп до вашата поща, календар и контакти в {org}. {send_note}',
        "page.consent_only_you": "Разрешете само ако <strong>вие</strong> току-що свързахте конектора в Claude.",
        "page.allow": "Разреши",
        "page.deny": "Откажи",
        "page.enroll_title": "Регистрация на passkey",
        "page.enroll_step_email": "1. Имейл",
        "page.enroll_step_code": "2. Код и passkey",
        "page.enroll_step_done": "3. Готово",
        "page.enroll_heading": "Регистрация на passkey за Claude",
        "page.enroll_intro": "Passkey-ят заменя паролата при свързване на Claude с пощата ви в {org}. Ще получите еднократен код във входящата си поща.",
        "page.enroll_invite_note": "Използвате лична покана от ИТ отдела. Тя важи само за вашия акаунт и само веднъж.",
        "page.enroll_email": "Служебен имейл",
        "page.enroll_send_code": "Изпрати код",
        "page.enroll_code_heading": "Въведете кода",
        "page.enroll_code_intro": "Отворете пощата си (Outlook) и потърсете писмо „Код за регистрация на passkey за Claude“.",
        "page.enroll_code": "Код от писмото",
        "page.enroll_device": "Име на устройството (по желание)",
        "page.enroll_device_placeholder": "напр. Служебен лаптоп",
        "page.enroll_create": "Създай passkey",
        "page.enroll_done_heading": "Готово",
        "page.enroll_done_intro": "Passkey-ят е регистриран. Сега в Claude:",
        "page.enroll_done_1": "Отворете <strong>Settings → Connectors</strong> и намерете конектора на {org}. Ако го няма, попитайте ИТ отдела.",
        "page.enroll_done_2": "Натиснете <strong>Connect</strong>.",
        "page.enroll_done_3": "Изберете <strong>Вход с passkey</strong> и потвърдете.",
        "page.cal_title": "Календар в Outlook",
        "page.cal_heading": "Календарът в Outlook",
        "page.cal_intro": (
            "Тук получавате токен за синхронизиране на календара от {org} с Outlook"
            " (добавката CalDav Synchronizer). Токенът дава достъп <strong>само до календара</strong>,"
            " не до пощата."
        ),
        "page.cal_tokens_heading": "Токени за календара",
        "page.cal_new_token": "Нов токен",
        "page.cal_new_intro": "Въведете тези данни в CalDav Synchronizer. Токенът се показва <strong>само сега</strong>.",
        "page.cal_url": "Адрес (DAV URL)",
        "page.cal_user": "Потребител",
        "page.cal_password": "Парола (токенът)",
        "page.cal_your_tokens": "Вашите токени",
        "page.cal_label": "Име на компютъра (по избор)",
        "page.cal_label_placeholder": "напр. Офис компютър",
        "js.locale": "bg-BG",
        "js.error_status": "Грешка {status}",
        "js.not_allowed": "Операцията беше отказана или изтече. Опитайте отново.",
        "js.invalid_state": "Това устройство вече има passkey за вашия акаунт.",
        "js.no_webauthn": "Този браузър не поддържа passkey. Използвайте актуален Chrome, Edge, Safari или Firefox.",
        "js.waiting_passkey": "Изчакване на passkey…",
        "js.redirecting": "Пренасочване към Claude…",
        "js.denied": "Отказано.",
        "js.incomplete_link": "Връзката е непълна. Отворете отново целия адрес от поканата.",
        "js.sending_code": "Изпращане на код…",
        "js.checking_code": "Проверка на кода…",
        "js.confirm_device": "Потвърдете с Windows Hello, пръстов отпечатък или телефон…",
        "js.never": "никога",
        "js.no_tokens": "Нямате токени.",
        "js.unnamed": "без име",
        "js.token_line": "{label} – създаден {created}, последно ползван {last_used} ",
        "js.revoke": "Спри",
        "js.revoke_confirm": "Да спра ли този токен? Outlook на този компютър ще спре да синхронизира.",
        "js.revoked": "Токенът е спрян.",
    },
}

LANGUAGES = tuple(MESSAGES)


def check_language(lang: str) -> str:
    """Return the normalised language code, or raise ValueError."""
    code = (lang or "").strip().lower()
    if code not in MESSAGES:
        raise ValueError(f"UI_LANG must be one of: {', '.join(LANGUAGES)} (got {lang!r})")
    return code


def translator(lang: str) -> Callable[..., str]:
    """Plain-text lookup: t("key", name=value) for messages, e-mails and errors."""
    catalogue = MESSAGES[check_language(lang)]

    def t(key: str, **values) -> str:
        text = catalogue[key]
        return text.format(**values) if values else text

    return t


def page_translator(lang: str) -> Callable[..., Markup]:
    """Template lookup: catalogue texts are trusted HTML, inserted values are escaped."""
    catalogue = MESSAGES[check_language(lang)]

    def t(key: str, **values) -> Markup:
        return Markup(catalogue[key]).format(**values)

    return t


def js_strings(lang: str) -> dict[str, str]:
    """The texts webauthn.js needs, keyed without the "js." prefix."""
    catalogue = MESSAGES[check_language(lang)]
    return {key[3:]: text for key, text in catalogue.items() if key.startswith("js.")}
