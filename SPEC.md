# Connector v2 — спецификация

Полное переписывание библиотеки. Чистый брейк с v1 (без обратной совместимости),
дух API сохраняется: `db.users.equal(status="active").more(age=18).items`.
Единственный намеренный шим совместимости: `schema_from_json` понимает старые
v1-ключи JSON-схемы (`is_primary`, `langs: true`) наряду с новыми.

## 1. Базовые решения

| Решение | Выбор |
|---|---|
| Драйвер | psycopg 3 (+psycopg_pool) |
| Совместимость | чистый брейк, v2.0 |
| Структура | пакет `connector/` с модулями, pyproject.toml |
| Python | 3.10+ |
| Классы | `PostgreSQLConnector` (sync), `AsyncPostgreSQLConnector` (async), без алиасов |
| Строка результата | Row-объект: `row["name"]`, `row.name`, `row.update(...)`, `row.delete()` (по PK), `row.to_dict()` |
| Ошибки | своя иерархия исключений (`ConnectorError` → `QueryError`, `SchemaError`, `ConnectionFailed`, ...); никаких `print(e)` + `[]` |
| Reconnect | автоматический, exponential backoff, лимит попыток (настраиваемый), не вечный цикл |

Старый `connector.py` удаляется (история остаётся в git).

## 2. Конфигурация

Источники **взаимоисключающие**:

- `PostgreSQLConnector()` — без аргументов → env (`.env` через python-dotenv + переменные окружения).
- `PostgreSQLConnector(config_json="config.json")` → только JSON, env не читается.
- `PostgreSQLConnector(database=..., host=..., ...)` → только args, env не читается.
- JSON **и** connection-args одновременно → ошибка `ConfigError`.

Параметры env-режима: `env_path=".env"`, имена переменных настраиваются:
`env_db_host="DB_HOST"`, `env_db_port="DB_PORT"`, `env_db_name="DB_NAME"`,
`env_db_user="DB_USER"`, `env_db_pass="DB_PASS"`.

Прочее: `unix_socket=...` (подключение через сокет), `use_id_as_uuid=False` —
влияет только на **создаваемые** таблицы: PK `UUID DEFAULT gen_random_uuid()` вместо `SERIAL`.
Поведенческие флаги (`use_id_as_uuid` и т.п.) источник конфига не переключают.

## 3. Подключение

```python
dbc.connect(
    connection_type="simple",      # simple | pool (psycopg_pool)
    use_prepared_statement=False,  # prepared statements psycopg3
    use_batching=False,            # pipeline mode
    use_binary=False,              # binary protocol
)
```

Коннектор — контекст-менеджер (`with`/`async with`), есть `close()`.

## 4. Интроспекция

`dbc.version()`, `dbc.databases()`, `dbc.tables()`, `dbc.views()`, `dbc.enums()`.

## 5. Query builder

- Фильтры: `equal / unequal / more / less / like / startswith / endswith / contains / overlaps / any / get`.
  - `%` и `_` в значениях `like/startswith/endswith` экранируются — матчатся литерально.
  - `contains`: скаляр — элемент в массиве; список — массив содержит ВСЕ элементы (`@>`). `overlaps` — хотя бы один общий (`&&`).
  - `more/less/like/...` с `None` — ошибка (используй `equal(col=None)`).
- `order_by("col", desc=False)` — без авто-DESC.
- Пагинация: `per_page(n).page(k)` (k ≥ 1, валидация).
- Агрегации: `count()`, `sum()` (не `summ`), `avg()`, `min()`, `max()`; `group_by(...)`.
  Без group_by выполняются сразу и возвращают число; алиасы аггрегатов уникализируются (`id_count`, `id_count_distinct`, `_2`...).
