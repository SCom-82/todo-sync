# ADR 0003 — Sync integrity: delta truncation (data loss), attachments push, linkedResource UI visibility

- **Статус:** Ready-to-implement (кодовый фикс). DATA-FIX VERIFIED RESOLVED 2026-06-15 12:03 UTC (§C-7-ter): bulk-delete completed<2026 по всем спискам (7815 задач) — КС-Финансы И Семья доходят до deltaLink (broke=False), remaining completed<2026 = 0. Кодовый фикс (defense-in-depth, §C-7 пп.1-bis/2/3 + Инвариант) — остаётся обязательным отдельным тикетом dev-coder.
- **Дата:** 2026-06-15
- **Версия на момент анализа:** 0.3.4 (прод)
- **Связанные:** ADR 0001 (F2.1 linked-resource push — статус Proposed), ADR 0002 (recurring completion fix — Accepted). Этот ADR закрывает три фронта одним документом, потому что у них общий корень — «нет исключения / частичный результат == успех», тот же класс, что в 0001/0002.
- **Затрагиваемые файлы:** `app/services/graph_client.py` (`_request`, `get_tasks_delta`, `_try_parse_truncated_json`, новый `Prefer`-header), `app/services/sync_service.py` (`pull_tasks_for_list` :442-444, push attachments :638-686), `app/services/attachment_service.py` (`create_reference` :74-76), `app/api/attachments.py` (`attach_url`), новые контрактные тесты.
- **Источник:** прод-логи (infra-ops, read-only) + точечный разбор кода + сверка с актуальной документацией Microsoft Graph v1.0 (цитаты ниже).

> Приоритет фронтов: **C (delta truncation) > B (attachments) > A (linkedResource UI)**. C — тихая безвозвратная потеря данных каждые 5 минут под `errors=0`; B — отсутствие фичи без потери; A — почти наверняка корректно работает по дизайну Graph.

---

## A. linkedResource — невидимость в UI

### A-1. Симптом
`POST .../linkedResources` → `201 Created`, `ms_id = 3b382e54-...` (GUID). В приложении MS To Do ссылка визуально не видна, хотя переданы `webUrl`, `displayName`, `applicationName=GitHub`.

