# ADR 0002 — Recurring tasks: write-path, completion-tracking, status-only PATCH

- **Статус:** Accepted (verified на живом Graph)
- **Дата:** 2026-06-14
- **Версии:** v0.3.1 (write-path) → v0.3.2 (pull-path + health) → v0.3.3 (status-only completion PATCH)
- **Связанные:** ADR 0001 (F2.1 linked-resource push — общий корень «нет исключения == успех»)
- **Затрагиваемые файлы:** `app/services/task_service.py`, `app/services/sync_service.py`, `app/api/tasks.py`, `app/api/sync.py`, `app/models.py`, `app/schemas.py`, миграция `007_recurring_completion_tracking.py`
- **Источник реконструкции:** commit-сообщения 737ca61, b9d0282, 77ca780, 44b2cc3, 58a0ef4. Полный архитектурный документ с эмпирикой (прямые Graph REST-тесты, infra-ops ticket `4c3bfed0`) — в vault `_system/docs/architect/2026-06-14-todo-sync-recurrence-model.md`. Этот файл — сжатая репо-копия для контекста разработчика.

---

## 1. Контекст и проблема

Recurring-задачи в todo-sync вели себя неправильно на двух последовательно вскрытых уровнях:

**Уровень 1 — write-path (v0.3.1).** Recurring-задачи **никогда не доходили до Graph** через наш сервис. `_task_to_graph_payload` строил recurrence-блок со всеми подполями, отсутствующие отправлял как `null` (`index`/`month`/`dayOfMonth`/`daysOfWeek`/`firstDayOfWeek`/`endDate`/`numberOfOccurrences`). Graph отвергает такой payload HTTP 400 (`range.endDate=null` нарушает `Edm.Date[Nullable=False]`). Дополнительно Graph v1.0 требует `dueDateTime` для любой recurring (недокументированное требование — выяснено эмпирически, Graph 400 без него). Ошибка push при этом **молча проглатывалась** — задача висела в `pending_push` с `ms_id=null`, баг невидим.

**Уровень 2 — pull-path completion (v0.3.2).** После того как create заработал, completion recurring всё ещё откатывался при delta sync. Эмпирика (прямой Graph REST) показала, что completion recurring — это **гибрид A+B**, а не «чистый флип того же id»:
- **A:** `PATCH {status:completed}` на серию → Graph отдаёт 200, но тот же `ms_id` при следующем GET снова `notStarted` с `dueDateTime`, сдвинутым на +1 интервал. Серию несёт исходный id (авто-прокрутка вперёд).
- **B:** параллельно Graph спавнит **новый id** со `status=completed` + `completedDateTime` + датой закрытого вхождения — completed-сиблинг. Delta отдаёт обе записи.

Sync видел, что его completed-задача (трекаемая по `ms_id`) внезапно снова `notStarted`, и слепо принимал это за uncomplete → откат completion.

**Уровень 3 — completion-PATCH Graph 400 (v0.3.3, пойман только прод-verify).** Write-path фикс прошёл 229 юнит-тестов + QA GO, но `complete_task` для recurring всё ещё падал: он слал полный `_task_to_graph_payload` PATCH (recurrence + dueDateTime + status) → Graph отвечает HTTP 400 на completion recurring, когда в теле присутствуют `recurrence`/`dueDateTime`. Минимальный `PATCH {"status":"completed"}` → 200. Completion не доходил до Graph, conflict-guard откатывал локальный `completed`.

---

## 2. Решение

**Write-path (v0.3.1, 737ca61):**
- `_recurrence_to_graph`: эмитить только заполненные не-sentinel подполя; стрипать sentinel-значения, которыми Graph бэкфиллит ответ (`month=0`, `daysOfWeek=[]`, `endDate="0001-01-01"`, `numberOfOccurrences=0`, `dayOfMonth=0`, `index="first"` для non-relative). `_task_to_graph_payload` использует его вместо сырого JSONB.
- `_validate_recurrence_has_due`: ValueError, если recurrence задан без `dueDateTime`/`due_date`; вызывается в `create_task` до push. API мапит ValueError с "recurring"/"recurrence" в 422 (не 404).
- **P0 push-verify:** `_try_push_task` валидирует `id` в ответе Graph (должен быть настоящий Graph base64, не локальный UUID4) и возвращает bool. Non-2xx/исключение оставляют задачу в `pending_push` — `synced` никогда не выставляется молча на провале.

