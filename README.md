# TurnstileConnector

Служба Windows между базой ISU (MariaDB 5.5) и турникетами (контроллеры WG3000 / ACB-001/002/004):

- **карты:** ISU изменила список жильцов → вызывает вебхук → коннектор сразу обновляет карты на контроллерах;
- **проходы:** контроллер сообщает о проходе → коннектор сразу пишет его в базу.

Нет изменений — нет вызовов, коннектор базу не опрашивает. binlog, триггеры и перезапуск базы не нужны.

```
ISU ── POST /sync {"student_ids":[…]} ──▶ коннектор ── UDP 60000 ──▶ контроллеры
                                              ▲                          │
                          turnstile_events ◀──┴──── push UDP 60001 ◀─────┘  (проход)
```

Страховка от пропущенных вызовов: при каждом запуске и (по желанию) раз в сутки ночью коннектор сверяет
весь список с тем, что записано на турникетах, и догоняет разницу.

| Файл | Зачем |
|---|---|
| `connector.py` | сам коннектор |
| `php/turnstile_notify.php` | функция для ISU: вызвать вебхук после изменения списка |
| `install_service.ps1`, `uninstall_service.ps1` | установка/удаление службы Windows и правил брандмауэра |
| `config.example.toml` | шаблон боевого конфига → копируется в `config.toml` |
| `config.test.toml` | конфиг для теста на своём ПК |
| `fake_controller.py` | эмулятор контроллеров для теста без железа |
| `test_data.sql` | тестовая база: таблица студентов + вьюха + пользователь |
| `turnstile_connector.sql` | служебные таблицы коннектора (можно выполнять повторно) |
| `turnstile_view.sql` | вьюхи для боя: `v_turnstile_xobgoh` (кому доступ) и `v_turnstile_passes` (отчёт по проходам) |

---

# Часть 1. Тест на своём ПК

## Шаг 1. Зависимости

```
uv sync
```
Нужен только `pymysql`. Если раньше стояли `mysql-replication` и `setuptools` — `uv sync` их уберёт.

## Шаг 2. Тестовая база

Тестовая MariaDB 5.5 в Docker, binlog не нужен:
```
docker run -d --name isu-test-db -p 3307:3306 -e MYSQL_ROOT_PASSWORD=root mariadb:5.5
```
Если контейнер уже есть с прошлых тестов — `docker start isu-test-db` (флаги binlog в нём не мешают).

Залить данные (пересоздаёт тестовую базу с нуля):
```
docker exec isu-test-db mysql -uroot -proot -e "DROP DATABASE IF EXISTS isu_test"
docker cp test_data.sql isu-test-db:/tmp/
docker cp turnstile_connector.sql isu-test-db:/tmp/
docker exec isu-test-db sh -c "mysql -uroot -proot --default-character-set=utf8 < /tmp/test_data.sql"
docker exec isu-test-db sh -c "mysql -uroot -proot --default-character-set=utf8 isu_test < /tmp/turnstile_connector.sql"
```

## Шаг 3. Три окна PowerShell в папке проекта

**Эмулятор:**
```
uv run fake_controller.py --serials 223218042,123228092
```
**Коннектор:**
```
uv run connector.py config.test.toml
```
Ожидается: `стартовая сверка: 3 студентов`, `вебхук: http://127.0.0.1:8090/sync`, в эмуляторе 4 строки `+ карта`.

**Команды** — объявить две функции (живут, пока окно открыто):
```
function SQL($q) { docker exec isu-test-db mysql -uroot -proot --default-character-set=utf8 isu_test -e $q }
function NOTIFY($body) { Invoke-RestMethod -Method Post http://127.0.0.1:8090/sync -Headers @{Authorization="Bearer test-token-1234567890"} -Body $body }
```
`NOTIFY` делает ровно то, что потом будет делать ISU.

## Шаг 4. Сценарии

| Действие | Ожидается |
|---|---|
| `SQL "UPDATE students SET card_no=13158330 WHERE id=3"` | **ничего** — вебхук не вызывали |
| `NOTIFY '{"student_ids":[3]}'` | `queued: 1`, в эмуляторе `+ карта 13158330` сразу |
| `SQL "UPDATE students SET status='expelled' WHERE id=1"`, затем `NOTIFY '{"student_ids":[1]}'` | `− карта 13158327` |
| `SQL "UPDATE students SET card_no=19999999 WHERE id=2"`, затем `NOTIFY '{"student_ids":[2]}'` | сначала `+ 19999999`, потом `− 20030837` |
| `NOTIFY '{"all":true}'` | полная сверка; если всё совпадает — в эмуляторе ничего |
| `NOTIFY '{"all":true,"force":true}'` | все карты переписываются заново |
| без заголовка `Authorization` | `401` |

**Коннектор был выключен.** Ctrl+C в окне коннектора → изменить что-нибудь в `SQL` → `NOTIFY` (ошибка: коннектор недоступен) →
запустить коннектор. Стартовая сверка сама допишет изменение.

