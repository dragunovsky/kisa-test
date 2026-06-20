"""КІСА - мінімальний робочий застосунок.

Приймає beacons від наземок, показує стан парку, дозволяє запланувати
оновлення ОДЕСА через beacon-driven модель, якщо налаштований git-репозиторій.

Об'єднана версія з двох попередніх варіантів:
  - env-конфіг з /opt/kisa/.env;
  - optional yaml-конфіг через KISA_CONFIG для сумісності зі старим PoC;
  - ProxyFix для роботи за Caddy;
  - demo seed/clear для швидкої перевірки dashboard;
  - atomic build bundle, щоб не віддати битий архів під час збірки.

Основні env:
  KISA_BASE_URL      напр. https://kisa.vps.me/app
                     використовується для bundle_url у pending-командах
  KISA_GIT_REPO      шлях до локального clone ОДЕСА; порожньо = deploy вимкнено
  KISA_BUNDLE_DIR    де складати зібрані бандли, default /data/bundles
  KISA_INTERNAL_PORT порт dev-сервера, default 8000
  KISA_CONFIG        optional yaml-файл зі старої версії
"""

from __future__ import annotations

import logging
import os
import pathlib
import subprocess
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=os.environ.get("KISA_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("kisa")


# ============================================================
# Configuration
# ============================================================

def _load_yaml_config() -> Dict[str, Any]:
    """Optional compatibility layer for the old PoC config.

    Якщо KISA_CONFIG не заданий або файл відсутній - повертаємо порожній dict.
    Якщо PyYAML не встановлений - не падаємо, а логуюємо warning.
    """
    cfg_path = os.environ.get("KISA_CONFIG", "").strip()
    if not cfg_path:
        return {}

    path = pathlib.Path(cfg_path)
    if not path.exists():
        log.warning("KISA_CONFIG is set but file does not exist: %s", path)
        return {}

    try:
        import yaml  # type: ignore
    except Exception:
        log.warning("KISA_CONFIG is set, but PyYAML is not installed. Ignoring yaml config.")
        return {}

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            log.warning("KISA_CONFIG must contain yaml mapping. Ignoring: %s", path)
            return {}
        return data
    except Exception:
        log.exception("failed to read KISA_CONFIG: %s", path)
        return {}


_YAML_CFG = _load_yaml_config()


def _cfg(name: str, default: str = "", yaml_key: Optional[str] = None) -> str:
    """Env має пріоритет над yaml, yaml потрібен тільки для сумісності."""
    val = os.environ.get(name)
    if val is not None:
        return val
    if yaml_key and yaml_key in _YAML_CFG:
        return str(_YAML_CFG[yaml_key])
    return default

def _ensure_repo() -> str:
    """Дозволяє KISA_GIT_REPO бути або шляхом, або URL.
    Якщо URL - клонує у KISA_REPO_DIR (default /data/odesa-repo).
    Повертає локальний шлях або порожній рядок, якщо налаштовано некоректно.
    """
    val = _cfg("KISA_GIT_REPO", "", "git_repo").strip()
    if not val:
        return ""

    is_url = "://" in val or val.startswith("git@")
    if not is_url:
        return val if pathlib.Path(val).exists() else ""

    clone_dir = pathlib.Path(_cfg("KISA_REPO_DIR", "/data/odesa-repo"))
    if (clone_dir / ".git").exists():
        log.info("using existing clone: %s", clone_dir)
        return str(clone_dir)

    log.info("cloning %s -> %s", val, clone_dir)
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "clone", val, str(clone_dir)],
            check=True, timeout=600,
        )
    except Exception:
        log.exception("git clone failed; deploy will stay disabled")
        return ""
    return str(clone_dir)

BASE_URL = _cfg("KISA_BASE_URL", "", "base_url").rstrip("/")
GIT_REPO = _ensure_repo()
BUNDLE_DIR = pathlib.Path(_cfg("KISA_BUNDLE_DIR", "/data/bundles", "bundle_dir"))
INTERNAL_PORT = int(_cfg("KISA_INTERNAL_PORT", "8000"))
ONLINE_S = int(_cfg("KISA_ONLINE_S", "60"))
STALE_S = int(_cfg("KISA_STALE_S", "300"))
GIT_FETCH_INTERVAL_S = int(
    _cfg("KISA_GIT_FETCH_INTERVAL_S", "3600")
)

BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
BUNDLE_PATHS = [
    item.strip()
    for item in _cfg(
        "KISA_BUNDLE_PATHS",
        "odesa,requirements.txt,run.py",
    ).split(",")
    if item.strip()
]

