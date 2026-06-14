"""КІСА - мінімальний робочий застосунок.
Приймає beacons від наземок, показує стан парку, дозволяє запланувати
оновлення (якщо налаштований git-репозиторій ОДЕСА).

Конфіг через env (з /opt/kisa/.env):
  KISA_BASE_URL      напр. https://kisa.dodproduction.space  (для bundle_url у командах)
  KISA_GIT_REPO      шлях до локального clone ОДЕСА; порожньо = deploy вимкнено
  KISA_BUNDLE_DIR    де складати зібрані бандли (default /data/bundles)
  KISA_INTERNAL_PORT порт (default 8000; слухає gunicorn ззовні)
"""
import os
import time
import uuid
import subprocess
import pathlib
import threading
import logging

from flask import Flask, request, jsonify, render_template, redirect, url_for, send_file, abort
from werkzeug.middleware.proxy_fix import ProxyFix

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("kisa")

app = Flask(__name__)
# За Caddy: підставляє реальний IP клієнта з X-Forwarded-For,
# схему з X-Forwarded-Proto. Caddy довірений (єдиний proxy перед нами).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# --- стан (in-memory; beacons відновлюють після рестарту) ---
STATE = {}     # drone_id -> beacon payload + last_beacon_ts + ip
PENDING = {}   # drone_id -> {id, action, commit, bundle_url, created_at}
LOCK = threading.Lock()

ONLINE_S, STALE_S = 60, 300

BASE_URL = os.environ.get("KISA_BASE_URL", "").rstrip("/")
GIT_REPO = os.environ.get("KISA_GIT_REPO", "").strip()
BUNDLE_DIR = pathlib.Path(os.environ.get("KISA_BUNDLE_DIR", "/data/bundles"))
BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
BUILD_LOCK = threading.Lock()


# ============================================================
# API: beacon (наземки б'ють сюди кожні 30с)
# ============================================================
@app.post("/api/beacon")
def beacon():
    data = request.get_json(force=True, silent=True) or {}
    did = data.get("drone_id")
    if not did:
        return jsonify({"error": "no drone_id"}), 400
    with LOCK:
        STATE[did] = {**data, "last_beacon_ts": time.time(),
                      "ip": request.remote_addr}
        # ACK виконаної команди
        if data.get("last_command_id") and did in PENDING:
            if PENDING[did]["id"] == data["last_command_id"]:
                log.info("ACK %s cmd %s", did[:8], data["last_command_id"][:8])
                del PENDING[did]
        pend = PENDING.get(did)
    return jsonify({"pending": pend})


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "drones": len(STATE), "pending": len(PENDING)})


@app.get("/api/state")
def api_state():
    return jsonify(_drones_view())


# ============================================================
# Dashboard
# ============================================================
def _drones_view():
    now = time.time()
    out = []
    with LOCK:
        for did, st in sorted(STATE.items()):
            age = now - st["last_beacon_ts"]
            status = "online" if age < ONLINE_S else "stale" if age < STALE_S else "offline"
            out.append({**st, "drone_id": did, "age_s": int(age),
                        "status": status, "pending": PENDING.get(did)})
    return out


@app.get("/")
def dashboard():
    drones = _drones_view()
    commits = _list_commits() if GIT_REPO else []
    counts = {"online": sum(1 for d in drones if d["status"] == "online"),
              "total": len(drones)}
    return render_template("dashboard.html", drones=drones, commits=commits,
                           deploy_enabled=bool(GIT_REPO), counts=counts)


