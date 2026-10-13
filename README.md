# TurnstileConnector

Сервис между базой ISU (MariaDB 5.5) и турникетами (контроллеры WG3000 / ACB-001/002/004).
Изменилась строка под вьюхой студентов → через binlog коннектор узнаёт об этом сразу → обновляет карту на контроллерах.
Не получилось 5 раз → строка в `turnstile_errors`.

```
ISU (MariaDB) ──binlog──▶ connector.py ──UDP 60000──▶ контроллеры
                 POST /sync ──▶ (вебхук, если нужно дёрнуть вручную)
```

Файлы:

| Файл | Зачем |
|---|---|
| `connector.py` | сам коннектор |
| `config.test.toml` | конфиг для теста на своём ПК |
| `config.example.toml` | шаблон боевого конфига → копируется в `config.toml` |
| `fake_controller.py` | эмулятор контроллеров для теста без железа |
| `test_data.sql` | тестовая база: таблица студентов + вьюха + пользователь |
| `turnstile_connector.sql` | две служебные таблицы коннектора (и на тесте, и на бою) |
| `turnstile_view.sql` | вьюха `v_turnstile_xobgoh` поверх `view_xobgoh_resident` (бой) |
| `Dockerfile`, `docker-compose.yml` | запуск на Ubuntu Server в Docker |

---

# Часть 1. Тест на своём ПК

Цель: убедиться, что коннектор ловит изменения, шлёт правильные команды, повторяет и пишет ошибки. Без риска для настоящих турникетов.

## Шаг 1. Обновить проект

1. Замените файлы в папке проекта на эти. `main.py` удалите — он не нужен.
2. Поставьте зависимости (важно: `mysql-replication` версии **0.45.1**, не 1.x — новая не работает с MariaDB 5.5):
   ```
   uv sync
   ```
3. Проверьте:
   ```
   uv run python -c "import importlib.metadata as m; print(m.version('mysql-replication'))"
   ```
   Должно быть `0.45.1`.

## Шаг 2. Поднять тестовую MariaDB 5.5 с binlog

Тестируем на той же версии, что на бою. Проще всего через Docker Desktop:

```
docker run -d --name isu-test-db -p 3307:3306 -e MYSQL_ROOT_PASSWORD=root mariadb:5.5 --log-bin=mysql-bin --binlog-format=ROW --server-id=1
```

- порт **3307**, чтобы не конфликтовать с вашей локальной базой на 3306;
- `--log-bin --binlog-format=ROW --server-id=1` — это ровно то, что потом добавим в `my.ini` на сервере.

Подождите ~10 секунд и проверьте:
```
docker exec isu-test-db mysql -uroot -proot -e "SHOW MASTER STATUS"
```
Должна вернуться строка вида `mysql-bin.000001 | 245`.

> Если образ `mariadb:5.5` не скачивается — возьмите zip-архив MariaDB 5.5.68 для Windows с archive.mariadb.org,
> распакуйте и запустите `bin\mysqld --console --port=3307 --log-bin=mysql-bin --binlog-format=ROW --server-id=1`.

## Шаг 3. Залить тестовые данные

```
docker cp test_data.sql isu-test-db:/tmp/
docker cp turnstile_connector.sql isu-test-db:/tmp/
docker exec isu-test-db sh -c "mysql -uroot -proot --default-character-set=utf8 < /tmp/test_data.sql"
docker exec isu-test-db sh -c "mysql -uroot -proot --default-character-set=utf8 isu_test < /tmp/turnstile_connector.sql"
```

Получится база `isu_test`: 4 студента (2 активных с картой, 1 активный без карты, 1 отчисленный), вьюха `v_campus_students`, таблицы `turnstile_synced` и `turnstile_errors`, пользователь `turnstile/turnstile`.

## Шаг 4. Запустить эмулятор контроллеров

Откройте **отдельный** терминал:
```
uv run fake_controller.py
```
Он слушает UDP 60000 и печатает каждую команду: `+ карта …` (добавлена) и `− карта …` (удалена).
Если Windows спросит про брандмауэр — разрешите для частной сети.