def _deploy_enabled() -> bool:
    return bool(GIT_REPO) and pathlib.Path(GIT_REPO).exists()


# ============================================================
# Flask app
# ============================================================

app = Flask(__name__)

# За Caddy: підставляє реальний IP клієнта з X-Forwarded-For,
# схему з X-Forwarded-Proto. Caddy довірений як єдиний proxy перед нами.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# In-memory state. Beacons відновлюють картину після рестарту.
STATE: Dict[str, Dict[str, Any]] = {}
PENDING: Dict[str, Dict[str, Any]] = {}
LOCK = threading.Lock()
BUILD_LOCK = threading.Lock()


# ============================================================
# Helpers
# ============================================================

def _now() -> float:
    return time.time()


def _short(value: Optional[str], n: int = 10) -> str:
    if not value:
        return "-"
    return str(value)[:n]


def _safe_commit(commit: str) -> bool:
    return (
        bool(commit)
        and 7 <= len(commit) <= 64
        and all(c in "0123456789abcdefABCDEF" for c in commit)
    )


def _drones_view() -> List[Dict[str, Any]]:
    now = _now()
    out: List[Dict[str, Any]] = []

    with LOCK:
        for did, st in sorted(STATE.items()):
            last_ts = float(st.get("last_beacon_ts", 0))
            age = max(0, now - last_ts)
            status = "online" if age < ONLINE_S else "stale" if age < STALE_S else "offline"

            out.append(
                {
                    **st,
                    "drone_id": did,
                    "age_s": int(age),
                    "status": status,
                    "pending": PENDING.get(did),
                }
            )

    return out


def _counts(drones: List[Dict[str, Any]]) -> Dict[str, int]:
    return {
        "total": len(drones),
        "online": sum(1 for d in drones if d.get("status") == "online"),
        "stale": sum(1 for d in drones if d.get("status") == "stale"),
        "offline": sum(1 for d in drones if d.get("status") == "offline"),
        "pending": sum(1 for d in drones if d.get("pending")),
    }


# ============================================================
# API: beacon
# ============================================================

@app.post("/api/beacon")
def beacon():
    data = request.get_json(force=True, silent=True) or {}
    did = data.get("drone_id")

    if not did:
        return jsonify({"error": "no drone_id"}), 400

    with LOCK:
        STATE[did] = {
            **data,
            "last_beacon_ts": _now(),
            "ip": request.remote_addr,
        }

        # ACK виконаної команди.
        if data.get("last_command_id") and did in PENDING:
            if PENDING[did]["id"] == data["last_command_id"]:
                log.info("ACK %s cmd %s", did[:8], data["last_command_id"][:8])
                del PENDING[did]

        pend = PENDING.get(did)

    return jsonify({"pending": pend})


@app.get("/healthz")
def healthz():
    with LOCK:
        return jsonify(
            {
                "ok": True,
                "drones": len(STATE),
                "pending": len(PENDING),
                "deploy_enabled": _deploy_enabled(),
            }
        )


@app.get("/api/state")
def api_state():
    return jsonify(_drones_view())


@app.get("/api/version")
def api_version():
    return jsonify(
        {
            "name": "kisa",
            "mode": "minimal",
            "deploy_enabled": _deploy_enabled(),
            "base_url": BASE_URL,
            "bundle_dir": str(BUNDLE_DIR),
        }
    )


# ============================================================
# Dashboard
# ============================================================

@app.get("/")
def dashboard():
    drones = _drones_view()
    commits = _list_commits() if _deploy_enabled() else []
    return render_template(
        "dashboard.html",
        drones=drones,
        commits=commits,
        deploy_enabled=_deploy_enabled(),
        counts=_counts(drones),
        base_url=BASE_URL,
        git_repo=GIT_REPO,
    )


# ============================================================
# Demo: інжект тестових наземок
# ============================================================

@app.post("/api/demo/seed")
def demo_seed():
    samples = [
        ("a1b2c3d4e5f6", {"ok": 14, "warn": 0, "fail": 0, "skip": 2}, 86_400),
        ("a1b2c3d4e5f6", {"ok": 13, "warn": 1, "fail": 0, "skip": 2}, 3_600),
        ("f0e1d2c3b4a5", {"ok": 12, "warn": 0, "fail": 1, "skip": 3}, 600),
    ]

    with LOCK:
        for i, (commit, hc, up) in enumerate(samples, 1):
            did = f"demo-{i:04d}-{uuid.uuid4().hex[:6]}"
            STATE[did] = {
                "drone_id": did,
                "commit": commit,
                "uptime_s": up,
                "healthcheck": {"summary": hc},
                "last_beacon_ts": _now(),
                "ip": "demo",
            }

    return redirect(url_for("dashboard"))


