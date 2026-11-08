# Инсталация на Zimbra MCP сървъра

[English version](INSTALL.md)

Примерите са за домейн `example.com`, Zimbra сървър `mail.example.com` и адрес на конектора `mcp.example.com`. Заменете ги с вашите.

## 0. Какво ви трябва

- Отделна VM: **2 vCPU, 2 GB RAM, 20–40 GB диск**, **Debian 12** или **Ubuntu 24.04**. Сървърът работи и с 1 vCPU и 1 GB, но тогава `docker compose up --build` може да спре поради липса на памет.
- Ако ползвате **Hyper-V**:
  - **Generation 2**, Secure Boot с шаблон **„Microsoft UEFI Certificate Authority“**, иначе Linux не стартира от ISO-то;
  - RAM **статично** (без Dynamic Memory);
  - диск **Dynamically expanding** VHDX, който заема само реално използваното място;
  - в Integration Services оставете **Time synchronization** включено, защото passkey, токените и сертификатът зависят от точния час.
- Публичен DNS запис `mcp.example.com` → публичното IP, с пренасочени портове **80 и 443** към VM-а. Claude се свързва от сървърите на Anthropic, затова адресът трябва да е достъпен от интернет.
- **Вътрешен (split) DNS:** в офиса `mcp.example.com` трябва да сочи към **вътрешното** IP на VM-а. Така служителите стигат директно до него и сървърът вижда вътрешния им адрес, което е нужно за страницата за регистрация.
- Достъп от VM-а до Zimbra: **443** (SOAP) и **389** (LDAP).

## 1. Подготовка в Zimbra

Командите се изпълняват на Zimbra сървъра като потребител `zimbra` (`su - zimbra`).

### 1.1. Preauth ключ

Първо **проверете дали домейнът вече има ключ**:

```bash
zmprov gd example.com zimbraPreAuthKey
```

- Ако **има** стойност, използвайте нея. **Не генерирайте нов ключ**, защото новият заменя стария и ще спре всяка друга интеграция, която го ползва.
- Ако **няма**, генерирайте:

```bash
zmprov generateDomainPreAuthKey example.com
```

Запишете ключа (дълъг шестнадесетичен низ). Той е **главният ключ** за всички пощенски кутии в домейна. Пазете го като парола на root.

### 1.2. LDAP акаунт само за четене

```bash
zmlocalconfig -s ldap_postfix_password
zmlocalconfig ldap_url
```

Първата команда дава паролата на `uid=zmpostfix,cn=appaccts,cn=zimbra`, а втората LDAP адреса.

### 1.3. Защитна стена на Zimbra

Разрешете порт **389** само от IP-то на MCP машината. Порт 443 вероятно вече е отворен. IP-то на MCP машината се вижда с `hostname -I` на нея. Пример с `ufw` (като root на Zimbra):

```bash
ufw allow from 192.168.10.20 to any port 389 proto tcp comment 'LDAP from MCP'
ufw status verbose
```

Ако Zimbra ползва `firewalld` или `iptables`, добавете еквивалентно правило там.

## 2. Подготовка на VM-а

При инсталацията на Ubuntu Server:

- **Hostname** `mcp`, домейн `example.com`. Името на машината е само за удобство, важни са DNS записите и `.env`.
- При ръчна мрежова настройка полето **Subnet** е в CIDR формат, например `192.168.10.0/24`, а не `255.255.255.0`. **Name servers** трябва да е вътрешният DNS, за да работи split DNS.
- Ако инсталаторът предложи да се обнови („Update to the new installer“), може да откажете. Обновленията се слагат после с `apt upgrade`.

Всички команди оттук нататък са като **root**. В Ubuntu влезте с потребителя си и пуснете `sudo -i`, за да не пишете `sudo` пред всяка команда.

```bash
apt update && apt install -y git ldap-utils ca-certificates curl
```

Инсталирайте Docker по официалното ръководство за вашата дистрибуция:

- **Ubuntu:** https://docs.docker.com/engine/install/ubuntu/
- **Debian:** https://docs.docker.com/engine/install/debian/

Използвайте раздела **„Install using the apt repository“**. Пакетът `docker.io` от хранилището на дистрибуцията е по-стар и често е без `docker compose`. Когато тестът с `hello-world` мине, Docker е готов. Останалото в тяхната страница не ви трябва.