### A-2. Что говорит документация Graph (load-bearing)
[linkedResource resource type](https://learn.microsoft.com/en-us/graph/api/resources/linkedresource?view=graph-rest-1.0):

> «A **linkedResource** object stores information about that source application, and lets you link back to the related item. **You can see the linkedResource in the task details view**, as shown.» (со скриншотом `todo-linkedresource-taskdetail.png`)

> «Some linkedResource objects are not associated with any web URLs, in which case, the **webUrl** property is not required. … The following is how a linkedResource appears **with and without a URL**.» (скриншот `todo-linkedresource.png`)

Свойства: `displayName` = «The title of the linkedResource»; `webUrl` = «Deep link to the linkedResource»; `id` = «Server generated ID … Inherited from entity» — **в JSON-примере id имеет форму GUID** (см. ADR 0001 §2).

### A-3. Вердикт
**WORKS AS DESIGNED на уровне sync. GUID в `ms_id` — штатный формат Graph для linkedResource, не признак провала push** (это уже зафиксировано в ADR 0001). По документации ресурс **должен** отображаться — и с URL, и без — но **только в task details view (панель деталей задачи)**, а НЕ как inline-чип в строке списка и НЕ как «вложение». Наиболее вероятная причина «не вижу»:

1. **Смотрят не туда** — linkedResource рендерится в панели деталей (открыть задачу), не в списке. Это первое, что надо исключить вручную.
2. **Платформенная задержка/кэш клиента** — desktop/mobile To Do кэширует детали; ресурс, добавленный через API, появляется после ре-синка клиента.
3. **Маловероятно, но возможно:** клиент To Do фильтрует linkedResources по `applicationName`/источнику (исторически он стабильно рендерит ресурсы от Outlook/Planner; произвольный `applicationName=GitHub` может не получить иконку, но текст/ссылка по доке должны быть).

**Документация НЕ специфицирует** рендеринг произвольного стороннего `applicationName` в конкретных клиентах — это **непроверённое допущение**. Поэтому:

- **Закрывать фронт A как «WORKS AS DESIGNED, UI-невидимость = клиентское поведение, не дефект sync»** — при условии ручной проверки, что ресурс виден в **task details view** (а не в списке). Это разовая ручная проверка, не код.
- **Если** в details view ресурс реально отсутствует при валидном `webUrl` — тогда и только тогда заводить отдельный тикет на live-контракт (T-LIVE из ADR 0001) и проверку, не теряется ли он при последующем delta-pull (см. фронт C — truncation мог «съесть» обратную синхронизацию linkedResource). **Подозрение на связь с C: если details view периодически теряет ресурс — это симптом C, а не A.**

Никакого payload-фикса фронт A не требует. Поле `webUrl` уже передаётся; `applicationName`/`displayName` — тоже.

---

## B. Attachments — файлы не пушатся в Graph

### B-1. Симптом
За 48 ч логов — только `GET .../attachments`, ни одного `POST`. Push local→Graph не наблюдается.

### B-2. Root cause (по коду — НЕ «не реализован»)
Push **реализован и подключён в цикл синка**, вопреки первой гипотезе. Доказательство по файлам:

- `attachment_service.create_file` (`attachment_service.py:49`) вызывает `_try_push_to_graph`.
- `_try_push_to_graph` (`attachment_service.py:102-113`) формирует корректный `POST` `#microsoft.graph.taskFileAttachment` с `contentBytes`.
- Фоновый ретрай: `sync_service.push_pending` (`sync_service.py:638-686`) выбирает `TaskAttachment.sync_status == "pending" AND content_bytes IS NOT NULL` и шлёт `create_attachment`. `push_pending` вызывается в `run_sync` каждый цикл.

Реальная причина «нулевых POST» — **поведенческая развилка двух путей вложения**, file:line:

1. **`attachment_service.create_reference` (`attachment_service.py:74-76`)** — путь «вложить URL» (`POST /tasks/{id}/attachments/url` → `api/attachments.py:48-66`, MCP-инструмент `todo_attach_url`). Тело:
   ```python
   # Reference attachments are stored locally only ...
   att.sync_status = "synced"   # :76  ← хардкод "synced" без какого-либо POST
   ```
   `content_bytes` у reference = `NULL`, поэтому фоновый push-loop (`:642` фильтр `content_bytes IS NOT NULL`) их **никогда не подхватит**. Reference-вложение **по дизайну никуда не уходит**, но при этом помечается `synced` (врёт, как и в классе багов ADR 0001). **Если пользователь/агент прикладывает ссылки (типичный кейс «прикрепить GitHub URL») — POST не происходит вообще. Это и есть наблюдаемый «ноль POST».**

2. **`create_file`** (реальный файл, `content_bytes` заполнен) — POST формируется. Нулевые POST по этому пути означают одно из: (а) за 48 ч **не загружали ни одного файла** (только URL-ссылки), либо (б) на момент `create_file` у задачи `task.ms_id is None` → `_try_push_to_graph` тихо `return` (`attachment_service.py:94`), а фоновый loop потом тоже пропускает, если задача так и не засинкалась. Лог-доказательство какой именно — нужно от infra-ops (был ли вообще `multipart`-upload). По симптому «ходят только GET» вероятнее (а): трафик — это reference-URL, не файлы.

### B-3. Что говорит документация Graph
[Create taskFileAttachment](https://learn.microsoft.com/en-us/graph/api/todotask-post-attachments?view=graph-rest-1.0):

> «This operation limits the size of the attachment you can add to **under 3 MB**. If the size of the file attachments is more than 3 MB, [**create an upload session**](taskfileattachment-createuploadsession) to upload the attachments.»

Обязательные поля `taskFileAttachment`: `contentBytes` (Required), `name` (Required); `contentType`, `size` — опциональны. Наш payload (`attachment_service.py:106-112`) корректен: содержит `@odata.type`, `name`, `contentType`, `contentBytes`, `size`. ✅

`createUploadSession` (для >3 МБ) **НЕ реализован**. Сейчас API жёстко режет 3 МБ (`MAX_ATTACHMENT_BYTES`, `api/attachments.py:29-33` → HTTP 413). То есть >3 МБ просто не принимаются — это осознанное ограничение, а не баг. Upload-session нужен только если хотим поддержать большие файлы; в текущем scope **не требуется**.

### B-4. Вердикт и объём фикса
- **Root cause: не «push не реализован», а (1) reference-URL путь врёт `synced` и не пушит по дизайну + (2) вероятно за период просто не было файловых upload, только URL.** Метод POST для <3 МБ — корректный по доке. Upload-session для >3 МБ — отдельная фича, вне scope.
- **Фикс (малый):**
  1. **Решить продуктово, что значит «прикрепить URL».** Если ссылка должна попадать в To Do — её правильный канал в Graph это **linkedResource, а не attachment** (attachment = файл с `contentBytes`; reference-URL в Graph todoTask-attachments не существует как тип). Поэтому `attach_url` / `todo_attach_url` должен создавать **linkedResource** (фронт A), а не локальный `TaskAttachment`-reference. Это убирает ложный `synced` и даёт видимую ссылку.
  2. Пока не переведено на linkedResource — **`create_reference` НЕ должен ставить `synced`**; честный статус — `local_only` (новый) или оставлять `pending` с пометкой, что Graph-эквивалента нет. Минимум — не врать `synced`.
  3. Файловый путь (`create_file` + push-loop) — оставить, он корректен; убедиться, что при `task.ms_id is None` вложение остаётся `pending` и переотправляется (это уже делает loop при появлении `ms_id`).
- **Upload-session (>3 МБ): отдельный тикет, низкий приоритет**, только если появится спрос на большие файлы.

---

## C. Delta-sync truncation — ТИХАЯ ПОТЕРЯ ДАННЫХ (критично)

### C-1. Симптом
Каждый цикл, оба списка:
```
WARNING graph_client: JSON parse failed (len=51147, attempt 1/3): Expecting ',' delimiter: line 1 column 50158 (char 50157)
... ×3 ...
WARNING get_tasks_delta: Skipping unparseable delta page ... skipped 1 pages, got 1050 items
```
Второй список `len=94999`. Синк репортит `errors=0`, `status=success`.

### C-2. Развенчание первой гипотезы
Гипотеза «тело обрезается нашим кодом до ~50KB» — **частично неверна**. В `graph_client._request` нет никакого среза по 50KB: тело читается целиком через `response.json()` / `response.content` (`graph_client.py:83, 85`). `len=51147` — это **число фактически полученных байт**, не наш лимит.

Но 51147 байт **физически не могут вместить 1050 задач** (1050 полных todoTask × ~150–300 байт = 150–300 КБ). Значит **тело реально усечено на транспортном уровне ДО нашего Python-кода** — мы получили ~51 КБ из значительно большего ответа. Парсер падает «Expecting ',' delimiter» ровно в конце полученных байт — **классическая сигнатура оборванного посреди массива HTTP-ответа** (валидный JSON до точки обрыва, дальше нужна запятая/закрытие, которых нет).

Стабильный обрыв у ~50157 (~49 КБ) указывает на **буфер промежуточного звена** (Traefik/Coolify reverse-proxy перед todo-sync, либо HTTP/2-фрейм, либо сам Graph отдаёт один гигантский page и он рвётся в транзите). **Какое именно звено рвёт — непроверённое допущение**, требует live-repro (§C-6). Но направление однозначно: ответ усечён в транзите, Graph не «шлёт битый JSON».

«got 1050 items» в логе — это либо накопленное за предыдущие успешные циклы, либо частично восстановленное; оно **не** доказывает, что 1050 реально дошли в этом цикле.

### C-3. Почему это безвозвратная потеря (file:line)
Цепочка проглатывания:

1. `_request` (`graph_client.py:82-101`): `response.json()` падает → `except` → `_try_parse_truncated_json(text)` (`graph_client.py:15-39`). Этот «костыль» через `raw_decode` пытается выдрать первый валидный JSON-объект. Но усечённый ответ — это **незакрытый** top-level объект (массив `value` оборван), `raw_decode` от позиции 0 не находит полного значения → `None`. После 3 ретраев (тот же обрыв) → `raise`.
2. `get_tasks_delta` (`graph_client.py:207-220`): ловит исключение → `skipped_pages += 1` → **`break`** (не может достать `@odata.nextLink` из битого тела) → возвращает `{"value": all_values, "delta_link": result.get("@odata.deltaLink")}`.
3. `pull_tasks_for_list` (`sync_service.py:442-444`):
   ```python
   state.delta_link = delta_result.get("delta_link")   # :442
   state.last_sync_at = ...                              # :443
   state.last_sync_status = "success"                    # :444  ← хардкод "success"
   ```
   - Если до обрыва успели распарситься страницы (`result` непустой с `@odata.deltaLink`) → `delta_link` **продвигается**, раунд помечается `success`, **пропущенные изменения теряются НАВСЕГДА** (следующий раунд стартует с нового токена).
   - Если упала первая же страница → `delta_link=None`, тот же токен → **тот же обрыв повторяется каждый цикл**, элементы за точкой обрыва **никогда не дойдут**.

В обоих случаях — `errors=0`, `status=success`. **Потеря невидима для мониторинга.** Это ровно тот же анти-паттерн «частичный результат == успех», что в ADR 0001/0002.

### C-4. Что говорит документация Graph (механизм, который мы не используем)
[todoTask: delta](https://learn.microsoft.com/en-us/graph/api/todotask-delta?view=graph-rest-1.0):

> Request headers: **`Prefer: odata.maxpagesize={x}`. Optional.**

> «If the request is successful, the response would include a state token, which is either a *skipToken* (in an `@odata.nextLink` … ) or a *deltaToken* (in an `@odata.deltaLink` …). Respectively, they indicate whether you should continue with the round or you have completed getting all the changes.»

Мы **НЕ отправляем `Prefer: odata.maxpagesize`** (подтверждено: `grep -rn "Prefer\|maxpagesize" app/` → NONE). Без него Graph отдаёт раунд **большими страницами** (вплоть до одной гигантской) — и именно она рвётся в транзите. С маленьким `maxpagesize` каждая страница помещается под буфер, парсится целиком, а полный набор добирается по `@odata.nextLink`. Это **документированный штатный механизм пагинации**, который мы обходим.

### C-5. Серьёзность и связь с прошлыми багами
**CRITICAL.** Систематическая безвозвратная потеря входящих изменений (completion, новые задачи, новые linkedResource/attachment-метаданные) каждые 5 минут, замаскированная `errors=0`.

**Вероятная связь с ADR 0002 (recurring completion revert):** completed-сиблинг (ветка B в ADR 0002) приходит в delta **отдельной записью**. Если эта запись лежит за точкой обрыва (≥50 КБ) — она **никогда не ингестится**, и sync видит только авто-прокрученную серию в `notStarted` → выглядит как откат completion. ADR 0002 лечил это «completed-intent protection» (симптоматически, на стороне нашей логики), но **корневой канал — truncation — мог делать сиблинга недоступным в принципе**. То есть C — кандидат на **общий корень** нескольких ранее симптоматически чиненных sync-багов.

### C-6. Непроверённое допущение → обязательный live-repro ДО кода
По правилу проекта (поведение внешней системы не выводим логически): **где именно рвётся тело — Graph / Traefik / HTTP-стек — не подтверждено**. До реализации фикса:

- Воспроизвести `GET .../tasks/delta` на живом Graph для проблемного списка **без** `Prefer` (ожидаем обрыв ~50 КБ) и **с** `Prefer: odata.maxpagesize=50` (ожидаем маленькие страницы + `@odata.nextLink`, полный набор без обрыва).
- Снять `curl` напрямую к Graph (минуя Traefik) vs через наш сервис — локализовать звено обрыва. Если прямой `curl` к Graph не рвётся, а через наш контейнер рвётся → виноват reverse-proxy буфер (тикет infra-ops на лимиты Traefik); если рвётся и напрямую → Graph/HTTP/2, лечится исключительно пагинацией.

### C-6-bis. Live-repro результаты (2026-06-15, infra-ops, read-only из контейнера + хоста)

**Метод:** токен добыт штатно внутри контейнера todo-sync (`r0gww8s8ogw0oo8gc8cow4s4-075048641670`, coolify-main) через `auth_service.get_access_token()`; curl к Graph из контейнера и с хоста сервера. Токен не светился. Проблемный список — **«КС-Финансы»** (`...AAjn-1oAAAAA=`).

| Тест | bytes | valid JSON | nextLink | headers |
|---|---|---|---|---|
| T1 первая страница, no Prefer, `--compressed` | 39949 | **VALID** | YES | chunked, gzip |
| T1 проблемная страница (skiptoken из лога), `--compressed` | 51147 | **NO** (char 50157) | NO | chunked, gzip |
| T1b та же страница, `Accept-Encoding: identity` | 51147 | **NO** (char 50157) | NO | chunked, без encoding |
| T2 `Prefer: odata.maxpagesize=50`, полный обход | стр.1-21 по 50 шт = 1050 items VALID; **стр.22 — 51147 / NO** (char 50157) | — | стр.1-21 YES, стр.22 NO | `Preference-Applied: odata.track-changes` (НЕ `odata.maxpagesize`) |
| с **хоста** сервера (минуя контейнер) | 51147 | **NO** (char 50157) | NO | chunked, gzip |
| egress-прокси в контейнере (`http_proxy/https_proxy`) | — | — | — | **отсутствует** |

**Опровергнутые гипотезы §C-2/§C-6:**
- ❌ **Не reverse-proxy / Traefik.** Traefik у нас только на ВХОД нашего API; к исходящему трафику к Graph отношения не имеет. Egress-прокси в контейнере нет.
- ❌ **Не gzip / декомпрессия.** T1 (gzip) и T1b (`identity`) рвутся идентично — на одном и том же байте 51147 / символе 50157. Декомпрессия ни при чём (что согласуется с кодом: httpx падает на `response.json()` уже прочитанных байт, а не на распаковке).
- ❌ **Не «гигантская страница, которую надо разбить пагинацией».** Страницы по 50 задач (1050 items за 21 страницу) собираются ВАЛИДНО. Обрыв не зависит от размера страницы.
- ❌ **`Prefer: odata.maxpagesize` НЕ лечит.** Graph отвечает `Preference-Applied: odata.track-changes` (для delta-эндпоинта maxpagesize применяется по-своему/частично). Битая задача просто переезжает на страницу 22, и эта страница рвётся так же. **План §C-7 п.1 (maxpagesize+nextLink) ОПРОВЕРГНУТ как решение truncation.**

**Истинный root cause — Graph отдаёт `InternalServerError` ВНУТРИ `200 OK` и обрывает тело.** Одна задача в списке (завершена 2022-12-20) имеет битый `linkedResources`. Graph при сериализации этой задачи вместо linkedResource-объекта вставляет:
```
"linkedResources":[{"error":{"code":"InternalServerError",
 "message":"Invalid object within the collection response from workload for navigation property
   linkedResources with declaring type microsoft.graph.todoTask.
   Expected a JObject, but got Jtoken type - Null","innerError":{"date":"...","request-id":"...",...}}}
```
…после чего **обрывает запись тела**: открытые `{` и `[` не закрыты, нет `@odata.nextLink`/`@odata.deltaLink`, нет завершающих `]}`. HTTP-уровень при этом завершается штатно (chunked), поэтому httpx ловит именно `JSONDecodeError` на 51147 прочитанных байтах (а не `RemoteProtocolError`). Воспроизводится при КАЖДОМ запросе той страницы (тело идентично, меняется только `innerError.date` и `request-id`) → стабильная безвозвратная потеря 50 задач/цикл (вся страница 22 + всё, что за ней).

**Звено обрыва: 100% Graph (server-side serialization bug на конкретной битой задаче).** Не httpx, не сеть, не proxy, не page-size.

**Уточняющие пункты (1) финальный 0-chunk, (2) наличие skiptoken в урезанном теле, (3) точная позиция/хвост за обрывом, (4) достижимость битой задачи через одиночный GET с/без `$expand=linkedResources` — НЕ доведены до конца** (второй диагностический заход прервался сетевой ошибкой). Помечены как **непроверенные допущения**; для финального дизайна фикса их стоит добить (см. C-7-bis, требует ещё одного read-only repro). Сильное допущение из имеющихся данных: тело урезано ДО блока nextLink (его в теле нет) → **skiptoken следующей страницы потерян**, штатно продолжить пагинацию после битой страницы нельзя.

### C-7. Решение (ПЕРЕСМОТРЕНО после live-repro C-6-bis)

> **`Prefer: odata.maxpagesize` исключён как фикс truncation** — repro показал, что обрыв вызывает битый `linkedResources` в одной задаче на стороне Graph, а не размер страницы. Ниже — пересмотренный план. (Сам по себе `maxpagesize` всё ещё полезен как гигиена пагинации — даёт детерминированные мелкие страницы и точную локализацию битой страницы — но НЕ как лекарство от потери.)

1. **(СНЯТО как фикс truncation)** ~~Слать `Prefer: odata.maxpagesize=50`~~. Опровергнуто repro. Опционально оставить maxpagesize для управляемости страниц, но без иллюзии, что он лечит обрыв.

1-bis. **Грациозно обрабатывать оборванную страницу как RECOVERABLE-skip с продолжением раунда, а НЕ как тихий success.** Когда страница не парсится:
   - извлечь из урезанного тела `request-id` встроенного Graph-error (для алерта/тикета в MS) и попытаться извлечь `$skiptoken` следующей страницы, ЕСЛИ он присутствует до точки обрыва;
   - **если skiptoken восстановим** → пропустить ТОЛЬКО битую страницу, продолжить пагинацию дальше (теряем 1 страницу, не хвост), пометить раунд `partial`;
   - **если skiptoken потерян** (наиболее вероятный случай — nextLink идёт после точки обрыва) → раунд `partial/error`, `delta_link` НЕ продвигать, `errors++`, alert. Battle-test (C-7-bis) должен подтвердить, какой из случаев реален.
1-ter. **Точечно нейтрализовать битую задачу (Graph-side bug workaround).** Корень — одна конкретная задача с `linkedResources` = `Null`-Jtoken, которую Graph не может сериализовать. Варианты (нужен battle-test C-7-bis):
   - если одиночный `GET .../tasks/{id}` БЕЗ `$expand=linkedResources` отдаёт задачу нормально — то delta-эндпоинт всё равно тащит linkedResources inline и ломается, тут отказ от expand не помогает (delta не управляется expand'ом так же, как обычный list). Но это подтверждает, что данные задачи целы, битый только её linkedResources-навигатор.
   - **прагматичный фикс данных:** «толкнуть» задачу (PATCH любого поля / снять-поставить linkedResource через `DELETE`+пересоздание битого linkedResource), чтобы Graph пересериализовал её корректно. Это разовая операция на ОДНОЙ задаче — но это **write на стороне пользовательских данных Graph**, не на нашем сервере; делать только с явного апрува и через интерактивную сессию, не из кода синка. Может полностью убрать обрыв без кода.
   - **тикет в Microsoft** с `request-id` из embedded error — это их server-side баг сериализации.

2. **Убрать тихий skip-and-break + ложный `success`.** При неустранимой ошибке парсинга страницы:
   - НЕ продвигать `state.delta_link` (не сохранять новый токен раунда);
   - НЕ ставить `last_sync_status = "success"` — ставить `error`/`partial`;
   - инкрементить `errors` в отчёте синка (сейчас `errors=0` при потере — главный масковщик);
   - логировать на уровне `error`, желательно alert.
3. **Карантинировать/удалить `_try_parse_truncated_json`.** «Восстановление» частичного тела и трактовка его как полного — сам механизм тихой потери. Если оставлять как диагностику — только под `logger.error` и **без** возврата частичного результата как успешного.
4. **(СНЯТО)** ~~тикет infra-ops на буфер Traefik~~ — repro доказал, что proxy/буфер ни при чём (Traefik не на egress-пути; обрыв идентичен с хоста и из контейнера). Инфраструктурного фикса НЕ требуется.

### C-7-bis. Остаточный battle-test — ВЫПОЛНЕН 2026-06-15 (read-only из контейнера)

**Статус: все 4 факта подтверждены. Непроверенных допущений не осталось.**

#### Битая задача — опознана точно

| Поле | Значение |
|------|----------|
| **title** | «Почта Банк. Оформление документов, отправка договоров» |
| **task id** | `AQMkADAwATNiZmYAZC1kMzA3LTAyZGYtMDACLTAwCgBGAAADedwyaoZUtkCYiCj7jr-IYgcAeFexHOuSm0ilpQAjRWuhUn8AAjn-1oAAAAB4V7Ec65KbSKWlACNFa6FSfwACOgBvrQAAAA==` |
| **status** | `completed` |
| **completedDateTime** | `2022-12-20T00:00:00Z` |
| **createdDateTime** | `2020-03-23T15:27:05Z` |
| **lastModifiedDateTime** | `2022-12-20T06:44:02Z` |
| **hasAttachments** | `true` |
| **Позиция на стр. 22** | задача #50 из 50 (последняя на странице) |

На странице 22 (maxpagesize=50) первые 49 задач приходят валидно; Graph спотыкается именно на #50 (этой задаче) и обрывает тело после `}}}` вставленного error-объекта без закрытия массива `value` и корневого объекта.

#### Факт 1 — HTTP-stream штатен (JSONDecodeError, не TransportError) ✅

`curl -v --raw` к странице 22: тело = 51191 байт, последние байты hex:
```
...7d 7d 7d 0d 0a  30 0d 0a 0d 0a
              ^^^^^^^^^^^^^^^^^^^
              финальный 0-chunk (chunked transfer)
```
`Connection #0 to host graph.microsoft.com left intact`. Стрим закрыт корректно. Graph **намеренно** завершает chunked-поток после сериализации error-объекта. Ошибка в нашем коде — `JSONDecodeError` (усечённый JSON, нет закрывающих `]}`), а не `RemoteProtocolError`/`ChunkedEncodingError`.

**Вывод:** транспортного обрыва нет. Это server-side serialization bug Graph, который намеренно завершает ответ в «некорректном» состоянии JSON, но корректном состоянии HTTP.

#### Факт 2 — skiptoken ДО точки обрыва: НЕТ (hard-stop) ✅

В теле страницы 22 до char 50157 `@odata.nextLink` как завершённый JSON-ключ **отсутствует**. Regex-grep по `@odata.nextLink` и `skiptoken=` в первых 60000 символах — 0 совпадений. Тело обрывается после `}}}` (конец error-объекта linkedResources), незакрытый массив `value` и корневой объект не содержат `nextLink`.

Skiptoken страницы 23 **известен** (он живёт в `@odata.nextLink` страницы 21, которая пришла валидно), но его нет внутри самого тела страницы 22. Из сломанного тела страницы 22 next-токен извлечь нельзя.

**Вывод: hard-stop.** Recoverable-skip через nextLink из страницы 22 невозможен. Единственный обходной путь — хранить nextLink каждой страницы ДО запроса следующей и при ошибке переиспользовать последний валидный. Это требует изменения алгоритма пагинации в `get_tasks_delta` (хранить `prev_next_link`). Без этого хвост (страницы 23+) теряется полностью.

#### Факт 3 — Объём потерянного хвоста ✅

`GET /tasks?$count=true` для todo-списка не возвращает `@odata.count` (известное ограничение Graph todo API). По delta-обходу: 21 стр × 50 + 49 валидных на стр.22 = **1099 задач получено** до точки обрыва. Страница 22 содержит 49 успешных + 1 битую (#50). Хвост (стр.23+) — минимум десятки задач, точная цифра неизвестна без полного прохода после фикса.

**Дополнительно**: из 49 успешно извлечённых на стр.22 задач (partial-parse) текущий код (`_try_parse_truncated_json`) их **теряет** — возвращает `None` и пропускает страницу целиком. Т.е. реальные потери = 49 задач страницы 22 + все задачи страниц 23+, а не только «хвост после битой».

#### Факт 4 — Достижимость битой задачи ✅

| Запрос | Результат |
|--------|-----------|
| `GET .../tasks/{id}` (без expand) | **Успех** — задача возвращается чисто: title, status, completedDateTime, hasAttachments=true. linkedResources в ответе отсутствует (Graph не тащит их без expand) |
| `GET .../tasks/{id}?$expand=linkedResources` | **InternalServerError** — тот же error-объект: `"Expected a JObject, but got Jtoken type - Null"` |

Сама задача данными цела. Битый только навигатор `linkedResources` (одна или несколько записей с `Null` вместо JObject в бэкенд-хранилище Graph). Задача завершена в 2022, `hasAttachments: true` — вероятно, когда-то к ней прикрепили вложение/ссылку, которая в хранилище осталась как Null-объект.

**Вывод:** задача читаема без expand. Это подтверждает жизнеспособность точечного data-fix: если удалить битый linkedResource или «толкнуть» задачу (PATCH + DELETE linkedResource), Graph пересериализует её корректно и обрыв уйдёт.

#### Итоговый вердикт battle-test

| Вопрос | Ответ |
|--------|-------|
| HTTP-стрим штатен? | ДА — 0-chunk присутствует, `JSONDecodeError` а не `TransportError` |
| skiptoken до обрыва? | НЕТ — hard-stop; next-token страницы 23 потерян |
| Объём потерь | 49 задач стр.22 + весь хвост стр.23+ |
| Битая задача читаема? | ДА без expand, НЕТ с expand=linkedResources |
| Recoverable-skip возможен? | Только если хранить `prev_next_link` — не из тела стр.22 |
| Data-fix нужен? | ДА — без него delta будет рваться на этой задаче вечно |

#### Предложенный минимальный data-fix (НЕ ВЫПОЛНЯТЬ без отдельного апрува)

**Вариант A — удалить битый linkedResource (рекомендуется):**
```
# Шаг 1: GET linkedResources задачи (через list, не expand — expand рвётся)
GET /v1.0/me/todo/lists/{listId}/tasks/{taskId}/linkedResources

# Шаг 2: для каждого найденного linkedResource с битыми данными:
DELETE /v1.0/me/todo/lists/{listId}/tasks/{taskId}/linkedResources/{linkedResourceId}
```
После удаления Graph сможет сериализовать задачу без ошибки. Дельта перестанет рваться.

**Риск/обратимость:** задача завершена в 2022, `linkedResources` — это либо ссылка на внешний ресурс (GitHub, URL), либо артефакт битого push нашего же кода (см. фронт B). Удаление linkedResource **необратимо** (без пересоздания). Данные самой задачи (title, completedDateTime, тело) не затрагиваются. Риск — **низкий**: задача 4-летней давности, завершена, linkedResource битый (Graph его уже не может прочитать сам).

**Вариант B — PATCH + пересоздать linkedResource:**
Если содержимое linkedResource важно — сначала `GET .../linkedResources` (не через expand!), записать данные, потом DELETE + POST новый. Но т.к. expand рвётся, данные linkedResource могут быть недоступны.

**Вариант C — тикет в Microsoft:**
Передать `request-id` из embedded InternalServerError (виден в сыром теле страницы 22 в поле `innerError`) в Microsoft Support как server-side bug Graph serialization. Параллельно с вариантом A/B — не вместо.

**Команда для Варианта A (выполнить после апрува в интерактивной сессии infra-ops):**
```bash
# 1. Посмотреть linkedResources (через прямой GET /linkedResources, не $expand):
docker exec r0gww8s8ogw0oo8gc8cow4s4-075048641670 bash -c '
TOKEN=$(cat /tmp/tok.txt)
LIST_ID="AAjn-1oAAAAA="  # КС-Финансы (короткий хвост)
TASK_ID="AQMkADAwATNiZmYAZC1kMzA3LTAyZGYtMDACLTAwCgBGAAADedwyaoZUtkCYiCj7jr-IYgcAeFexHOuSm0ilpQAjRWuhUn8AAjn-1oAAAAB4V7Ec65KbSKWlACNFa6FSfwACOgBvrQAAAA=="
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://graph.microsoft.com/v1.0/me/todo/lists/$LIST_ID/tasks/$TASK_ID/linkedResources"
'

# 2. После проверки — удалить каждый linkedResource (ТОЛЬКО ПОСЛЕ АПРУВА):
# DELETE /v1.0/me/todo/lists/$LIST_ID/tasks/$TASK_ID/linkedResources/{linkedResourceId}
```

Эти 4 пункта — **подтверждены**; все допущения из §C-6-bis сняты. Финальный дизайн фикса определён (см. C-7 пп. 1-bis, 1-ter, 2, 3).

### C-7-bis. Верификация data-fix (2026-06-15 15:18 МСК / 11:18 UTC)

Пользователь удалил всю серию «Почта Банк. Оформление документов, отправка договоров» из КС-Финансы (в т.ч. все recurring-инстансы с битым linkedResources Wunderlist 2019).

**Delta-обход после удаления (read-only, из контейнера todo-sync):**

| Тест | Результат |
|------|-----------|
| `Prefer: odata.maxpagesize=50` | 2 страницы, 53 задачи, **deltaLink получен** — JSONDecodeError: 0 |
| Без Prefer (prod-baseline) | 2 страницы, 53 задачи, **deltaLink получен** — JSONDecodeError: 0 |
| Логи parse failed за 30 мин | **0 ошибок** для КС-Финансы |

**КС-Финансы: verified resolved.**

**Дополнительно обнаружен список «Семья»** (`AAjn-1oUAAAA=`) с идентичным симптомом (рвётся на стр.5, char 89930, len=94999, 200 задач до обрыва). Тот же класс бага — Wunderlist 2019 recurring-инстанс с Null JToken в linkedResources.

### C-7-ter. BULK-DELETE completed<2026 по всем спискам — VERIFIED RESOLVED (2026-06-15 12:03 UTC)

По авторизации пользователя удалены все `completed` задачи с `completedDateTime < 2026-01-01` во всех 20 непустых списках (7815 шт., `err=0`, 10 PROTECTED в «Семья» = 5 notStarted + 5 completed-2026). Это вычистило битые linkedResources-инстансы в обоих затронутых списках.

| Тест | Результат |
|------|-----------|
| Delta КС-Финансы | 2 стр, 53 задачи, **deltaLink, broke=False** |
| Delta Семья | 2 стр, 10 задач, **deltaLink, broke=False** |
| GRAND remaining completed<2026 (все 48 списков) | **0** |

**И КС-Финансы, И Семья: verified resolved.** Backup+лог: `_system/infra/incidents/2026-06-15-all-lists-{backup,deletion-log}.jsonl`. Кодовый фикс (пп.1-bis/2/3 + Инвариант) остаётся обязательным независимо — защита от будущих битых тасков.

---

## Инвариант проекта (расширение P0 push-verify из ADR 0001/0002 на pull-границу)

> **Контракт целостности pull:** delta-раунд считается успешным (`status=success`, продвижение `delta_link`) **только если все страницы раунда распарсились без потерь**. Любой неустранимый сбой парсинга/пагинации → раунд `partial/error`, `delta_link` НЕ продвигается, `errors` инкрементится. **Запрещено** возвращать частично восстановленное тело как полное и помечать раунд успешным. «Частичный результат == успех» — баг.

> **Контрактные тесты на HTTP-границе `graph_client._request`** (мок `httpx.AsyncClient.request`, НЕ мок самих методов — урок S118/ADR 0001):
> - **C-T1:** ответ >50 КБ, валидный, в нескольких страницах через `@odata.nextLink` (мок отдаёт page1 с nextLink → page2 с deltaLink). Ассерт: все items собраны, `delta_link` = финальный deltaToken. Тест с **реальным >50 КБ телом** (генерировать 1100 фейковых задач), чтобы поймать регресс truncation-обработки.
> - **C-T2:** усечённый ответ (валидный JSON оборван на `…"id":"x`). Ассерт: `pull_tasks_for_list` НЕ продвигает `state.delta_link`, `last_sync_status != "success"`, `errors >= 1`. Фиксирует, что тихая потеря больше невозможна.
> - **C-T3:** ассерт, что delta-запрос несёт заголовок `Prefer: odata.maxpagesize=...` (перехват в моке `request`).
> - **B-T1:** `create_file` happy → перехват на HTTP-границе подтверждает, что `POST .../attachments` с `@odata.type=#microsoft.graph.taskFileAttachment`, `name`, `contentBytes` **реально формируется** (тело, не мок метода). Ассерт `sync_status=synced` только при наличии `id`.
> - **B-T2:** `attach_url`/`create_reference` НЕ ставит `synced` (после фикса) и НЕ шлёт attachment-POST.

---

## Последствия
- C: устранение систематической тихой потери входящих изменений; `errors`/`status` перестают врать; вероятно снимается корень части recurring-симптомов (ADR 0002 — проверить после фикса C, не деградировал ли «completed-sibling» путь без truncation).
- B: «прикрепить URL» получает честную семантику (linkedResource или честный `local_only`); файловый push не трогаем — он корректен; >3 МБ остаётся осознанно неподдержанным.
- A: закрывается как WAD после ручной проверки task details view; код не меняем.
- Серверная часть: возможен тикет infra-ops (Traefik response-буфер) — только после repro §C-6; миграций БД нет (для B п.2 опц. новый enum-статус `local_only` — мелкая миграция, на усмотрение dev-coder).