## Шаг 5. Проверка без запуска

```
uv run connector.py config.test.toml --check
```
Ожидаемо:
```
INFO база: 5.5.xx-MariaDB-...
INFO вьюха v_campus_students: 3 строк, с картой 2
INFO таблица turnstile_synced: 0 строк
INFO таблица turnstile_errors: 0 строк
INFO binlog: mysql-bin.000001:..., формат ROW — ок
INFO контроллер 223218042 (127.0.0.1): отвечает, карт в памяти: 0
INFO контроллер 123228092 (127.0.0.1): отвечает, карт в памяти: 0
```
Если что-то не так — коннектор сам напишет, что именно (binlog выключен, нет прав, не та версия библиотеки, контроллер не отвечает).

## Шаг 6. Запуск

Третий терминал:
```
uv run connector.py config.test.toml
```
Сразу пройдёт **стартовая сверка**: две карты активных студентов уйдут на оба контроллера. В окне эмулятора появятся 4 строки `+ карта`.

## Шаг 7. Проверить, что изменения ловятся сразу

Выполняйте по одной команде и смотрите в окна коннектора и эмулятора — реакция должна быть в течение секунды:

```
docker exec isu-test-db mysql -uroot -proot isu_test -e "UPDATE students SET card_no=13158330 WHERE id=3"
```
→ студенту без карты выдали карту: `+ карта 13158330`

```
docker exec isu-test-db mysql -uroot -proot isu_test -e "UPDATE students SET status='expelled' WHERE id=1"
```
→ отчисление: `− карта 13158327`

```
docker exec isu-test-db mysql -uroot -proot isu_test -e "UPDATE students SET card_no=19999999 WHERE id=2"
```
→ перевыпуск: `− карта 20030837`, затем `+ карта 19999999`

```
docker exec isu-test-db mysql -uroot -proot isu_test -e "UPDATE students SET graduation_date='2031-06-01' WHERE id=3"
```
→ поменялся срок: `+ карта 13158330 … → 20310601`

```
docker exec isu-test-db mysql -uroot -proot isu_test -e "UPDATE students SET full_name='Другое имя' WHERE id=3"
```
→ **ничего** не должно уйти на контроллеры: имя на турникет не влияет.

Посмотреть, что коннектор считает записанным:
```
docker exec isu-test-db mysql -uroot -proot isu_test -e "SELECT * FROM turnstile_synced"
```

## Шаг 8. Проверить ошибки и повторы

1. Остановите эмулятор (Ctrl+C).
2. Сделайте изменение:
   ```
   docker exec isu-test-db mysql -uroot -proot isu_test -e "UPDATE students SET graduation_date='2032-06-01' WHERE id=3"
   ```
3. В логе коннектора: `попытка 1 … попытка 5` с паузами 2, 5, 15, 60 сек, затем `записано в turnstile_errors`.
4. Проверьте таблицу:
   ```
   docker exec isu-test-db mysql -uroot -proot isu_test -e "SELECT * FROM turnstile_errors"
   ```
5. Запустите эмулятор снова. Через минуту (в тестовом конфиге `after_fail = 60`) коннектор сам допишет карту, а у ошибки появится `resolved_at`.

> Эмулятор хранит карты в памяти, поэтому после перезапуска он «пустой». Настоящие контроллеры карты не теряют.

## Шаг 9. Проверить вебхук

В **cmd** (в PowerShell `curl` — это другая команда, и кавычки в JSON ломаются):
```
curl.exe http://localhost:8090/health
curl.exe -X POST http://localhost:8090/sync -H "Authorization: Bearer test-token" -d "{\"student_ids\":[2,3]}"
curl.exe -X POST http://localhost:8090/sync -H "Authorization: Bearer test-token" -d "{\"all\":true}"
```
Или в PowerShell:
```
Invoke-RestMethod http://localhost:8090/health
Invoke-RestMethod -Method Post http://localhost:8090/sync -Headers @{Authorization="Bearer test-token"} -Body '{"all":true,"force":true}'
```
`/health` показывает очередь и какие контроллеры отвечают. Без токена — `401`.