### 2.1. Проверка на LDAP от VM-а

Първо проверете, че VM-ът стига до **вътрешния** адрес на Zimbra:

```bash
getent hosts mail.example.com
```

Трябва да излезе вътрешното IP на Zimbra. Ако излиза публичното, добавете ред в `/etc/hosts`, например `192.168.10.10  mail.example.com`. Не пишете IP в `LDAP_URL` и `ZIMBRA_URL`, защото TLS сертификатът е издаден за името, не за IP-то.

После проверете, че порт 389 е отворен:

```bash
nc -zv -w 5 mail.example.com 389
```

- `succeeded` означава, че портът е достъпен.
- `timed out` означава, че защитната стена на Zimbra не пуска VM-а (т. 1.3). В този случай `ldapsearch` по-долу просто виси, без да пита за парола.

Накрая пробвайте търсене с реален адрес:

```bash
ldapsearch -x -ZZ -H ldap://mail.example.com:389 \
  -D "uid=zmpostfix,cn=appaccts,cn=zimbra" -W \
  -b "ou=people,dc=example,dc=com" \
  "(zimbraMailDeliveryAddress=ivan@example.com)" \
  zimbraMailDeliveryAddress displayName zimbraAccountStatus zimbraMailStatus
```

Въведете паролата от т. 1.2, когато се покаже `Enter LDAP Password:`. Трябва да видите акаунта и `result: 0 Success`.

- `displayName::` с две двоеточия е нормално. Когато името е на кирилица (или съдържа други не-ASCII символи), то е кодирано в base64.
- Ако `zimbraAccountStatus` не се показва, това също е нормално: `zmpostfix` няма право да го чете. Тогава спрените акаунти се отсяват при входа в Zimbra.

## 3. Инсталация

### 3.1. Изтегляне на кода

Кодът се слага в **`/opt/zimbra-mcp`**. Командата `git clone` се пуска от `/opt` и сама създава папката `zimbra-mcp`:

```bash
cd /opt
git clone https://github.com/Dobrin77/zimbra-mcp.git
cd /opt/zimbra-mcp
```

Ако работите със собствен private fork, добавете на VM-а ключ само за четене (deploy key) в GitHub (**Settings → Deploy keys**, без „Allow write access“) и клонирайте през SSH.

**Всички команди оттук нататък се пускат от `/opt/zimbra-mcp`.**

### 3.2. Тайни

Папките `secrets/` и `data/` трябва да са в `/opt/zimbra-mcp`, до `docker-compose.yml`:

```bash
cd /opt/zimbra-mcp
mkdir -p secrets data
nano secrets/preauth_keys
nano secrets/ldap_password
```

- В `secrets/preauth_keys` напишете един ред: домейн, интервал, ключ от т. 1.1. Например `example.com 0123abcd...`. При няколко домейна пишете по един ред за всеки.
- В `secrets/ldap_password` напишете само паролата от т. 1.2.

Пишете ги с `nano`, а не с `echo`, иначе ключът остава в историята на командите (`~/.bash_history`). Не ги пращайте и в чат.

След това ограничете правата:

```bash
chown -R 10001 secrets data
chmod 700 secrets data
chmod 400 secrets/*
```

Накрая трябва да имате:

```
/opt/zimbra-mcp/secrets/preauth_keys
/opt/zimbra-mcp/secrets/ldap_password
/opt/zimbra-mcp/data/
```

### 3.3. Настройки

```bash
cp .env.example .env
nano .env
```

Попълнете поне `PUBLIC_URL`, `MCP_HOSTNAME`, `ACME_EMAIL`, `ORG_NAME`, `ZIMBRA_URL`, `LDAP_URL` и **двата реда `ENROLL_ALLOWED_NETWORKS`** с офисната ви мрежа (възможно най-тясна, например `192.168.10.0/24`). Ако служителите са в няколко мрежи, изброете всички: в `ENROLL_ALLOWED_NETWORKS` със запетая, а в `ENROLL_ALLOWED_NETWORKS_CADDY` с интервал.

`TRUSTED_PROXIES` изброява обратните проксита, на чийто хедър `X-Real-IP` приложението вярва. От всеки друг адрес хедърът се пренебрегва и се взема адресът на самата връзка. Стойността по подразбиране `127.0.0.1/32,::1/128,172.16.0.0/12` пасва на тази инсталация, при която Caddy стига до контейнера през мрежата на Docker. Променяйте я само ако проксито е на друга машина.