**Pull-path (v0.3.2, b9d0282) — гибрид A+B:**
- **Completed-intent protection (ветка A, только `recurrence IS NOT NULL`):** при pull трекаемого `ms_id` серии в `notStarted` со сдвинутым вперёд `dueDateTime` — НЕ откатывать (это авто-прокрутка после нашего complete). Реальный uncomplete (дата та же или назад) различаем и принимаем.
- **Ингест сиблинга (ветка B):** новый `ms_id` со `status=completed` ингестим как отдельную completed-задачу; **не** дедуплицируем с серией (ms_id — единственный ключ).
- `local_modified_at` как арбитр conflict-guard вместо server-side `updated_at` (момент COMMIT, не действия пользователя).
- `complete_task` для recurring: не ставить `synced` после push — оставлять `pending_push`, чтобы conflict-guard поймал авто-прокрутку.
- Миграция 007: `last_completed_occurrence_date` (completion-intent marker, НЕ механизм истории — история приходит сиблингами B), `local_modified_at`.
- Health: `/sync/trigger` возвращает читаемый 500+traceback вместо пустой ошибки; `delta_skip_rate_pct` → `delta_success_rate_pct` (был misnomer).

**Status-only PATCH (v0.3.3, 44b2cc3 / 58a0ef4):**
- `complete_task`/`uncomplete_task` шлют **минимальный status-only payload** (`_completion_patch_payload`) через `_try_push_task payload_override` — без recurrence/dueDateTime/title. Применяется ко **всем** задачам (единообразно и безопасно, убирает ветвление «recurring vs обычная»).
- `update_task` опускает `recurrence` из PATCH, если оно не в изменённом наборе полей (тот же латентный 400 на recurring rename/смене даты).

---

## 3. Зафиксированный инвариант (урок mock-blindness)

Write-path фикс прошёл все юнит-тесты, но completion-400 поймал **только прод-verify на живом Graph** — потому что тесты мокали `_try_push_task` целиком (`return_value=True`), то есть **ровно ту функцию, что строит payload**. Исходящий PATCH никогда не инспектировался. Юнит-моки слепы к контракту Graph.

> **Инвариант проекта:** для каждой write-операции, форма исходящего payload которой зависит от контракта внешней системы, обязателен **contract-тест на ТЕЛО исходящего запроса**, мокающий **на HTTP-границе `graph_client`** (`create_task`/`update_task`), а **не выше** (`_try_push_task` или сам мокаемый метод). Тест перехватывает `data`, переданный в `graph_client.*`, и ассертит форму payload (например: recurring completion → `{"status":"completed",...}` без `recurrence`/`dueDateTime`/`title`). Юнит-моки остаются для скорости, но поверх них — contract-проверка payload + обязательный live-Graph verify перед закрытием тикета.

Реализовано в `tests/test_completion_patch_contract.py` (44b2cc3/58a0ef4); edge-cases и health-coverage — `tests/test_recurring_pull_path.py` (TestRecurringEdgeCases) и `tests/test_recurring_health.py` (77ca780).

---

## 4. Последствия

- Recurring наконец создаётся в Graph (главный, ранее невидимый дефект).
- Completion перестаёт молча откатываться: авто-прокрутка серии (A) не путается с uncomplete; completed-сиблинг (B) ингестится как факт завершения — история приходит бесплатно от источника, без новой сущности.
- conflict-guard корректен для всех задач (отвязка от COMMIT-времени).
- Один push-verify (P0) лечит write-path, completion и весь класс ложно-`synced` (общий корень с ADR 0001 / F2.1).
- Прод-данные: застрявшие `pending_push` recurring + ложные `synced`+`ms_id IS NULL` требуют data-fix — применяет infra-ops после деплоя.
- Миграция 007 применяется на проде через infra-ops (не dev-coder напрямую).

Полный набор тестов: 238 зелёных (v0.3.3).