`{"all": true}` сверяет вьюху с `turnstile_synced` и отправляет только разницу.
`{"all": true, "force": true}` переписывает **все** карты на контроллерах заново — нужно, если контроллер заменили/сбросили или права перезалили из N3000.
Проверка: перезапустите эмулятор (он забудет карты) → обычный `all` ничего не отправит → `all` + `force` вернёт все карты.

## Шаг 10. Проверить на настоящей структуре ISU

До сих пор была упрощённая таблица `students`. Теперь — ваша реальная схема:

1. На сервере ISU снимите **только структуру** (без данных) и определение вьюхи:
   ```
   mysqldump -u root -p --no-data isu > isu_schema.sql
   mysql -u root -p isu -e "SHOW CREATE VIEW v_campus_students\G"
   ```
2. Создайте у себя новую базу из `isu_schema.sql`, добавьте несколько тестовых студентов руками.
3. Посмотрите, из каких таблиц собрана вьюха, и заполните секцию `[watch]` в конфиге:
   - если в таблице есть колонка со `student_id` — просто `таблица = "колонка"`;
   - если нет (например, таблица групп) — запрос: `groups = "SELECT id FROM students WHERE group_id = %(id)s"`.
4. Повторите шаги 5–8, меняя данные в **каждой** таблице из `[watch]`.

## Шаг 11 (по желанию). Одна настоящая карта на настоящем контроллере

Если ваш ПК видит сеть 192.168.140.x:

1. В тестовой базе оставьте **одного** студента — со своей картой (номер узнайте в N3000 или считывателем).
2. В `config.test.toml` поставьте настоящие IP контроллеров.
3. `--check` → контроллеры должны ответить и показать текущее число карт.
4. Запустите коннектор, приложите карту к турникету — должен пустить.
5. Отчислите студента в тестовой базе — карта должна перестать работать.

Чужие карты (сотрудников из N3000) коннектор не трогает: удаляет только то, что сам записал.

---

# Часть 2. Внедрение (Ubuntu Server + Docker)

## Сервер для коннектора

Коннектор лёгкий: ~50 МБ памяти, процессор почти не использует.

| | Минимум | Удобно |
|---|---|---|
| ОС | Ubuntu Server 22.04 / 24.04 LTS | |
| CPU | 1 vCPU | 1 vCPU |
| RAM | 512 МБ | 1 ГБ |
| Диск | 10 ГБ | 20 ГБ |

Главное — сеть:
- **статический IP** (по нему выдаются права в базе);
- доступ к серверу базы по **TCP 3306**;
- доступ к контроллерам **192.168.140.22 и .13 по UDP 60000**;
- порт 8090 снаружи открывать только если вебхук будут вызывать с других машин.

На сервере нужны Docker Engine и плагин compose (`docker compose version`).

## Шаг 1. Бэкап

```
mysqldump -u root -p --single-transaction --routines <база> > isu_backup_$(date +%F).sql
```
И копия `iCCard3000.mdb` из N3000.

## Шаг 2. Включить binlog на сервере базы

Сейчас `log_bin = OFF`, поэтому нужен **перезапуск MariaDB** (2–3 минуты простоя ISU).

В `my.cnf` (`/etc/mysql/my.cnf` на Linux, `my.ini` на Windows), секция `[mysqld]`:
```
server-id        = 1
log_bin          = mysql-bin
binlog_format    = ROW
expire_logs_days = 7
```
Проверьте там же `bind-address`: если `127.0.0.1`, база не примет подключение с сервера коннектора — нужно поставить IP сервера базы или `0.0.0.0` (с ограничением доступа брандмауэром).

Перезапуск и проверка:
```sql
SHOW VARIABLES LIKE 'log_bin';         -- ON
SHOW VARIABLES LIKE 'binlog_format';   -- ROW
SHOW MASTER STATUS;                    -- mysql-bin.000001 | ...
```
Убедитесь, что ISU работает.