`UI_LANG` задава езика на уеб страниците (вход, регистрация, покани, календарни токени) и на писмата с кода: `en` (по подразбиране) за английски, `bg` за български.

В примера `ZIMBRA_ENABLE_SEND` е `false`: Claude само пише чернови, видими в уеб пощата на Zimbra. `true` позволява на Claude да изпраща писма след потвърждение в чата. Това е нужно, ако служителите ползват Outlook с POP3, защото той не показва черновите от сървъра. Оставете `ZIMBRA_ALLOW_HARD_DELETE=false`.

Писмата, изпратени от Claude, **никога не се записват в Sent**. Вместо това подателят автоматично получава копие (BCC) във входящата си поща, което Outlook изтегля през POP3. Освен това сървърът записва в лога си подател, получатели и тема, без текста на писмото. Вижте го с `docker compose logs zimbra-mcp | grep "Sent mail"`.

### 3.4. Външен вид (по желание)

Страниците за вход и регистрация могат да показват вашето лого, цветове и текстове. Всичко се слага в `data/themes/`, извън кода, така че `git pull` никога не го презаписва. Всеки файл е по желание. Без папката страниците изглеждат неутрално.

```bash
cd /opt/zimbra-mcp
mkdir -p data/themes
cp deploy/themes.example/* data/themes/
nano data/themes/theme.json
```

| Файл | Какво е |
|---|---|
| `logo.svg` (или `.png`, `.jpg`, `.webp`) | логото в горната част на картата. Показва се на бял фон, най-добре е SVG. |
| `favicon.png` (или `.ico`, `.svg`) | иконката в раздела на браузъра |
| `theme.json` | `org_name` (името в текстовете, в passkey-а и като подател на писмата с кода; ако липсва, се ползва `ORG_NAME`), `welcome` (поздравът над картата), `support` (ред за помощ под картата), `copyright` (най-долният ред, например `© {year} Пример ООД. Всички права запазени.`; `{year}` става текущата година) |
| `theme.css` | цветовете. Примерът задава основен цвят за светлия режим и по-светъл вариант за тъмния. |

Всеки файл трябва да е до 1 MB. След промяна в темата:

```bash
chown -R 10001 data/themes
docker compose restart zimbra-mcp
```

### 3.5. Стартиране

```bash
docker compose up -d --build
docker compose logs -f zimbra-mcp
```

Caddy сам взима Let's Encrypt сертификат при първото стартиране.

## 4. Проверки

1. `curl https://mcp.example.com/health` трябва да върне `{"status":"ok"}`.
2. `curl https://mcp.example.com/.well-known/oauth-authorization-server` трябва да върне JSON.
3. **От телефон на мобилни данни, не през Wi-Fi в офиса**, отворете `https://mcp.example.com/enroll`. Трябва да видите съобщение, че **регистрацията е възможна само от вътрешната мрежа**. Ако страницата се отвори, рутерът подменя адресите (SNAT) и ограничението не работи. **Не продължавайте**, преди да го оправите.
4. От офиса отворете `https://mcp.example.com/enroll` и регистрирайте passkey за себе си. Кодът трябва да пристигне във входящата поща.
5. Когато всичко работи, направете **snapshot (checkpoint)** на VM-а. Ако някое обновяване се обърка, се връщате към него.

## 5. Свързване в Claude

> **Адресът на конектора завършва на `/mcp`:** `https://mcp.example.com/mcp`. Само `https://mcp.example.com` не работи, защото на `/` няма нищо и сървърът връща 404.

### За администратора на Claude Team или Enterprise

Конекторът се добавя веднъж за цялата организация, от акаунт с админ права:

1. **Admin settings → Connectors → Add custom connector**. Имената на менютата понякога се сменят.
2. **Name:** име, което служителите ще разпознаят, например „Поща ПРИМЕР“. Страницата за регистрация им казва да търсят конектора на организацията, затова не е добре да е само „Zimbra“.
3. **URL:** `https://mcp.example.com/mcp`.
4. Оставете това, което Claude открие сам (**Detected**):
   - **Authentication:** **Sign in now**.
   - **OAuth client:** **Register automatically** (DCR). Не попълвайте Client ID или Secret.
   - **Request headers:** не добавяйте нищо.