**Ночная сверка.** В `config.test.toml` поставить `reconcile_at` на ближайшую минуту (например, `"14:05"`), перезапустить коннектор,
изменить данные через `SQL` **без** `NOTIFY`. В указанную минуту в логе `ночная сверка: …` и изменение уходит на эмулятор.

**Ошибки и повторы.** Остановить эмулятор → изменение + `NOTIFY` → в логе `попытка 1…5` (паузы 2, 5, 15, 60 сек),
затем `записано в turnstile_errors`. Запустить эмулятор → через минуту карта уходит, у ошибки появляется `resolved_at`.

## Шаг 5. Вызов из PHP (как будет в ISU)

```
php -r "define('TURNSTILE_URL','http://127.0.0.1:8090/sync'); define('TURNSTILE_TOKEN','test-token-1234567890'); require 'php/turnstile_notify.php'; var_dump(turnstile_notify(array(3)));"
```
Ожидается `bool(true)`. Остановите коннектор и повторите — `bool(false)` за доли секунды и строка `turnstile_notify: …` в логе ошибок PHP.

## Шаг 6. Проходы

Пока эмулятор и коннектор работают:
```
uv run fake_controller.py swipe 13158330
SQL "SELECT event_time, card_no, student_id, direction, granted FROM turnstile_events"
```
Подробно — часть 4.

---

# Часть 2. Внедрение (Windows)

## Компьютер для коннектора

Любой Windows-компьютер или сервер, который **постоянно включён** и:
- видит сервер базы (TCP 3306);
- видит контроллеры 192.168.140.22 и .13 (UDP 60000), и они видят его (UDP 60001 — для проходов);
- доступен с веб-сервера ISU (TCP 8090 — вебхук);
- имеет **статический IP**: на него указывают ISU (`TURNSTILE_URL`) и контроллеры (push).

Ресурсы минимальные: служба занимает ~50 МБ памяти и почти не использует процессор.

## Шаг 1. Бэкап

```
mysqldump -u root -p --single-transaction --routines <база> > isu_backup.sql
```
И копия `iCCard3000.mdb` из N3000.

## Шаг 2. Таблицы, вьюхи, пользователь

В базе ничего не перезапускается и настройки сервера не меняются — только новые таблицы и вьюхи:
```
mysql -u root -p <база> < turnstile_connector.sql
mysql -u root -p <база> < turnstile_view.sql
```
Отдельный пользователь с минимальными правами (IP — компьютера коннектора):
```sql
GRANT SELECT ON <база>.* TO 'turnstile'@'IP_КОННЕКТОРА' IDENTIFIED BY 'пароль';
GRANT INSERT, UPDATE, DELETE ON <база>.turnstile_synced TO 'turnstile'@'IP_КОННЕКТОРА';
GRANT INSERT, UPDATE ON <база>.turnstile_errors TO 'turnstile'@'IP_КОННЕКТОРА';
GRANT INSERT ON <база>.turnstile_events TO 'turnstile'@'IP_КОННЕКТОРА';
GRANT INSERT, UPDATE ON <база>.turnstile_event_cursor TO 'turnstile'@'IP_КОННЕКТОРА';
FLUSH PRIVILEGES;
```

## Шаг 3. Проект и конфиг

1. Скопировать папку проекта, например в `C:\TurnstileConnector`.
2. Установить uv: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`.
3. Скачать NSSM (nssm.cc), положить `win64\nssm.exe` в `C:\TurnstileConnector\tools\nssm.exe`.
4. `copy config.example.toml config.toml` и заполнить: база, токен вебхука, IP контроллеров. **`dry_run = true`**.

## Шаг 4. Проверка и пробный запуск

```
uv run connector.py config.toml --check
uv run connector.py config.toml --selftest 99999999
uv run connector.py config.toml
```
- `--check`: вьюха (`281 строк, с картой 281`), служебные таблицы, оба контроллера отвечают.
- `--selftest` с номером карты, которой нет ни у кого: по обоим контроллерам `— ок`.
- запуск с `dry_run = true`: 281 строка `[DRY-RUN] …`, на контроллеры ничего не уходит. Сверить 5–10 жителей с N3000. Ctrl+C.

## Шаг 5. Служба

`dry_run = false` в `config.toml`. PowerShell **от имени администратора**:
```
cd C:\TurnstileConnector
.\install_service.ps1 -IsuServerIp <IP веб-сервера ISU>
```
Скрипт: ставит Python в папку проекта (служба не зависит от профиля пользователя), запускает `--check`,
создаёт службу `TurnstileConnector` (автозапуск, перезапуск при сбое), открывает в брандмауэре вебхук
**только** для сервера ISU и push **только** из сети контроллеров, запускает службу.

Лог: `C:\TurnstileConnector\logs\connector.log` (ротация по 10 МБ, 10 файлов).
Управление: `Get-Service TurnstileConnector`, `Restart-Service TurnstileConnector`.

## Шаг 6. Подключить ISU

Часть 3. Проверка: заселить/выселить тестового жителя в ISU — карта у турникета начинает/перестаёт работать сразу.

## N3000

С этого момента **не нажимайте в N3000 загрузку прав на контроллеры** — она перезапишет их своим списком.
Если нажали — `{"all": true, "force": true}` в вебхук или перезапуск службы с `--force`.

## Наблюдение

- Ошибки: `SELECT * FROM turnstile_errors WHERE resolved_at IS NULL`
- Состояние: `Invoke-RestMethod http://localhost:8090/health`
- Лог: `logs\connector.log`