## Шаг 3. Таблицы, вьюха, пользователь

```
mysql -u root -p <база> < turnstile_connector.sql
mysql -u root -p <база> < turnstile_view.sql
```
`turnstile_view.sql` создаёт `v_turnstile_xobgoh` поверх существующей `view_xobgoh_resident` (её не трогаем — она нужна для выгрузки в N3000). Даты берутся оттуда же, так что источник правды один.

Права (IP — сервера коннектора):
```sql
GRANT SELECT ON <база>.* TO 'turnstile'@'IP_КОННЕКТОРА' IDENTIFIED BY 'пароль';
GRANT INSERT, UPDATE, DELETE ON <база>.turnstile_synced TO 'turnstile'@'IP_КОННЕКТОРА';
GRANT INSERT, UPDATE ON <база>.turnstile_errors TO 'turnstile'@'IP_КОННЕКТОРА';
GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'turnstile'@'IP_КОННЕКТОРА';
FLUSH PRIVILEGES;
```

## Шаг 4. Развернуть коннектор

```
git clone <репозиторий> /opt/turnstile-connector     # или скопировать папку
cd /opt/turnstile-connector
cp config.example.toml config.toml
nano config.toml            # база, пароль, токен; dry_run = true
chmod 600 config.toml
docker compose build
```

## Шаг 5. Проверка

```
docker compose run --rm turnstile-connector python connector.py /app/config.toml --check
```
Ожидаемо: `вьюха v_turnstile_xobgoh: 281 строк, с картой 281`, `binlog … ROW — ок`, оба контроллера отвечают.

## Шаг 6. Пробный запуск (dry-run)

```
docker compose up -d
docker compose logs -f
```
С `dry_run = true` на контроллеры ничего не уходит, в логе:
```
студент 1234: [DRY-RUN] на турникетах: нет  →  должно быть: карта 13158327 (2026-10-01 — 2027-06-30)
```
Сверьте 5–10 жителей с N3000. Поменяйте что-нибудь у тестового жителя в ISU — строка про него должна появиться сразу.

## Шаг 7. Боевой запуск

```
nano config.toml            # dry_run = false
docker compose restart
docker compose logs -f
```
Стартовая сверка запишет 281 карту за несколько секунд. Проверьте своей картой и тестовой (удалите тестового жителя из списка — карта должна перестать работать, верните — снова работает).

Контейнер с `restart: unless-stopped` поднимается сам после перезагрузки сервера и при падении.

## Шаг 8. N3000

С этого момента **не нажимайте в N3000 загрузку прав на контроллеры** — она перезапишет их своим списком.
Если всё-таки нажали:
```
curl -X POST http://localhost:8090/sync -H "Authorization: Bearer <токен>" -d '{"all":true,"force":true}'
```
Обычный `{"all": true}` не поможет: он сверяет вьюху с `turnstile_synced`, а не с памятью контроллера.

## Шаг 9. Наблюдение

- Ошибки: `SELECT * FROM turnstile_errors WHERE resolved_at IS NULL`
- Состояние: `curl http://localhost:8090/health`, `docker ps` (колонка STATUS — healthy)
- Лог: `docker compose logs --since 1h`

Первую неделю смотрите `turnstile_errors` каждый день.

## Как коннектор видит изменения в `_____list_xobgoh`

- INSERT / UPDATE / DELETE — по каждой строке сразу;
- `TRUNCATE` (если список перезаливается целиком) — полная сверка через 5 сек после него;
- изменение вьюх (`CREATE OR REPLACE VIEW`, например новые даты на следующий год) — тоже полная сверка.

## Обновление

```
cd /opt/turnstile-connector && git pull && docker compose build && docker compose up -d
```

## Откат

`docker compose down` — и всё работает как раньше через N3000. Записанные карты остаются на контроллерах.
Binlog можно оставить включённым — ISU он не мешает.