5. Натиснете **Add**.

Така конекторът става **достъпен** за всички, но никой не е свързан автоматично. Всеки се свързва сам със своя passkey, така че администраторът не вижда ничия поща. Ако самият администратор иска да ползва конектора, той също трябва да мине през регистрацията и **Connect** от собствения си акаунт.

### За служителите

1. **Еднократно, от офиса:** отворете `https://mcp.example.com/enroll`, въведете служебния си имейл, въведете кода от писмото и потвърдете с Windows Hello, пръстов отпечатък или телефон.
2. **В Claude:** Settings → Connectors → намерете конектора на организацията → **Connect** → **Вход с passkey** → **Разреши**. При личен акаунт (без Team) първо **Add custom connector** с URL `https://mcp.example.com/mcp`.
3. В настройките на конектора оставете `send_email` на **„Needs approval“**.

Passkey-ят трябва да е на устройството, от което се натиска **Connect**. След това връзката важи за целия Claude акаунт: уеб, Desktop и мобилно приложение.

### Устройства и passkey

- **Windows:** нужен е Windows Hello (ПИН, пръстов отпечатък или лице), Windows 10 версия 1903 или по-нова. ПИН-ът е в *Settings → Accounts → Sign-in options → Windows Hello PIN*. В сесия през **отдалечен работен плот (RDP)** Windows Hello не е достъпен. Регистрирайте се от компютъра, пред който седите, или от телефона.
- **Mac:** нужен е **macOS 13 Ventura** или по-нов. Safari записва passkey-я в **iCloud Keychain** и го потвърждава с Touch ID или с паролата за вход в Mac-а.
  - **iCloud Keychain трябва да е включен:** *System Settings → [вашето име] → iCloud → Passwords & Keychain*.
  - Passkey-ят се синхронизира с всички устройства със **същия Apple ID** (iPhone, iPad). Ако на служебния Mac сте влезли с личен Apple ID, passkey-ят ще е и на личните ви устройства.
  - **Регистрацията и Connect да са в един и същи браузър.** Chrome на Mac може да предложи да запише passkey-я в Google Password Manager. Тогава Safari няма да го вижда, а Claude Desktop често отваря Connect точно в Safari. В Chrome изберете **iCloud Keychain**, който се вижда и от двата браузъра.
- **Телефон (iPhone / Android):** регистрацията може да стане и директно от телефона, вързан на офисния Wi-Fi, или с покана. Passkey-ят се пази в iCloud Keychain или Google Password Manager.
- **Устройството няма passkey:** при вход изберете „Use a phone or tablet“ и сканирайте QR кода с телефона, на който е passkey-ят. Компютърът трябва да има Bluetooth.
- **Резерва:** всеки може да регистрира повече от един passkey, например на компютъра и на телефона. Ако загуби едното устройство, не остава без достъп.

### Служители извън офиса (покани)

Служителите, които не идват в офиса и нямат VPN, се регистрират с **покана**: лична еднократна връзка, която работи от всяка мрежа. Освен поканата им трябва и кодът от пощата. Така не стига нито само открадната парола за пощата, нито само прихваната връзка.

1. Създайте поканата на MCP машината (от `/opt/zimbra-mcp`):

   ```bash
   docker compose exec zimbra-mcp zimbra-mcp-admin invite ivan@example.com
   ```

   За няколко души наведнъж изредете адресите с интервал:

   ```bash
   docker compose exec zimbra-mcp zimbra-mcp-admin invite ivan@example.com maria@example.com
   ```

   Или ги запишете във файл, по един адрес на ред (празните редове и всичко след `#` се пропускат):

   ```
   # външни служители
   ivan@example.com
   maria@example.com    # търговски отдел
   ```

   и го подайте така (обърнете внимание на `-T` и `-`):

   ```bash
   docker compose exec -T zimbra-mcp zimbra-mcp-admin invite --file - < /root/invites.txt
   ```

2. Командата проверява всеки адрес в LDAP и показва по една връзка за всеки активен акаунт:

   ```
   2 invite(s), valid until 2026-11-04 21:30.
   Send each link to its owner by phone, Viber or SMS, NOT by e-mail. Each works once.

     ivan@example.com (Иван Петров)
       https://mcp.example.com/invite#k3J9...

     maria@example.com (Мария Иванова)
       https://mcp.example.com/invite#Qx7a...
   ```

   Ред `SKIPPED` означава, че за този адрес **не е създадена** покана: адресът е сгрешен, акаунтът е спрян, или LDAP не отговаря.

