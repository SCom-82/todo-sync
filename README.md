# Todo Sync Service

Микросервис двусторонней синхронизации задач между **Microsoft To Do** и **PostgreSQL** с REST API.

## Что делает

- Синхронизирует списки задач и задачи из Microsoft To Do в PostgreSQL (и обратно)
- Предоставляет REST API для создания, редактирования, удаления задач
- Показывает статистику: сколько задач не выполнено, просрочено, на сегодня/неделю
- Напоминания: список задач с приближающимся дедлайном
- Автоматическая синхронизация каждые 5 минут (настраивается)

## Стек

- Python 3.12+, FastAPI, uvicorn
- SQLAlchemy 2.0 (async) + asyncpg + Alembic
- Microsoft Graph API (httpx + msal)
- APScheduler
- Docker

## Быстрый старт (Docker Compose)

```bash
# 1. Клонировать
git clone https://github.com/SCom-82/todo-sync.git
cd todo-sync

# 2. Скопировать и заполнить .env
cp .env.example .env
# Отредактировать .env — указать MS_CLIENT_ID (см. раздел "Настройка Microsoft")

# 3. Запустить
docker compose up -d

# 4. Проверить
curl http://localhost:8000/api/v1/healthz
# → {"status":"ok"}

# 5. Авторизоваться в Microsoft (см. ниже)
```

## Быстрый старт (без Docker)

```bash
# 1. Python 3.12+ и PostgreSQL должны быть установлены
python -m venv .venv
source .venv/bin/activate
pip install .

# 2. Создать БД
psql -U postgres -c "CREATE DATABASE todo_sync;"

# 3. Скопировать и заполнить .env
cp .env.example .env

# 4. Применить миграции
alembic upgrade head

# 5. Запустить
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Переменные окружения

| Переменная | Обязательная | По умолчанию | Описание |
|---|---|---|---|
| `DATABASE_URL` | Да | `postgresql+asyncpg://postgres:password@localhost:5432/todo_sync` | Строка подключения к PostgreSQL |
| `MS_CLIENT_ID` | Да | — | Client ID из Microsoft Entra |
| `MS_TENANT_ID` | Нет | `consumers` | `consumers` для личного аккаунта, `organizations` для рабочего, или конкретный tenant ID |
| `SYNC_INTERVAL_SECONDS` | Нет | `300` | Интервал синхронизации в секундах |
| `LOG_LEVEL` | Нет | `INFO` | Уровень логирования |

## Настройка Microsoft (одноразово)

### 1. Зарегистрировать приложение

