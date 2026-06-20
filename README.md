# KISA app

Мінімальний Flask-застосунок для dashboard КІСА.

## Локальна перевірка

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python kisa.py
```

Відкриваємо:

```text
http://127.0.0.1:8000/
```

## У Docker Compose

Сервіс має слухати `0.0.0.0:8000`.

Рекомендовані env:

```env
KISA_INTERNAL_PORT=8000
KISA_BASE_URL=https://kisa.vps.me/app
KISA_GIT_REPO=
KISA_BUNDLE_DIR=/data/bundles
```

## Caddy

Якщо застосунок відкривається як `https://kisa.vps.me/app/`, краще використовувати:

```caddy
handle /app {
    redir /app/ 308
}

handle_path /app/* {
    reverse_proxy kisa:8000
}

handle /api/* {
    reverse_proxy kisa:8000
}

handle /bundles/* {
    reverse_proxy kisa:8000
}
```

`handle_path /app/*` обрізає `/app`, тому Flask бачить `/`.
Для `/api/*` і `/bundles/*` префікс не обрізаємо, бо маршрути в застосунку саме такі.
