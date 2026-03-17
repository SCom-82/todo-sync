# Skill: Todo — Управление задачами

Ты умеешь управлять задачами через сервис todo-sync, который синхронизирован с Microsoft To Do.

## Подключение

- **URL**: `${TODO_SYNC_URL}/api/v1` (env var `TODO_SYNC_URL`)
- **Авторизация**: заголовок `X-API-Key: ${TODO_SYNC_API_KEY}` (env var `TODO_SYNC_API_KEY`)

Все запросы делай через `curl` с этими параметрами:
```bash
curl -s -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/..."
```

## Доступные команды

### Посмотреть статистику
```bash
curl -s -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/stats"
```
Показывает: всего задач, не начатых, выполненных, просроченных, на сегодня, на неделю, и по каждому списку.

### Просроченные задачи
```bash
curl -s -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/reminders/overdue"
```

### Задачи с ближайшими напоминаниями
```bash
curl -s -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/reminders/upcoming?hours=24"
```

### Все списки задач
```bash
curl -s -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/lists"
```

### Задачи из конкретного списка
```bash
curl -s -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/tasks?list_id=UUID"
```

### Незавершённые задачи
```bash
curl -s -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/tasks?status=notStarted"
```

### Поиск задач по тексту
```bash
curl -s -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/tasks?search=ТЕКСТ"
```

### Задачи с дедлайном на сегодня
```bash
curl -s -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/tasks?due_before=$(date -I)&status=notStarted"
```

### Создать задачу
```bash
curl -s -X POST -H "X-API-Key: $TODO_SYNC_API_KEY" -H "Content-Type: application/json" \
  "$TODO_SYNC_URL/api/v1/tasks" \
  -d '{"list_id":"UUID_СПИСКА", "title":"Название задачи", "importance":"normal", "due_date":"2026-03-20"}'
```
Параметры:
- `list_id` (обязательно) — UUID списка (получи через GET /lists)
- `title` (обязательно) — название
- `importance` — `low`, `normal`, `high`
- `due_date` — дата дедлайна (YYYY-MM-DD)
- `body` — описание/заметки
- `reminder_datetime` — дата+время напоминания (ISO 8601)
- `is_reminder_on` — `true`/`false`
- `categories` — массив тегов `["тег1", "тег2"]`

### Завершить задачу
```bash
curl -s -X POST -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/tasks/UUID/complete"
```

### Вернуть задачу в работу
```bash
curl -s -X POST -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/tasks/UUID/uncomplete"
```

### Обновить задачу
```bash
curl -s -X PATCH -H "X-API-Key: $TODO_SYNC_API_KEY" -H "Content-Type: application/json" \
  "$TODO_SYNC_URL/api/v1/tasks/UUID" \
  -d '{"title":"Новое название", "importance":"high", "due_date":"2026-03-25"}'
```

### Удалить задачу
```bash
curl -s -X DELETE -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/tasks/UUID"
```

### Создать новый список
```bash
curl -s -X POST -H "X-API-Key: $TODO_SYNC_API_KEY" -H "Content-Type: application/json" \
  "$TODO_SYNC_URL/api/v1/lists" \
  -d '{"display_name":"Название списка"}'
```

### Запустить синхронизацию с Microsoft To Do
```bash
curl -s -X POST -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/sync/trigger"
```

### Статус синхронизации
```bash
curl -s -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/sync/status"
```

## Правила поведения

1. **Перед созданием задачи** — всегда сначала получи список (GET /lists), чтобы узнать UUID нужного списка. Спроси пользователя, в какой список добавить, если не очевидно.
2. **При показе задач** — форматируй результат в читаемом виде: название, статус, дедлайн, важность. Не показывай технические поля (UUID, ms_id, sync_status).
3. **Просроченные задачи** — если пользователь спрашивает "что надо сделать" или "какие задачи", начни с просроченных и задач на сегодня.
4. **Важность** — `high` показывай как срочную, `normal` как обычную, `low` как неважную.
5. **Статусы** — `notStarted` = не начата, `inProgress` = в работе, `completed` = выполнена.
6. **Синхронизация** — задачи автоматически синхронизируются с Microsoft To Do каждые 5 минут. Если пользователь говорит, что задача не появилась — запусти POST /sync/trigger.
7. **Ответы на русском** — всегда отвечай на русском языке.