1. Зайти на [Microsoft Entra](https://entra.microsoft.com) (бесплатно, нужна только учётная запись Microsoft)
2. **Identity → Applications → App registrations → New registration**
3. Имя: `todo-sync` (любое)
4. Supported account types:
   - **Personal Microsoft accounts only** — если личный аккаунт (@outlook.com, @hotmail.com)
   - **Accounts in any org directory and personal** — если нужны оба типа
5. Redirect URI: **оставить пустым** (Device Code Flow не требует)
6. Нажать **Register**

### 2. Настроить приложение

1. Скопировать **Application (client) ID** → это ваш `MS_CLIENT_ID`
2. **Authentication → Advanced settings → Allow public client flows** → **Yes** → Save
3. **API permissions → Add a permission → Microsoft Graph → Delegated permissions**:
   - `Tasks.ReadWrite` — чтение и запись задач
   - `User.Read` — обычно добавлено по умолчанию
4. Нажать **Grant admin consent** (если вы администратор тенанта)

### 3. Авторизоваться через сервис

```bash
# Инициировать Device Code Flow
curl -X POST http://localhost:8000/api/v1/auth/device-code

# Ответ:
# {
#   "user_code": "ABCD1234",
#   "verification_uri": "https://microsoft.com/devicelogin",
#   "expires_in": 900,
#   "message": "Go to https://microsoft.com/devicelogin and enter code ABCD1234"
# }

# Открыть https://microsoft.com/devicelogin в браузере
# Ввести код ABCD1234
# Войти в Microsoft аккаунт

# Проверить статус
curl http://localhost:8000/api/v1/auth/status
# → {"authenticated": true}
```

Авторизация сохраняется в БД. Повторно входить нужно только если refresh token истечёт (90 дней без использования).

## Деплой на Coolify

### 1. Подготовка БД

Создать базу `todo_sync` на существующем PostgreSQL:

```sql
CREATE DATABASE todo_sync;
```

### 2. Создать приложение на Coolify

1. Открыть проект на Coolify
2. **New → Application → Docker Image** (или из GitHub)
3. Указать репозиторий: `https://github.com/SCom-82/todo-sync`
4. Build Pack: **Dockerfile**
5. Port: **8000**

### 3. Переменные окружения (в Coolify)

```
DATABASE_URL=postgresql+asyncpg://postgres:YOUR_PG_PASSWORD@PG_CONTAINER_NAME:5432/todo_sync
MS_CLIENT_ID=ваш-client-id
MS_TENANT_ID=consumers
SYNC_INTERVAL_SECONDS=300
LOG_LEVEL=INFO
```

### 4. Сеть

Убедиться, что приложение подключено к той же Docker-сети, что и PostgreSQL (обычно `coolify`), чтобы контейнеры видели друг друга по имени.

### 5. Health check

```
/api/v1/healthz
```

## API — основные эндпоинты

Полная документация доступна после запуска: `http://localhost:8000/docs` (Swagger UI)

### Задачи

```bash
# Список задач
GET /api/v1/tasks
GET /api/v1/tasks?status=notStarted&overdue=true
GET /api/v1/tasks?list_id=UUID&search=купить

# Создать задачу
POST /api/v1/tasks
{"list_id": "UUID", "title": "Купить молоко", "due_date": "2026-03-20"}

# Завершить задачу
POST /api/v1/tasks/{id}/complete

# Вернуть в работу
POST /api/v1/tasks/{id}/uncomplete

# Обновить
PATCH /api/v1/tasks/{id}
{"title": "Новое название", "importance": "high"}

# Удалить
DELETE /api/v1/tasks/{id}
```

### Списки задач

```bash
GET    /api/v1/lists                    # все списки
POST   /api/v1/lists                    # создать {"display_name": "Работа"}
PATCH  /api/v1/lists/{id}               # переименовать
DELETE /api/v1/lists/{id}               # удалить
```

### Статистика и напоминания

```bash
GET /api/v1/stats                       # общая статистика
GET /api/v1/reminders/upcoming?hours=24 # задачи с напоминанием в ближайшие N часов
GET /api/v1/reminders/overdue           # просроченные задачи
```

### Синхронизация

```bash
POST /api/v1/sync/trigger               # запустить синхронизацию вручную
GET  /api/v1/sync/status                # статус последней синхронизации
GET  /api/v1/sync/log                   # журнал синхронизаций
```

### Авторизация

```bash
POST /api/v1/auth/device-code           # начать Device Code Flow
GET  /api/v1/auth/status                # проверить авторизацию
```

## Структура проекта

```
todo-sync/
├── app/
│   ├── main.py               # FastAPI приложение
│   ├── config.py             # настройки из env vars
│   ├── database.py           # подключение к PostgreSQL
│   ├── models.py             # ORM модели (5 таблиц)
│   ├── schemas.py            # Pydantic схемы запросов/ответов
│   ├── scheduler.py          # периодическая синхронизация
│   ├── api/
│   │   ├── auth.py           # авторизация Microsoft
│   │   ├── task_lists.py     # CRUD списков
│   │   ├── tasks.py          # CRUD задач
│   │   ├── stats.py          # статистика и напоминания
│   │   └── sync.py           # ручная синхронизация
│   └── services/
│       ├── auth_service.py   # MSAL Device Code Flow
│       ├── graph_client.py   # Microsoft Graph API клиент
│       ├── sync_service.py   # движок синхронизации
│       └── task_service.py   # бизнес-логика задач
├── alembic/                  # миграции БД
├── Dockerfile
├── docker-compose.yml        # для локальной разработки
├── .env.example              # шаблон переменных окружения
└── pyproject.toml
```

## Таблицы БД

| Таблица | Описание |
|---------|----------|
| `task_lists` | Списки задач (синхронизированы с MS To Do) |
| `tasks` | Задачи с полями: title, body, importance, status, due_date, reminder, categories |
| `sync_state` | Delta-токены для инкрементальной синхронизации |
| `auth_tokens` | Кэш MSAL токенов (access + refresh) |
| `sync_log` | Журнал синхронизаций |

## Лицензия

Private repository.
