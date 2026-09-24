# Подключение Google Docs (отчёты по лидам на ваш Google Диск)

Бот-агент по команде «собери документ» создаёт Google-документ со списком найденных лидов.
Документ создаётся **от вашего имени** и лежит **на вашем Google Диске**.

Способ через сервисный аккаунт (`GOOGLE_SERVICE_ACCOUNT_FILE`) для обычного Gmail **не подходит**:
у сервисного аккаунта нет своего места на Диске, и Google отказывает в создании файлов. Он
нужен только для корпоративного Google Workspace с общими дисками. Поэтому используем вход
через OAuth — это 10 минут один раз.

## Шаг 1. Создать проект в Google Cloud

1. Откройте https://console.cloud.google.com/ под тем Google-аккаунтом, на чей Диск должны
   падать документы.
2. Вверху слева — выбор проекта → **New project** (Новый проект) → имя, например `og1-leads` →
   **Create**. Убедитесь, что вверху выбран именно этот проект.

## Шаг 2. Включить два API

1. Меню ☰ → **APIs & Services** → **Library**.
2. Найдите **Google Docs API** → **Enable**.
3. Вернитесь в Library, найдите **Google Drive API** → **Enable**.

## Шаг 3. Настроить экран согласия (OAuth consent screen)

1. Меню ☰ → **APIs & Services** → **OAuth consent screen** (в новой консоли — **Google Auth
   Platform**) → **Get started**.
2. App name: `og1 leads`, User support email: ваш e-mail → Next.
3. Audience: **External** → Next.
4. Contact information: ваш e-mail → Next → согласиться с правилами → **Create**.
5. Раздел **Audience** → в **Test users** нажмите **Add users** и добавьте свой e-mail.
6. **Важно:** там же, в **Audience**, нажмите **Publish app** → Confirm (статус «In production»).
   Если оставить статус «Testing», Google отзывает доступ **каждые 7 дней**, и отчёты
   перестанут создаваться. Проверку (verification) Google проходить не нужно — приложение
   пользуется только вами.

## Шаг 4. Создать OAuth-клиент и скачать JSON

1. **APIs & Services** → **Credentials** (или **Clients** в Google Auth Platform) →
   **Create credentials** → **OAuth client ID**.
2. Application type: **Desktop app**, имя — любое → **Create**.
3. В появившемся окне нажмите **Download JSON**.
4. Переименуйте скачанный файл (`client_secret_....json`) в `google-oauth-client.json` и
   положите в папку проекта: `leads/secrets/google-oauth-client.json`
   (папку `secrets` создать, если её нет).

Этот файл — секрет: никому не отправляйте и не публикуйте его.

## Шаг 5. Войти один раз

В `.env` уже должно быть:

```
GOOGLE_OAUTH_CLIENT_FILE=secrets/google-oauth-client.json
GOOGLE_OAUTH_TOKEN_FILE=secrets/google-token.json
```

Запустите в папке `leads`:

```bash
.venv/bin/python google_login.py
```

Откроется браузер → выберите свой аккаунт → Google покажет «Google hasn't verified this app»
→ **Advanced** (Дополнительно) → **Go to og1 leads (unsafe)** → разрешите доступ. Это
предупреждение нормально: приложение ваше собственное.

В терминале появится «Готово: токен сохранён в secrets/google-token.json». Дальше токен
обновляется сам.

## Шаг 6. Необязательные настройки в `.env`

- `GOOGLE_SERVICE_ACCOUNT_FILE=` — **оставить пустым**.
- `GOOGLE_SHARE_EMAILS=` — e-mail'ы через запятую, кому автоматически открывать каждый
  документ на редактирование (например, менеджерам). Можно оставить пустым.
- `GOOGLE_DRIVE_FOLDER_ID=` — **оставить пустым**: документы будут появляться в корне
  «Моего диска». С выбранным доступом (только файлы, созданные приложением) бот не может
  класть документы в папку, созданную вручную, — Google ответит «File not found».

После изменения `.env` перезапустите `main.py`.

## Проверка

Напишите ТГ-агенту оператора: «собери документ». В ответ придёт ссылка на Google-документ.