3. **Изпратете всяка връзка на собственика ѝ по телефон, Viber или SMS, никога по имейл.** Ако връзката мине през пощата, крадец, влязъл в пощата, ще има и връзката, и кода.
4. Служителят отваря връзката, натиска **Изпрати код**, въвежда кода от пощата си и потвърждава с passkey. Поканата важи само за неговия акаунт и само веднъж. В пощата му идва известие, че passkey-ят е регистриран чрез покана от администратора.

Поканите важат **72 часа**. За друг срок добавете `--hours 24` (от 1 до 336). Нова покана за същия човек анулира старата.

```bash
# Кои покани още не са използвани
docker compose exec zimbra-mcp zimbra-mcp-admin invites

# Анулиране на покана (например изпратена на грешен човек)
docker compose exec zimbra-mcp zimbra-mcp-admin cancel-invite ivan@example.com
```

### Какво трябва да знаят служителите

- Страницата за вход **никога** не иска парола. Ако някой поиска парола от тяхно име, това е фишинг.
- Преди да изпрати писмо, Claude показва получателите, темата и текста и чака потвърждение. **Четете внимателно преди да потвърдите**, особено получателите.
- Ако писмо, което сте получили, „кара“ Claude да препраща нещо на непознат адрес, откажете. Това е вид атака.
- За всяко писмо, изпратено от Claude, получават **копие във входящата поща** (те са в BCC, получателите не го виждат). В Sent на сървъра копие не остава. Копието може да се премести ръчно в **Sent Items** или автоматично с правило в Outlook: *Home → Rules → Manage Rules & Alerts → New Rule → „from people or public group“ = собственият адрес и „sent to me“ → „move it to the specified folder“ = Sent Items*.
- Изтритите от Claude писма отиват в **кошчето**.
- В настройките на конектора в Claude оставете за `send_email` режим **„Needs approval“**, а не „Always allow“.

## 6. Администриране

```bash
# Кой има passkey и активни сесии
docker compose exec zimbra-mcp zimbra-mcp-admin list

# Служител напуска или губи лаптоп: маха всички passkey-ове, сесии и покани
docker compose exec zimbra-mcp zimbra-mcp-admin revoke ivan@example.com

# Само прекратява сесиите в Claude (passkey-овете остават)
docker compose exec zimbra-mcp zimbra-mcp-admin logout ivan@example.com
```

**Спиране на акаунт в Zimbra** спира достъпа и през Claude до 5 минути, без нищо да се пипа тук.

### Изпращане от общ адрес на отдел

Служителите могат да изпращат през Claude и от общ адрес, например `sales@example.com`, ако имат правото **Send As** в Zimbra. Правата се дават само в Zimbra и конекторът ги чете оттам. Zimbra ги проверява отново при всяко изпращане.

На Zimbra сървъра, като потребител `zimbra`:

```bash
# Право за няколко души наведнъж
for u in ivan maria petar; do zmprov grantRight account sales@example.com usr $u@example.com sendAs; done

# Или за група, за да не пипате правата при смяна на хора в отдела
zmprov cdl sales-team@example.com zimbraHideInGal TRUE
zmprov adlm sales-team@example.com ivan@example.com maria@example.com
zmprov grantRight account sales@example.com grp sales-team@example.com sendAs

# Кой има право
zmprov getGrants -t account sales@example.com

# Отнемане
zmprov revokeRight account sales@example.com usr ivan@example.com sendAs
```

- **`sendAs`:** получателят вижда само общия адрес.
- **`sendOnBehalfOf`:** получателят вижда „Иван от името на sales@“.

Служителят казва на Claude например *„отговори от sales@“*. Claude показва писмото с адреса на подателя и изпраща след потвърждение. Копие (BCC) получават **и служителят, и общата кутия**, за да видят писмото и останалите от отдела.

С това право служителите не трябва да знаят паролата на общия адрес. Ако и Outlook на всички се прехвърли да изпраща през своите акаунти, паролата на `sales@` може да се смени и никой да не я знае.

### Архивиране

Архивирайте `data/` (базата с passkey-ове) и `secrets/`. Ако `data/` се загуби, служителите просто регистрират passkey наново.