- `for item in db.users.get(...):` — итератор стримит server-side cursor'ом чанками; `.items` — забирает всё.
- Все идентификаторы — через `psycopg.sql.Identifier` (никаких f-строк с именами), значения — только параметрами.
- `to_csv(path)` — на билдере (учитывает фильтры/lang/columns).
- UPDATE/DELETE без фильтров — ошибка; явное «всё» — через `.all()`.
- Retry-политика: обрыв соединения ретраится с бэкоффом для чтений/UPDATE/DELETE
  (сама мутация идемпотентна: абсолютные SET, тот же WHERE; но RETURNING-результат
  после повтора может отличаться — например, пустой список, если первый прогон успел закоммититься);
  INSERT никогда не переисполняется (мог уже закоммититься) — честный `ConnectionFailed`.
- После `close()` операции кидают `ConnectionFailed` (никакого тихого переподключения); `connect()` оживляет.
- Откат батча (`pending().exec()`, `db.exec([...])`) сохраняет staged-состояние запросов — батч можно повторить.

### Кумулятивные операции

```python
db.users.add(...); db.users.add(...)         # копятся на коннекторе
db.pending(['add', 'delete', 'update', 'all']).exec()   # сброс одной транзакцией

db.exec([q1, q2, ...])                        # список запросов одной транзакцией
```

### Views

`query.as_view("active_users").save()` — создаёт VIEW из построенного запроса;
опция `materialized=True`.

## 6. Joins

Цепочка от таблицы; условие — строка; фильтры чужих таблиц через `таблица__колонка`:

```python
result = (db.users
    .join("orders", on="users.id = orders.user_id", type="left")   # inner|left|right|full|cross
    .join("products", on="orders.product_id = products.id")
    .columns("users.id", "users.name", "products.title")
    .equal(users__status="active")
    .more(orders__total=100)
    .group_by("users.id", "users.name")
    .count("orders.id")
    .order_by("users.name")
    .per_page(20).page(1)
    .exec())
```

## 7. Схема: md / json

### md-формат

```
langs: en, ru, zh, kr

enum user_status = active, banned, pending

users
- id serial primary
- status user_status default=active # текущий статус юзера
- username varchar(100) unique not_null
- bio text multilanguage # описание профиля
- manager_id int ->managers.id
```

- Описание после `#` одной строкой → хранится как `COMMENT ON COLUMN`,
  переживает round-trip: `from_md → база → export_as_md`, попадает в `export(json)` и `make_models`.
- FK: `->table[.column]` (без column — PK целевой таблицы).
- `enum name = v1, v2, ...` → `CREATE TYPE ... AS ENUM`. `ALTER TYPE ... ADD VALUE` в diff/migrate
  поддерживается; удаление значения enum в PG невозможно → diff помечает как «ручное решение».

### Мультиязычные колонки (суффиксы)

- `multilanguage` → физические колонки `bio_en, bio_ru, bio_zh, bio_kr` (по списку `langs`), базовой колонки нет.
- Первый язык в `langs` — дефолтный (цель fallback).
- `.lang('ru')` на запросе: SELECT возвращает `COALESCE(bio_ru, bio_en) AS bio`;
  фильтры/сортировка по `bio` бьют в `bio_ru`; `add`/`update` c `bio=` пишут в `bio_ru`.
  Без `.lang()` — работа с суффиксными колонками напрямую.
- `dbc.add_lang('kr')` — `ALTER TABLE ... ADD COLUMN IF NOT EXISTS <col>_kr` для всех ml-групп всех таблиц.
- Определение ml-групп: из схемы (md/json); на чужой базе — по конвенции имён против `langs`.

### Операции со схемой

- `dbc.export(type='json'|'sql')` — схема без данных.
- `dbc.export_as_md()` / `dbc.from_md(path)`.
- `dbc.init_db(json=... | dbc=... | md=...)` — развернуть схему (без данных).
- `dbc.diff(json=... | dbc=... | md=...)` — расхождения схем.
- `dbc.migrate(from_dbc=... | from_json=...)` — генерирует `migrations/<timestamp>.sql`
  (+ парный `.down.sql`, где реверс возможен). Применение: `dbc.apply_migration(path)`
  или `migrate(..., apply=True)`.