# ============================================================
# Demo: інжект тестових наземок щоб побачити живий дашборд
# ============================================================
@app.post("/api/demo/seed")
def demo_seed():
    samples = [
        ("a1b2c3d4e5f6", {"ok": 14, "warn": 0, "fail": 0, "skip": 2}, 86400),
        ("a1b2c3d4e5f6", {"ok": 13, "warn": 1, "fail": 0, "skip": 2}, 3600),
        ("f0e1d2c3b4a5", {"ok": 12, "warn": 0, "fail": 1, "skip": 3}, 600),
    ]
    with LOCK:
        for i, (commit, hc, up) in enumerate(samples, 1):
            did = f"demo-{i:04d}-{uuid.uuid4().hex[:6]}"
            STATE[did] = {
                "drone_id": did, "commit": commit, "uptime_s": up,
                "healthcheck": {"summary": hc},
                "last_beacon_ts": time.time(), "ip": "demo",
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
# Deploy (працює лише якщо KISA_GIT_REPO налаштований)
# ============================================================
@app.post("/deploy")
def deploy():
    if not GIT_REPO:
        abort(400, "deploy вимкнено: не налаштований KISA_GIT_REPO")
    commit = (request.form.get("commit") or "").strip()
    drone_ids = request.form.getlist("drone_ids")
    if not commit or not drone_ids:
        abort(400, "потрібен commit і хоча б одна наземка")
    if not _commit_exists(commit):
        abort(400, f"commit {commit} не знайдено")
    _build_bundle(commit)
    cmd_id = str(uuid.uuid4())
    bundle_url = f"{BASE_URL}/bundles/{commit}.tar.gz"
    with LOCK:
        for did in drone_ids:
            PENDING[did] = {"id": cmd_id, "action": "update", "commit": commit,
                            "bundle_url": bundle_url, "created_at": time.time()}
            log.info("queued update %s -> %s", did[:8], commit[:8])
    return redirect(url_for("dashboard"))


@app.get("/bundles/<commit>.tar.gz")
def serve_bundle(commit):
    if not all(c in "0123456789abcdefABCDEF" for c in commit) or not (7 <= len(commit) <= 64):
        abort(400)
    path = BUNDLE_DIR / f"{commit}.tar.gz"
    if not path.exists():
        if GIT_REPO and _commit_exists(commit):
            _build_bundle(commit)
        else:
            abort(404)
    return send_file(path, mimetype="application/gzip",
                     as_attachment=True, download_name=f"{commit}.tar.gz")


def _build_bundle(commit):
    with BUILD_LOCK:
        path = BUNDLE_DIR / f"{commit}.tar.gz"
        if path.exists():
            return path
        subprocess.run(["git", "-C", GIT_REPO, "fetch", "origin"],
                       check=True, timeout=120)
        with open(path, "wb") as f:
            subprocess.run(["git", "-C", GIT_REPO, "archive",
                            "--format=tar.gz", commit],
                           stdout=f, check=True, timeout=60)
        log.info("built bundle %s", commit[:8])
        return path


def _commit_exists(commit):
    r = subprocess.run(["git", "-C", GIT_REPO, "cat-file", "-e", f"{commit}^{{commit}}"],
                       capture_output=True, timeout=10)
    return r.returncode == 0


def _list_commits(n=25):
    try:
        out = subprocess.run(["git", "-C", GIT_REPO, "log", "-n", str(n),
                              "--format=%H|%s|%ci"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return []
    rows = []
    for line in out.strip().splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            rows.append({"hash": parts[0], "subject": parts[1], "date": parts[2]})
    return rows


# periodic git fetch (свіжий список commits)
def _fetch_loop():
    while True:
        if GIT_REPO:
            try:
                subprocess.run(["git", "-C", GIT_REPO, "fetch", "origin"],
                               check=False, timeout=120)
            except Exception:
                log.exception("git fetch failed")
        time.sleep(60)


if GIT_REPO:
    threading.Thread(target=_fetch_loop, daemon=True).start()

"""КІСА PoC: централізоване оновлення ОДЕСА через beacon-driven модель.
Без авторизації. Працює всередині WG-тунелю."""
import logging
import os
import pathlib
import subprocess
import threading
import time
import uuid

import yaml
from flask import (Flask, request, jsonify, render_template,
                   send_file, redirect, abort)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kisa")

CFG_PATH = os.environ.get("KISA_CONFIG", "/etc/kisa/kisa.yml")
CFG = yaml.safe_load(pathlib.Path(CFG_PATH).read_text(encoding="utf-8"))
GIT_REPO = CFG["git_repo"]                       # локальний clone ОДЕСА
BUNDLE_DIR = pathlib.Path(CFG["bundle_dir"])     # /srv/kisa/bundles
BASE_URL = CFG["base_url"].rstrip("/")           # http://10.66.66.10:8000
BUNDLE_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

# in-memory state (без persistence; beacons відновлюють за хвилину)
state = {}            # drone_id -> {commit, healthcheck, uptime_s, last_beacon_ts, ip}
pending = {}          # drone_id -> {id, action, commit, bundle_url, created_at}
state_lock = threading.Lock()
build_lock = threading.Lock()

ONLINE_S, STALE_S = 60, 300


# --------------------------------------------------------------------
@app.route("/api/beacon", methods=["POST"])
def beacon():
    data = request.get_json(force=True, silent=True) or {}
    did = data.get("drone_id")
    if not did:
        return jsonify({"error": "no drone_id"}), 400
    with state_lock:
        state[did] = {**data, "last_beacon_ts": time.time(),
                      "ip": request.remote_addr}
        # ACK: команда виконана наземкою?
        if data.get("last_command_id") and did in pending:
            if pending[did]["id"] == data["last_command_id"]:
                log.info("ACK from %s for cmd %s",
                         did[:8], data["last_command_id"][:8])
                del pending[did]
        resp_pending = pending.get(did)
    return jsonify({"pending": resp_pending})


# --------------------------------------------------------------------
@app.route("/")
def index():
    now = time.time()
    drones = []
    with state_lock:
        items = sorted(state.items())
        for did, st in items:
            age = now - st["last_beacon_ts"]
            status = ("online" if age < ONLINE_S
                      else "stale" if age < STALE_S else "offline")
            drones.append({**st, "drone_id": did, "age_s": int(age),
                           "status": status, "pending": pending.get(did)})
    commits = list_commits(GIT_REPO, 25)
    return render_template("index.html", drones=drones, commits=commits)


# --------------------------------------------------------------------
@app.route("/deploy", methods=["POST"])
def deploy():
    commit = (request.form.get("commit") or "").strip()
    drone_ids = request.form.getlist("drone_ids")
    if not commit or not drone_ids:
        abort(400, "commit і хоча б одна наземка обов'язкові")
    if not commit_exists(GIT_REPO, commit):
        abort(400, f"commit {commit} не знайдено")
    build_bundle(commit)
    cmd_id = str(uuid.uuid4())
    bundle_url = f"{BASE_URL}/bundles/{commit}.tar.gz"
    with state_lock:
        for did in drone_ids:
            pending[did] = {"id": cmd_id, "action": "update", "commit": commit,
                            "bundle_url": bundle_url, "created_at": time.time()}
            log.info("queued update %s -> %s (cmd %s)",
                     did[:8], commit[:8], cmd_id[:8])
    return redirect("/")


# --------------------------------------------------------------------
@app.route("/bundles/<commit>.tar.gz")
def serve_bundle(commit):
    # дозволяємо тільки hex-hash (захист від path traversal)
    if (not all(c in "0123456789abcdefABCDEF" for c in commit)
            or not (7 <= len(commit) <= 64)):
        abort(400)
    path = BUNDLE_DIR / f"{commit}.tar.gz"
    if not path.exists():
        if commit_exists(GIT_REPO, commit):
            build_bundle(commit)
        else:
            abort(404)
    return send_file(path, mimetype="application/gzip",
                     as_attachment=True, download_name=f"{commit}.tar.gz")


@app.route("/healthz")
def healthz():
    with state_lock:
        return jsonify({"ok": True, "drones": len(state),
                        "pending": len(pending)})


# --------------------------------------------------------------------
def build_bundle(commit):
    """Atomic build: пишемо у .tmp, потім rename. Захищає від битого
    архіву при паралельному завантаженні наземкою під час білда."""
    with build_lock:
        path = BUNDLE_DIR / f"{commit}.tar.gz"
        if path.exists():
            return path
        subprocess.run(["git", "-C", GIT_REPO, "fetch", "origin"],
                       check=True, timeout=120)
        tmp = path.with_suffix(".tar.gz.tmp")
        try:
            with open(tmp, "wb") as f:
                subprocess.run(["git", "-C", GIT_REPO, "archive",
                                "--format=tar.gz", commit],
                               stdout=f, check=True, timeout=60)
            os.replace(tmp, path)   # atomic на тому ж ФС
        finally:
            if tmp.exists():
                tmp.unlink()
        log.info("built bundle %s (%d bytes)", commit[:8], path.stat().st_size)
        return path


def commit_exists(repo, commit):
    r = subprocess.run(
        ["git", "-C", repo, "cat-file", "-e", f"{commit}^{{commit}}"],
        capture_output=True, timeout=10)
    return r.returncode == 0


def list_commits(repo, n=25):
    out = subprocess.run(
        ["git", "-C", repo, "log", "-n", str(n), "--format=%H|%s|%ci"],
        capture_output=True, text=True, timeout=15).stdout
    rows = []
    for line in out.strip().splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            rows.append({"hash": parts[0], "subject": parts[1],
                         "date": parts[2]})
    return rows


# periodic git fetch (щоб список commits був свіжий)
def fetch_loop():
    while True:
        try:
            subprocess.run(["git", "-C", GIT_REPO, "fetch", "origin"],
                           check=False, timeout=120)
        except Exception:
            log.exception("git fetch failed")
        time.sleep(60)


threading.Thread(target=fetch_loop, daemon=True).start()
