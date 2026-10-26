# TurnstileConnector — образ для внутреннего сервера (Ubuntu 24.04, Docker 29).
# Базовый образ тянем через зеркало mirror.gcr.io (на сервере только оно доступно).
# Multi-stage: сборка зависимостей отдельно, в финальный образ не тащим кэш pip и компиляторы.

# ---------- этап сборки: ставим зависимости в отдельный префикс ----------
FROM mirror.gcr.io/library/python:3.12-slim AS build

WORKDIR /app
COPY requirements.txt .
# --prefix=/install — складываем пакеты в отдельную папку, чтобы скопировать их одним слоем
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------- финальный этап: только рантайм ----------
FROM mirror.gcr.io/library/python:3.12-slim

# Время — Душанбе (чтобы ночная сверка и метки времени совпадали с локальным)
ENV TZ=Asia/Dushanbe
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# Не буферизовать stdout/stderr — логи сразу видны в docker logs
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# Непривилегированный пользователь
RUN useradd --create-home --uid 10001 app

# Зависимости из этапа сборки
COPY --from=build /install /usr/local

WORKDIR /app
COPY --chown=app:app . .

USER app
EXPOSE 8000

# Контейнер сам сообщает о своём состоянии; /health всегда отвечает 200, детали — в теле.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3).status==200 else 1)"

# Один воркер: фон (опрос контроллеров, очередь карт) живёт внутри процесса — несколько воркеров
# опрашивали бы контроллеры параллельно. proxy-headers + forwarded-allow-ips — т.к. за nginx.
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