### Обновяване

```bash
git pull
docker compose up -d --build
```

Преди обновяване направете snapshot на VM-а. След обновяване изтрийте старите образи и кеша от build-а, за да не пълнят диска:

```bash
docker system prune -f
```

Логовете на контейнерите се ограничават сами, до 50 MB на услуга.

Проверка, че контейнерът работи с новия код:

```bash
docker compose exec zimbra-mcp python -c "import zimbra_mcp; print(zimbra_mcp.__file__)"
git log --oneline -1
```

**Нови инструменти или полета:** claude.ai пази списъка с инструментите от момента на свързването. След обновяване, което добавя инструмент или поле, служителите трябва да отворят **нов чат**. Ако Claude още вижда старите инструменти, трябва **Settings → Connectors → Disconnect → Connect**.

### Подмяна на preauth ключа

Ако подозирате, че ключът е изтекъл:
1. `zmprov generateDomainPreAuthKey example.com` на Zimbra.
2. Сменете го в `secrets/preauth_keys`.
3. `docker compose restart zimbra-mcp`.

## 7. Локалната поща от Outlook

Конекторът вижда само пощата, която Zimbra още пази на сървъра. За по-стара поща, която е само в класическия Outlook на компютъра (PST файлове), има отделен проект: **outlook-local-mcp** (https://github.com/Dobrin77/outlook-local-mcp). Той работи локално с Claude Desktop, без пароли и без връзка с този сървър. Инсталацията и настройката са описани в неговото хранилище.

## 8. Календарът в Outlook (CalDAV шлюз)

Служителите синхронизират календара си от Zimbra с класическия Outlook, **без да въвеждат паролата си**. Вместо паролата получават отделен **токен само за календара**. Токенът не дава достъп до пощата, контактите или Briefcase.

Шлюзът е част от MCP сървъра и работи на `https://mcp.example.com/dav/`. Outlook говори с него, а той с Zimbra от името на служителя, както конекторът.

### За служителите

1. **Добавката.** Инсталирайте безплатната добавка **CalDav Synchronizer** за Outlook (https://caldavsynchronizer.org). Администраторът може да я разпространи централно.
2. **Токен.** От офиса (вътрешната мрежа) отворете **https://mcp.example.com/calendar**. Натиснете „Вход с passkey“, после „Нов токен“, и дайте име, например „Офис компютър“. Страницата показва адрес, потребител и токен. Токенът се вижда **само веднъж**. В пощата ви идва известие, че е създаден токен.
3. **Настройка в Outlook.**
   - Отворете Outlook → лента **CalDav Synchronizer** → **Synchronization Profiles** → **+** → **Generic CalDAV/CardDAV**.
   - **DAV URL:** `https://mcp.example.com/dav/<вашият имейл>/Calendar/` (адресът от страницата).
   - **Username:** вашият имейл.
   - **Password:** токенът.
   - Натиснете **Test or discover settings**, изберете календара („Calendar“) и натиснете **OK**, не Cancel.
   - **Outlook folder:** изберете или създайте календарна папка в Outlook.
   - **Synchronization mode:** двупосочно („Outlook ↔ Server (Two-Way)“).

Токенът е за едно устройство. За всеки компютър си направете отделен, най-много 5.

### Спиране на токен

- **От служителя:** на https://mcp.example.com/calendar, бутон „Спри“ до токена.
- **От администратора:**

```bash
# Всички календарни токени
docker compose exec zimbra-mcp zimbra-mcp-admin dav-tokens

# Спира един токен по номер или всички на акаунта
docker compose exec zimbra-mcp zimbra-mcp-admin dav-revoke 12
docker compose exec zimbra-mcp zimbra-mcp-admin dav-revoke ivan@example.com
```

`revoke <email>` от раздел 6 спира и календарните токени.

### Ако нещо не работи

- **„Unauthorized“ или 401.** Проверете две неща. Потребителят трябва да е пълният имейл. Токенът трябва да е копиран без интервали и още да е в списъка на страницата.
- **„400 Bad Request“ / „not a calendar resource“.** DAV URL сочи началната папка. Трябва да завършва на `/Calendar/`.
- **Не се виждат контактите.** Така е нарочно: шлюзът пропуска само календари.
- **Синхронизацията спря след смяна на компютъра.** Направете нов токен за новия компютър и спрете стария.