@app.post("/api/demo/clear")
def demo_clear():
    with LOCK:
        for did in [d for d in STATE if d.startswith("demo-")]:
            del STATE[did]
            PENDING.pop(did, None)

    return redirect(url_for("dashboard"))


# ============================================================
# Deploy
# ============================================================

@app.post("/deploy")
def deploy():
    if not _deploy_enabled():
        abort(400, "deploy вимкнено: не налаштований KISA_GIT_REPO або repo недоступна")

    commit = (request.form.get("commit") or "").strip()
    drone_ids = request.form.getlist("drone_ids")

    if not commit or not drone_ids:
        abort(400, "потрібен commit і хоча б одна наземка")

    if not _safe_commit(commit):
        abort(400, "commit має бути hex-hash довжиною 7-64 символи")

    if not _commit_exists(commit):
        abort(400, f"commit {commit} не знайдено")

    _build_bundle(commit)

    cmd_id = str(uuid.uuid4())
    bundle_url = f"{BASE_URL}/bundles/{commit}.tar.gz" if BASE_URL else f"/bundles/{commit}.tar.gz"

    with LOCK:
        for did in drone_ids:
            PENDING[did] = {
                "id": cmd_id,
                "action": "update",
                "commit": commit,
                "bundle_url": bundle_url,
                "created_at": _now(),
            }
            log.info("queued update %s -> %s", did[:8], commit[:8])

    return redirect(url_for("dashboard"))


@app.get("/bundles/<commit>.tar.gz")
def serve_bundle(commit: str):
    if not _safe_commit(commit):
        abort(400)

    path = BUNDLE_DIR / f"{commit}.tar.gz"

    if not path.exists():
        if _deploy_enabled() and _commit_exists(commit):
            _build_bundle(commit)
        else:
            abort(404)

    return send_file(
        path,
        mimetype="application/gzip",
        as_attachment=True,
        download_name=f"{commit}.tar.gz",
    )


def _build_bundle(commit: str) -> pathlib.Path:
    """Atomic build: пишемо у .tmp, потім rename.

    Це захищає від битого архіву, якщо наземка почне завантаження
    під час паралельної збірки.
    """
    with BUILD_LOCK:
        path = BUNDLE_DIR / f"{commit}.tar.gz"

        if path.exists():
            return path

        subprocess.run(["git", "-C", GIT_REPO, "fetch", "origin"], check=True, timeout=120)

        tmp = path.with_suffix(".tar.gz.tmp")
        try:
            with open(tmp, "wb") as f:
                subprocess.run(
                    [
                        "git",
                        "-C",
                        GIT_REPO,
                        "archive",
                        "--format=tar.gz",
                        commit,
                        *BUNDLE_PATHS,
                    ],
                    stdout=f,
                    check=True,
                    timeout=60,
                )
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()

        log.info("built bundle %s (%d bytes)", commit[:8], path.stat().st_size)
        return path


def _commit_exists(commit: str) -> bool:
    if not _deploy_enabled():
        return False

    result = subprocess.run(
        ["git", "-C", GIT_REPO, "cat-file", "-e", f"{commit}^{{commit}}"],
        capture_output=True,
        timeout=10,
    )
    return result.returncode == 0


def _list_commits(n: int = 25) -> List[Dict[str, str]]:
    if not _deploy_enabled():
        return []

    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                GIT_REPO,
                "log",
                "origin/HEAD",
                "-n",
                str(n),
                "--format=%H|%s|%ci",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout
    except Exception:
        log.exception("git log failed")
        return []

    rows: List[Dict[str, str]] = []
    for line in out.strip().splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            rows.append({"hash": parts[0], "subject": parts[1], "date": parts[2]})
    return rows


def _fetch_loop():
    while True:
        if _deploy_enabled():
            try:
                result = subprocess.run(
                    [
                        "git",
                        "-C",
                        GIT_REPO,
                        "fetch",
                        "--prune",
                        "origin",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=120,
                )

                if result.returncode == 0:
                    log.info(
                        "git fetch completed; next check in %d seconds",
                        GIT_FETCH_INTERVAL_S,
                    )
                else:
                    log.warning(
                        "git fetch failed with code %d: %s",
                        result.returncode,
                        result.stderr.strip(),
                    )

            except Exception:
                log.exception("git fetch failed")

        time.sleep(GIT_FETCH_INTERVAL_S)


if _deploy_enabled():
    threading.Thread(target=_fetch_loop, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=INTERNAL_PORT)
