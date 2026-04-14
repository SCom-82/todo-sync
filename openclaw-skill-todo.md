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

### Задачи из списка по имени (F1.1)
```bash
curl -s -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/tasks?list_name=dev-coder"
```
Удобнее чем `list_id` — UUID нестабильны между миграциями БД. Используй `list_name` в автоматизации.

### Разрешить id списка по имени (F1.1)
```bash
curl -s -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/lists/resolve?name=dev-coder"
```
Возвращает `{id, ms_id, display_name}`. 404 если не найден, 409 если имя неоднозначное (несколько списков).

### Незавершённые задачи
```bash
curl -s -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/tasks?status=notStarted"
```

### Поиск задач по тексту
```bash
curl -s -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/tasks?search=ТЕКСТ"
```

### Задачи на сегодня
```bash
curl -s -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/tasks?filter=today"
```
Возвращает незавершённые задачи с дедлайном на сегодня.

### Просроченные задачи (через filter)
```bash
curl -s -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/tasks?filter=overdue"
```
Возвращает незавершённые задачи с просроченным дедлайном.

### Задачи на неделю
```bash
curl -s -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/tasks?filter=week"
```
Возвращает незавершённые задачи на ближайшие 7 дней.

### Создать задачу
```bash
curl -s -X POST -H "X-API-Key: $TODO_SYNC_API_KEY" -H "Content-Type: application/json" \
  "$TODO_SYNC_URL/api/v1/tasks" \
  -d '{"list_name":"dev-coder", "title":"Название задачи", "importance":"normal", "due_datetime":"2026-03-20T15:00:00+04:00"}'
```
Параметры (указать ровно **один** из `list_name` / `list_id` / `list_ms_id`):
- `list_name` — имя списка (F1.1, рекомендовано)
- `list_id` — внутренний UUID списка
- `list_ms_id` — id списка в MS Graph
- `title` (обязательно) — название
- `importance` — `low`, `normal`, `high`
- `due_date` — дата дедлайна (YYYY-MM-DD) — legacy, используй `due_datetime`
- `due_datetime` (F1.2) — полный datetime с таймзоной, ISO 8601 (`2026-03-20T15:00:00+04:00` или `...Z`)
- `start_datetime` / `start_timezone` (F1.2) — дата старта и её таймзона (`Europe/Samara`)
- `body` — описание/заметки
- `body_content_type` (F1.3) — `text` (default) или `html`
- `reminder_datetime` — дата+время напоминания (ISO 8601)
- `is_reminder_on` — `true`/`false`
- `categories` — массив тегов `["тег1", "тег2"]`
- `recurrence` (F1.4) — объект повторения: `{"pattern":{"type":"daily","interval":1},"range":{"type":"noEnd","startDate":"2026-04-14"}}`

Пример с HTML-body:
```json
{"list_name":"personalos","title":"Отчёт","body":"<b>Сводка</b>: ...","body_content_type":"html"}
```

Пример с повторением каждую неделю в пн/ср/пт:
```json
{
  "list_name":"dev-coder",
  "title":"Daily standup",
  "recurrence":{
    "pattern":{"type":"weekly","interval":1,"daysOfWeek":["monday","wednesday","friday"]},
    "range":{"type":"noEnd","startDate":"2026-04-14"}
  }
}
```

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

### Checklist items (F1.5)

Получить checklist задачи:
```bash
curl -s -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/tasks/UUID/checklist"
```

Добавить пункт:
```bash
curl -s -X POST -H "X-API-Key: $TODO_SYNC_API_KEY" -H "Content-Type: application/json" \
  "$TODO_SYNC_URL/api/v1/tasks/UUID/checklist" \
  -d '{"displayName":"Купить хлеб","isChecked":false}'
```

Обновить один пункт (поточечно, без перезаписи всего списка):
```bash
curl -s -X PATCH -H "X-API-Key: $TODO_SYNC_API_KEY" -H "Content-Type: application/json" \
  "$TODO_SYNC_URL/api/v1/tasks/UUID/checklist/ITEM_ID" \
  -d '{"isChecked":true}'
```

Удалить пункт:
```bash
curl -s -X DELETE -H "X-API-Key: $TODO_SYNC_API_KEY" "$TODO_SYNC_URL/api/v1/tasks/UUID/checklist/ITEM_ID"
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
