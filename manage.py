"""
Управление коннектором турникетов из командной строки — чтобы не лезть в SQL руками.

Примеры:
    uv run manage.py controllers list
    uv run manage.py controllers add 222110572 192.168.140.13 --doors 1,2 --dir1 in --dir2 out
    uv run manage.py controllers disable 222110572
    uv run manage.py controllers rm 222110572
    uv run manage.py reconcile "C:\\N3000\\iCCard3000.mdb"     # добрать проходы из файла Download
    uv run manage.py sync 12345            # перезалить карту одного студента сейчас
    uv run manage.py sync all              # полная сверка карт
    uv run manage.py card 1234567          # кто это и что стоит на турникетах
    uv run manage.py status                # контроллеры и счётчики таблиц

Все команды берут подключение к базе из config.toml (как коннектор).
Контроллеры живут в таблице turnstile_controllers — add/rm/disable правят её.
"""
import argparse
import sys

import connector as C


def _db():
    return C.connect()


def _print_rows(rows, cols):
    widths = [max(len(c), *(len(str(r[c])) for r in rows)) if rows else len(c) for c in cols]
    line = "  ".join(c.ljust(w) for c, w in zip(cols, widths))
    print(line)
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths)))


# ------------------------------------------------------------- controllers
def cmd_controllers(a):
    with _db() as db, db.cursor() as cur:
        cur.execute("SHOW TABLES LIKE 'turnstile_controllers'")
        if not cur.fetchone():
            sys.exit("нет таблицы turnstile_controllers — прогоните turnstile_connector.sql")

        if a.action == "list":
            cur.execute("SELECT sn, ip, port, doors, dir1, dir2, active, note FROM turnstile_controllers ORDER BY sn")
            rows = cur.fetchall()
            if not rows:
                print("таблица пуста — коннектор возьмёт контроллеры из config.toml")
            else:
                _print_rows(rows, ["sn", "ip", "port", "doors", "dir1", "dir2", "active", "note"])

        elif a.action == "add":
            cur.execute("INSERT INTO turnstile_controllers (sn, ip, port, doors, dir1, dir2, note)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                        " ON DUPLICATE KEY UPDATE ip=VALUES(ip), port=VALUES(port), doors=VALUES(doors),"
                        " dir1=VALUES(dir1), dir2=VALUES(dir2), note=VALUES(note), active=1",
                        (a.sn, a.ip, a.port, a.doors, a.dir1, a.dir2, a.note))
            print(f"контроллер {a.sn} ({a.ip}) сохранён: двери {a.doors}, {a.dir1}/{a.dir2}")

        elif a.action in ("disable", "enable"):
            cur.execute("UPDATE turnstile_controllers SET active=%s WHERE sn=%s",
                        (1 if a.action == "enable" else 0, a.sn))
            print(f"контроллер {a.sn}: {'включён' if a.action == 'enable' else 'выключен'}"
                  f"{' (не найден)' if cur.rowcount == 0 else ''}")

        elif a.action == "rm":
            cur.execute("DELETE FROM turnstile_controllers WHERE sn=%s", (a.sn,))
            print(f"контроллер {a.sn} удалён" if cur.rowcount else f"контроллер {a.sn} не найден")


# ------------------------------------------------------------- reconcile
def cmd_reconcile(a):
    C.load_controllers()
    path = a.path or C.EVENTS.get("mdb_path", "")
    if not path:
        sys.exit("укажите путь к .mdb или задайте events.mdb_path в конфиге")
    added = C.reconcile_events_mdb(path)
    print(f"готово: добавлено {added} проходов")


# ------------------------------------------------------------- sync (через вебхук)
def cmd_sync(a):
    import json
    import urllib.request
    url = f"http://127.0.0.1:{C.HTTP_PORT}/sync"
    body = {"all": True} if a.who == "all" else {"student_ids": [int(a.who)]}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {C.HTTP_TOKEN}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            print("коннектор принял:", r.read().decode())
    except Exception as e:
        sys.exit(f"не удалось обратиться к вебхуку коннектора ({url}): {e}\n"
                 "Запущена ли служба? Синхронизация идёт через работающий коннектор.")


# ------------------------------------------------------------- card lookup
def cmd_card(a):
    with _db() as db, db.cursor() as cur:
        cur.execute(f"SELECT student_id, card_no, valid_from, valid_to FROM {C.VIEW} WHERE card_no=%s", (a.card,))
        v = cur.fetchone()
        cur.execute("SELECT student_id, valid_from, valid_to, synced_at FROM turnstile_synced WHERE card_no=%s", (a.card,))
        s = cur.fetchone()
        print(f"карта {a.card}")
        print("  в списке доступа (вьюха):", v if v else "нет")
        print("  на турникетах (synced): ", s if s else "нет")


# ------------------------------------------------------------- status
def cmd_status(a):
    C.load_controllers()
    with _db() as db, db.cursor() as cur:
        for t in ("turnstile_synced", "turnstile_events", "turnstile_errors"):
            try:
                cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
                print(f"{t}: {cur.fetchone()['n']} строк")
            except Exception:
                print(f"{t}: нет таблицы")
    print("контроллеры:")
    for c in C.controllers:
        alive = "отвечает" if c.alive() else "НЕ отвечает"
        print(f"  {c.sn} ({c.ip}) двери {c.door_mask} — {alive}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import os
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    p = argparse.ArgumentParser(description="Управление коннектором турникетов")
    p.add_argument("--config", default="config.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("controllers", help="контроллеры в базе")
    pcs = pc.add_subparsers(dest="action", required=True)
    pcs.add_parser("list")
    pa = pcs.add_parser("add")
    pa.add_argument("sn", type=int)
    pa.add_argument("ip")
    pa.add_argument("--doors", default="1,2")
    pa.add_argument("--dir1", default="in", choices=["in", "out"])
    pa.add_argument("--dir2", default="out", choices=["in", "out"])
    pa.add_argument("--port", type=int, default=60000)
    pa.add_argument("--note", default=None)
    for act in ("disable", "enable", "rm"):
        pp = pcs.add_parser(act)
        pp.add_argument("sn", type=int)

    pr = sub.add_parser("reconcile", help="добрать проходы из файла N3000 Download")
    pr.add_argument("path", nargs="?", default=None)

    ps = sub.add_parser("sync", help="синхронизировать карты (через вебхук работающего коннектора)")
    ps.add_argument("who", help="student_id или all")

    pcard = sub.add_parser("card", help="где карта: в доступе и на турникетах")
    pcard.add_argument("card", type=int)

    sub.add_parser("status", help="контроллеры и счётчики таблиц")

    a = p.parse_args()
    C.load_config(a.config)
    {"controllers": cmd_controllers, "reconcile": cmd_reconcile, "sync": cmd_sync,
     "card": cmd_card, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    main()
