"""
Единый сервис: FastAPI + Swagger + фоновый опрос контроллеров (один процесс, uvicorn 1 воркер).

Работает и на Windows (config.toml), и в Docker (переменные окружения) — конфиг через
connector.configure(). Запуск:
    uv run api.py                                   # читает config.toml если есть, иначе env
    uvicorn api:app --workers 1 --proxy-headers --forwarded-allow-ips "*" --root-path /turnstile

Фон (очередь карт, опрос проходов, ночная сверка) стартует в lifespan и штатно
останавливается при остановке. Эндпоинты записи защищены заголовком X-API-Key (env API_KEY)
плюс (на сервере) ограничением по IP в nginx. GET /health открыт и всегда отвечает 200.
Swagger — на <root_path>/docs.
"""
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

import connector as C

log = logging.getLogger("turnstile")
CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.toml")


@asynccontextmanager
async def lifespan(app: FastAPI):
    C.configure(CONFIG if os.path.exists(CONFIG) else None)   # TOML или env
    C.setup_logging()
    C.check_db()
    C.load_controllers()
    C.check_controllers()
    if C.DRY_RUN:
        log.warning("РЕЖИМ DRY-RUN: на контроллеры ничего не отправляется, только лог")
    C.start_workers(force=False)                               # фон: очередь карт, опрос проходов, ночная сверка
    try:
        yield
    finally:
        C.stop_workers()                                       # штатная остановка фона


app = FastAPI(title="Turnstile Connector API",
              description="Управление контроллерами, синхронизацией карт и сверкой проходов.",
              version="1.0", root_path=os.getenv("UVICORN_ROOT_PATH", ""), lifespan=lifespan)


def require_key(x_api_key: str = Header(None, description="Ключ для операций записи (env API_KEY)")):
    """Защита эндпоинтов записи. Пустой API_KEY в конфиге — запись запрещена целиком."""
    if not C.API_KEY:
        raise HTTPException(403, "запись отключена: API_KEY не задан")
    if x_api_key != C.API_KEY:
        raise HTTPException(401, "неверный X-API-Key")


def db():
    conn = C.connect()
    try:
        yield conn
    finally:
        conn.close()


# ------------------------------------------------------------------ модели
class ControllerIn(BaseModel):
    sn: int = Field(..., description="серийник, как в N3000")
    ip: str
    port: int = 60000
    doors: str = Field("1,2", description="какие двери открывать карте, напр. '1,2'")
    dir1: str = Field("in", pattern="^(in|out)$", description="направление двери 1")
    dir2: str = Field("out", pattern="^(in|out)$", description="направление двери 2")
    note: Optional[str] = None


class ControllerPatch(BaseModel):
    ip: Optional[str] = None
    port: Optional[int] = None
    doors: Optional[str] = None
    dir1: Optional[str] = Field(None, pattern="^(in|out)$")
    dir2: Optional[str] = Field(None, pattern="^(in|out)$")
    active: Optional[bool] = None
    note: Optional[str] = None


class SyncIn(BaseModel):
    student_ids: Optional[list[int]] = Field(None, description="кого перезалить; пусто + all=true — всех")
    all: bool = False
    force: bool = False


class ReconcileIn(BaseModel):
    path: Optional[str] = Field(None, description="путь к .mdb (N3000 Download); пусто — mdb_path из конфига")


# ---------------------------------------------------------------- здоровье
@app.get("/health", tags=["Статус"])
def health():
    """Лёгкая проверка: всегда 200, состояние зависимостей — в теле (Docker не перезапускает из-за сети)."""
    db_ok = False
    try:
        with C.connect() as c, c.cursor() as cur:
            cur.execute("SELECT 1")
            db_ok = True
    except Exception:
        pass
    return {"status": "ok", "db": db_ok,
            "controllers": [{"sn": c.sn, "ip": c.ip, "alive": c.alive()} for c in C.controllers]}


# ------------------------------------------------------------ контроллеры
@app.get("/controllers", tags=["Контроллеры"], dependencies=[Depends(require_key)])
def list_controllers(conn=Depends(db)):
    with conn.cursor() as cur:
        cur.execute("SELECT sn, ip, port, doors, dir1, dir2, active, note FROM turnstile_controllers ORDER BY sn")
        return cur.fetchall()


