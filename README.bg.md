# Zimbra MCP сървър за Claude

[English](README.md)

Отдалечен многопотребителски MCP сървър, с който служителите свързват **Claude** (claude.ai, Desktop, мобилно) със своята поща, календар и контакти в **Zimbra** като персонализиран конектор.

Базиран на [jeremie-lesage/zimbra-mcp](https://github.com/jeremie-lesage/zimbra-mcp) (MIT). Добавени са многопотребителски режим, вход с passkey, Zimbra preauth, LDAP проверка и CalDAV шлюз само за календара в Outlook.

## Как работи

```
Claude ──HTTPS + OAuth──▶ Caddy (mcp.example.com:443) ──▶ zimbra-mcp ──SOAP (preauth)──▶ Zimbra
                                                              └────LDAP (само четене)─────▶ Zimbra LDAP
```

- **Без пароли.** Потребителите никъде не въвеждат паролата си за пощата. Пред Claude се представят с **passkey** (Windows Hello, пръстов отпечатък, телефон). Той е вързан за адреса на сървъра и не работи на фишинг сайт.
- **Preauth.** Сървърът влиза в Zimbra от името на потребителя с домейн preauth ключ, който стои само на сървъра.
- **LDAP.** Достъп имат всички активни акаунти в Zimbra. Спрян или изтрит акаунт губи достъп до 5 минути.
- **Изпращане само с потвърждение.** Claude показва писмото (получатели, тема, текст) в чата и изпраща чак след потвърждение. В Sent не остава копие, което пести място. Вместо това подателят получава копие (BCC) във входящата си поща (полезно, когато Outlook ползва POP3). Получателите и темата се записват в лога на сървъра. С `ZIMBRA_ENABLE_SEND=false` Claude пише само чернови. Окончателното изтриване е изключено, писмата отиват в кошчето.
- **Регистрацията на passkey** е възможна само от вътрешната мрежа, с еднократен код, доставен директно във входящата поща. Служителите извън офиса получават от администратора лична еднократна покана по телефон или SMS. Тя замества вътрешната мрежа, но не и кода от пощата.
- **Бройки за ползването, без съдържание.** За да се види дали конекторът реално се ползва, сървърът брои извикванията на инструментите по човек, ден и група (поща, изпращане, календар, отпуска, контакти, тагове). Не пази въпроси, писма, теми или получатели и изтрива бройките след 13 седмици. `zimbra-mcp-admin usage` дава седмична справка. Уведомете служителите, както изискват правилата ви за лични данни.
- **Език на уеб страниците.** Страниците за вход, регистрация, покани и календар са на английски (по подразбиране) и на български, според настройката `UI_LANG`.

## Инструменти

| Поща | Календар | Контакти | Тагове и списъци |
|---|---|---|---|
| search_emails, get_email, get_attachment, list_folders, search_folder, move_emails, mark_as_read, delete_emails (в кошчето), create_draft, send_email, list_send_identities (изпращане от общ адрес с правото Send As в Zimbra) | get_calendar_events, get_event_details, create_event, get_free_busy, get_out_of_office, set_out_of_office, disable_out_of_office (автоматичен отговор при отсъствие със събитие „Отпуск“), who_is_out (кой е в отпуск) | search_contacts, get_contact, create_contact, update_contact, delete_contact | list_tags, create_tag, delete_tag, add_tag_to_emails, remove_tag_from_emails; list_my_distribution_lists, get_distribution_list_members, compare_distribution_list, update_distribution_list (за собствениците на списъци) |

## Календарът в Outlook (CalDAV шлюз)

Сървърът включва и CalDAV шлюз само за календара на `/dav/`. Така служителите синхронизират календара си от Zimbra с класическия Outlook чрез безплатната добавка [CalDav Synchronizer](https://caldavsynchronizer.org), **без да въвеждат паролата си**. Всеки създава токен само за календара, отделен за всяко устройство, на страницата `/calendar` (след вход с passkey). Токенът не дава достъп до пощата, контактите или Briefcase и може да бъде спрян от служителя или от администратора (`zimbra-mcp-admin dav-tokens`, `dav-revoke`). Вижте раздел 8 в ръководството.

## Локалната поща от Outlook

Сървърът вижда само пощата, която Zimbra още пази. За по-стара поща, която е само в класическия Outlook на компютъра (PST файлове), вижте отделния проект [outlook-local-mcp](https://github.com/Dobrin77/outlook-local-mcp), който работи локално с Claude Desktop.

## Инсталация

Вижте **[deploy/INSTALL.bg.md](deploy/INSTALL.bg.md)** (на английски: [deploy/INSTALL.md](deploy/INSTALL.md)). Сървърът се пуска с Docker Compose и Caddy (автоматични Let's Encrypt сертификати). Настройките са в `.env` (вижте `.env.example`), а тайните в `./secrets/`.

За служителите има отделно **[ръководство](docs/USER-GUIDE.bg.md)** (на английски: [docs/USER-GUIDE.md](docs/USER-GUIDE.md)): регистрация, свързване в Claude и календарът в Outlook. Заменете `mcp.example.com` с вашия адрес и го изпратете на всички.

## Разработка

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[test]"
.venv/Scripts/pytest
```

На Linux/macOS използвайте `.venv/bin/` вместо `.venv/Scripts/`.

## Състояние и поддръжка

Проектът е направен за нуждите на една организация и е споделен такъв, какъвто е, с надеждата да помогне на други, които ползват Zimbra. Няма гаранции за поддръжка. Issues и pull requests са добре дошли, но отговорите са според възможностите.

## Сигурност

Моля, не съобщавайте проблеми със сигурността в публични issues. Използвайте частното докладване на уязвимости в GitHub: раздел **Security** на хранилището → **Report a vulnerability**.

## Благодарности

Написано от Добрин Тасев заедно с [Claude Code](https://claude.com/claude-code) (Anthropic), който свърши по-голямата част от програмирането, тестовете и прегледа за сигурност.

## Лиценз

MIT, вижте [LICENSE](LICENSE).
