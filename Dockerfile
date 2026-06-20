FROM python:3.12-alpine

WORKDIR /app

# git для збірки бандлів, curl для HEALTHCHECK,
# safe.directory щоб git не скаржився на ownership у bind-mount
RUN apk add --no-cache git curl \
    && git config --system --add safe.directory '*'

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY kisa.py /app/kisa.py
COPY templates /app/templates
COPY static /app/static

ENV KISA_INTERNAL_PORT=8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${KISA_INTERNAL_PORT:-8000}/healthz || exit 1

# --workers 1 ОБОВ'ЯЗКОВО: STATE/PENDING живуть у пам'яті процесу.
# Багато worker = розкол стану і поламана ACK-логіка beacons.
# Паралелізм покривається --threads (GIL відпускається на I/O).
CMD ["sh", "-lc", "gunicorn --workers 1 --threads 4 -b 0.0.0.0:${KISA_INTERNAL_PORT:-8000} --access-logfile - kisa:app"]