@app.post("/controllers", tags=["Контроллеры"], dependencies=[Depends(require_key)])
def upsert_controller(c: ControllerIn, conn=Depends(db)):
    """Добавить/обновить контроллер (по sn). Применится после перезапуска сервиса."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO turnstile_controllers (sn, ip, port, doors, dir1, dir2, note)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s)"
                    " ON DUPLICATE KEY UPDATE ip=VALUES(ip), port=VALUES(port), doors=VALUES(doors),"
                    " dir1=VALUES(dir1), dir2=VALUES(dir2), note=VALUES(note), active=1",
                    (c.sn, c.ip, c.port, c.doors, c.dir1, c.dir2, c.note))
    return {"ok": True, "sn": c.sn}


@app.patch("/controllers/{sn}", tags=["Контроллеры"], dependencies=[Depends(require_key)])
def patch_controller(sn: int, p: ControllerPatch, conn=Depends(db)):
    """Изменить поля (в т.ч. active=false — выключить). Применится после перезапуска сервиса."""
    fields = {k: (int(v) if k == "active" else v) for k, v in p.dict().items() if v is not None}
    if not fields:
        raise HTTPException(400, "нет полей для изменения")
    sets = ", ".join(f"{k}=%s" for k in fields)
    with conn.cursor() as cur:
        n = cur.execute(f"UPDATE turnstile_controllers SET {sets} WHERE sn=%s", (*fields.values(), sn))
    if not n:
        raise HTTPException(404, f"контроллер {sn} не найден")
    return {"ok": True, "sn": sn, "updated": list(fields)}


@app.delete("/controllers/{sn}", tags=["Контроллеры"], dependencies=[Depends(require_key)])
def delete_controller(sn: int, conn=Depends(db)):
    with conn.cursor() as cur:
        n = cur.execute("DELETE FROM turnstile_controllers WHERE sn=%s", (sn,))
    if not n:
        raise HTTPException(404, f"контроллер {sn} не найден")
    return {"ok": True, "sn": sn}


# ------------------------------------------------------------------ карты
@app.post("/sync", tags=["Карты"], dependencies=[Depends(require_key)])
def sync(body: SyncIn):
    """Поставить карты в очередь на синхронизацию (фон обработает). Это и есть вебхук ISU."""
    if body.all:
        try:
            n = C.enqueue_all(force=body.force)
        except C.DB_ERRORS as e:
            raise HTTPException(503, f"база недоступна: {e}")
        return {"queued": n}
    if not body.student_ids:
        raise HTTPException(400, "укажите student_ids или all=true")
    for sid in body.student_ids:
        C.queue.add(int(sid), force=body.force)
    return {"queued": len(body.student_ids)}


@app.get("/card/{card_no}", tags=["Карты"], dependencies=[Depends(require_key)])
def card(card_no: int, conn=Depends(db)):
    with conn.cursor() as cur:
        cur.execute(f"SELECT student_id, card_no, valid_from, valid_to FROM {C.VIEW} WHERE card_no=%s", (card_no,))
        in_view = cur.fetchone()
        cur.execute("SELECT student_id, valid_from, valid_to, synced_at FROM turnstile_synced WHERE card_no=%s", (card_no,))
        on_turnstiles = cur.fetchone()
    return {"card_no": card_no, "в_доступе": in_view, "на_турникетах": on_turnstiles}


# --------------------------------------------------------------- проходы
@app.post("/reconcile", tags=["Проходы"], dependencies=[Depends(require_key)])
def reconcile(body: ReconcileIn):
    """Добрать проходы из файла N3000 Download (.mdb) в журнал."""
    path = body.path or C.EVENTS.get("mdb_path", "")
    if not path:
        raise HTTPException(400, "укажите path или задайте mdb_path в конфиге")
    try:
        added = C.reconcile_events_mdb(path)
    except Exception as e:
        raise HTTPException(500, f"сверка не выполнена: {e}")
    return {"ok": True, "добавлено": added}


@app.get("/passes", tags=["Проходы"], dependencies=[Depends(require_key)])
def passes(limit: int = 50, conn=Depends(db)):
    """Последние проходы (из вьюхи v_turnstile_passes)."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM v_turnstile_passes ORDER BY event_time DESC LIMIT %s", (min(limit, 500),))
        return cur.fetchall()


# ---------------------------------------------------------------- статус
@app.get("/status", tags=["Статус"], dependencies=[Depends(require_key)])
def status(conn=Depends(db)):
    counts = {}
    with conn.cursor() as cur:
        for t in ("turnstile_synced", "turnstile_events", "turnstile_errors"):
            try:
                cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
                counts[t] = cur.fetchone()["n"]
            except Exception:
                counts[t] = None
    controllers = [{"sn": c.sn, "ip": c.ip, "doors": c.door_mask, "alive": c.alive()} for c in C.controllers]
    return {"dry_run": C.DRY_RUN, "events": bool(C.EVENTS), "таблицы": counts, "контроллеры": controllers}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("API_BIND", "127.0.0.1"), port=int(os.getenv("API_PORT", "8000")),
                proxy_headers=True, forwarded_allow_ips="*")