## 8. Backup / restore / clone

`dbc.backup(type='sql'|'binary'|'json', pg_dump_path=None)`:

- `pg_dump_path=None` → автопоиск бинаря (PATH, стандартные директории установки PG).
- `pg_dump_path=False` или бинарь не найден → только `sql`/`json` собственными силами (для `json` — warning, что это хуже pg_dump).
- `binary` — только через pg_dump.
- SQL-дампы пост-обрабатываются до **версионно-нейтральных**: вычищаются
  `-- Dumped from/by ... version` и версионно-специфичные `SET`-ы. Binary-формат
  версию содержит структурно — не трогаем (подтверждено: «если нельзя — окей»).

`dbc.restore(path)`, `dbc.clone(dbc=other)` — полное клонирование (схема + данные).

## 9. pgvector

- Тип `vector(n)` в md/json-схеме; при init — `CREATE EXTENSION IF NOT EXISTS vector`.
- Поиск: `db.docs.nearest(embedding=[...], metric="cosine"|"l2"|"ip", limit=10)`.
- Опциональная зависимость `pip install .[vector]` (пакет pgvector — адаптеры psycopg3).

## 10. serve_as_api

`dbc.serve_as_api(host, port, key)` — на обоих классах. FastAPI + uvicorn,
опциональная зависимость `pip install .[api]`. REST CRUD: `GET/POST/PATCH/DELETE /{table}`,
фильтры query-параметрами, auth: заголовок `X-API-Key`.

## 11. make_models

`dbc.make_models(path="models/", style='peewee'|'sqlalchemy'|'connector')` —
по одному файлу на стиль (`models/peewee_models.py`, ...). Кодогенерация текстом,
сами peewee/sqlalchemy для генерации не нужны.

## 12. Пакет

```
connector/
  __init__.py      # публичные экспорты
  errors.py        # иерархия исключений
  config.py        # env/json/args (взаимоисключающие)
  connection.py    # simple/pool, reconnect/backoff, pgvector-адаптеры
  core.py          # PostgreSQLConnector (sync)
  aio.py           # AsyncPostgreSQLConnector
  query.py         # builder, Row, View, итератор, pending, nearest (pgvector)
  join.py          # JoinQuery
  schema.py        # модель схемы, introspection, DDL, json
  markdown.py      # export_as_md / from_md
  migrate.py       # diff, генерация/применение миграций
  backup.py        # pg_dump/sql/json, restore, clone
  codegen.py       # make_models
  api.py           # serve_as_api
tests/
pyproject.toml
```

Зависимости: `psycopg[binary]>=3.2`, `psycopg_pool`, `python-dotenv`, `tabulate`.
Extras: `[api]` = fastapi + uvicorn; `[vector]` = pgvector.

## 13. Тесты

pytest на живом локальном Postgres (`127.0.0.1:5432`, docker отсутствует).
Отдельная база `connector_test`: создаётся/сносится в фикстурах. Нужны креды локального PG.

## 14. План фаз

1. **Каркас**: pyproject, скелет пакета, ruff, pytest + фикстуры тестовой БД.
2. **Ядро**: config, connect (simple/pool/flags), исключения, reconnect, интроспекция, Row, builder (фильтры/агрегации/итератор), pending, `db.exec([...])`.
3. **Схема**: внутренняя модель схемы, md/json parse+export, init_db, diff, migrate, enums, multilanguage (+`.lang()`, `add_lang`), uuid-PK, комментарии.
4. **Данные**: backup/restore/clone (pg_dump + нейтрализация версии), to_csv.
5. **Joins, as_view, pgvector.**
6. **Async-класс** (зеркало через общее ядро), make_models, serve_as_api.
7. **Докуметация**: README v2, примеры; финальное ревью.

## 15. Открыто

- Креды локального Postgres для тестовой БД.
- Публикация на PyPI — отложена (имя «connector» занято).