## Обновление и откат

Обновление: заменить файлы → `Restart-Service TurnstileConnector` (если менялись зависимости — сначала `uv sync`).
Откат: `.\uninstall_service.ps1` — всё снова работает через N3000, записанные карты остаются на контроллерах.

---

# Часть 3. Подключение ISU (вебхук)

`php/turnstile_notify.php` — две функции, совместимы с PHP 5.3+:

```php
require_once 'turnstile_notify.php';      // в начале — задать TURNSTILE_URL и TURNSTILE_TOKEN (см. файл)

turnstile_notify(array($studentId));      // изменились эти жильцы
turnstile_notify($studentIds);            // приказ по нескольким
turnstile_notify_all();                   // список перезалит целиком
```

Правила:
- **Вызывать после успешного сохранения** (после `COMMIT`), не внутри транзакции: коннектор сразу читает базу,
  и незакоммиченное изменение он не увидит.
- **Во всех местах**, где меняется `_____list_xobgoh` или поля, влияющие на вьюху: заселение, выселение,
  выдача и перевыпуск карты, приказы. Если таблица перезаливается целиком — `turnstile_notify_all()`.
- Функция **никогда не ломает ISU**: таймаут 2 секунды, при ошибке — `false` и строка в `error_log`.
  Пропущенное коннектор догонит при запуске и ночной сверкой.
- Токен хранить в конфиге ISU, а не в коде, который попадает в git.

Что именно отправляется: `POST http://<коннектор>:8090/sync`, заголовок `Authorization: Bearer <token>`,
тело `{"student_ids":[101,102]}` или `{"all":true}`. Ответ `202 {"queued": N}` — задачи приняты в очередь.

---

# Часть 4. Проходы: турникеты → база

## Как это работает

- Каждое событие в контроллере имеет номер: 1, 2, 3… Коннектор помнит, до какого номера прочитал
  (`turnstile_event_cursor`), и всегда дочитывает всё после него.
- **Push** — контроллер сам сообщает о проходе, задержка — доли секунды. Это только «звонок»: запись всегда идёт через
  чтение по номеру, поэтому потерянный UDP-пакет ничего не теряет — событие дочитается при следующем проходе или запуске.
- Нет проходов — нет пакетов, коннектор ничего не делает (`poll_seconds = 0`). Сам он обращается к контроллерам
  только раз в 5 минут (проверить адрес push) и раз в сутки (выставить часы).
- Коннектор был выключен или недоступна база — после восстановления дочитывает пропущенное.
- Повтор одного и того же события не запишется (уникальный ключ: контроллер + номер + время).
- Память контроллера очистили или контроллер заменили — коннектор это замечает и читает заново с начала.
- `student_id` — по номеру карты на момент прохода; карты нет в списке — `NULL`, отказ тоже записывается.

Для отчётов — вьюха `v_turnstile_passes`: время, житель, ФИО, группа, вход/выход, пропущен ли.

## Настройка (`[events]`)

| Параметр | Что значит |
|---|---|
| `enabled` | включить чтение проходов |
| `listen_port` | UDP-порт для push; `0` — без push |
| `listener_address` | IP компьютера коннектора, как его видят контроллеры; коннектор сам пропишет его в контроллеры. `""` — не трогать настройку контроллеров |
| `poll_seconds` | `0` — только по push. Если push невозможен (`listen_port = 0`) — `1`: опрос контроллеров раз в секунду |
| `history_on_first_start` | `false` — старую историю из памяти контроллера не читать (она уже в базе через N3000) |
| `sync_time_hours` | как часто выставлять часы контроллеров; `0` — не трогать |

## Внедрение

1. Права на `turnstile_events` и `turnstile_event_cursor` — уже в шаге 2 части 2.
2. `enabled = true`, `listener_address` = IP компьютера коннектора. `Restart-Service TurnstileConnector`.
3. `--check` по каждому контроллеру показывает `push → <IP>:60001` и расхождение часов в пределах пары секунд.
4. Пройти тестовой картой туда и обратно — две строки в `v_turnstile_passes`. Карта не из списка — строка с отказом.
5. **N3000:** убедиться, что мониторинг в реальном времени по-прежнему показывает проходы. У контроллера один адрес
   для push, и коннектор его занимает. Если мониторинг пропал — `listener_address = ""`, `listen_port = 0`,
   `poll_seconds = 1` (опрос раз в секунду, настройки контроллеров не трогаются).
