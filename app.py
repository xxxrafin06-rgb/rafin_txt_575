import os
import json
import signal
import subprocess
import shutil
import zipfile
import hashlib
import psutil
import threading
import time
import urllib.request
import atexit
from pathlib import Path
from functools import wraps
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify

app = Flask(__name__)

# ---- Stable secret key (env থেকে, না থাকলে fallback) ----
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-render-env-" + hashlib.sha256(b"rafin").hexdigest())
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024

BASE_DIR = Path(__file__).parent

# ---- Persistent data dir (Render Disk mount path বা local) ----
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    DATA_DIR = BASE_DIR

DATA_FILE   = DATA_DIR / "data.json"
SERVERS_DIR = DATA_DIR / "servers"
SERVERS_DIR.mkdir(parents=True, exist_ok=True)

NORMAL_PASSWORD = os.environ.get("NORMAL_PASSWORD", "RAFIN_TxT_123")
RUNNING_PROCESSES = {}


def load_data():
    if DATA_FILE.exists():
        try:
            d = json.loads(DATA_FILE.read_text())
            if "settings" not in d:
                d["settings"] = {}
            return d
        except Exception:
            pass
    return {
        "servers": {},
        "users": {},
        "settings": {
            "maintenance": False,
            "maintenance_msg": "System under maintenance.",
            "theme_color": "#8b5cf6",
            "normal_password": NORMAL_PASSWORD,
            "site_name": "RAFIN TxT",
            "auto_restart_interval": 300
        }
    }


def save_data(data):
    try:
        DATA_FILE.write_text(json.dumps(data, indent=2, default=str))
    except Exception as e:
        print(f"[save_data error] {e}")


def hash_password(p):
    return hashlib.sha256(p.encode()).hexdigest()


def get_theme_color():
    return load_data().get("settings", {}).get("theme_color", "#8b5cf6")


@app.context_processor
def inject_theme():
    return {"theme_color": get_theme_color()}


def is_process_alive(pid):
    try:
        if not pid:
            return False
        p = psutil.Process(pid)
        return p.is_running() and p.status() not in [psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def kill_process(pid):
    try:
        p = psutil.Process(pid)
        for c in p.children(recursive=True):
            try:
                c.terminate()
            except Exception:
                pass
        p.terminate()
        try:
            p.wait(timeout=5)
        except psutil.TimeoutExpired:
            p.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


def get_run_command(runtime, main_file):
    ext = Path(main_file).suffix.lower()
    if runtime == "node" or ext in (".js", ".ts", ".mjs"):
        return ["node", main_file]
    if runtime == "static":
        return ["python", "-m", "http.server", "8080"]
    return ["python", "-u", main_file]


# ==================== STARTUP RECOVERY ====================
def startup_recovery():
    """Render restart হলে পুরনো 'running' status গুলো false হয়ে যায়।
    তাই startup এ সব server কে stopped mark করি।"""
    try:
        data = load_data()
        changed = False
        for name, cfg in data.get("servers", {}).items():
            if cfg.get("status") == "running":
                cfg["status"] = "stopped"
                cfg["pid"] = None
                changed = True
        if changed:
            save_data(data)
        print(f"[startup] recovery done. data_dir={DATA_DIR}")
    except Exception as e:
        print(f"[startup] error: {e}")


# ==================== GRACEFUL SHUTDOWN ====================
def shutdown_all():
    print("[shutdown] killing child processes...")
    for name in list(RUNNING_PROCESSES.keys()):
        try:
            entry = RUNNING_PROCESSES[name]
            os.killpg(os.getpgid(entry["proc"].pid), signal.SIGTERM)
        except Exception:
            pass
        try:
            entry["log_file"].close()
        except Exception:
            pass


atexit.register(shutdown_all)


# ==================== EMERGENCY MAINTENANCE KILL ====================
@app.route("/killmaintenance")
def kill_maintenance():
    data = load_data()
    data.setdefault("settings", {})["maintenance"] = False
    save_data(data)
    return """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Fixed</title>
    <style>body{background:#0a0b14;color:#8b5cf6;font-family:monospace;display:flex;
    align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}
    h1{font-size:80px;margin:0;text-shadow:0 0 40px #8b5cf6}
    a{color:#fff;text-decoration:none;border:2px solid #8b5cf6;padding:12px 24px;
    border-radius:12px;display:inline-block;margin-top:20px}
    a:hover{background:#8b5cf6;color:#0a0b14}</style></head>
    <body><div><h1>✓</h1><h2>MAINTENANCE DISABLED</h2>
    <p style="opacity:.6">System is now live.</p>
    <a href="/">→ Go to Login</a></div></body></html>"""


# ==================== LOGIN REQUIRED ====================
def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("username"):
            return redirect(url_for("login"))
        data = load_data()
        s = data.get("settings", {})
        if s.get("maintenance"):
            return render_template("maintenance.html",
                message=s.get("maintenance_msg", "Under maintenance"),
                site_name=s.get("site_name", "RAFIN TxT"),
                theme_color=get_theme_color())
        return f(*a, **kw)
    return wrapper


# ==================== KEEP ALIVE (Render sleep রোধ) ====================
def render_keep_alive():
    """Render free tier এ ১৫ মিনিট inactivity তে sleep হয়।
    এটা local + external ping দিয়ে জাগিয়ে রাখার চেষ্টা করে।"""
    external_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    local_port = os.environ.get("PORT", "5000")   # ✅ Dynamic port fix

    while True:
        try:
            time.sleep(600)  # প্রতি ১০ মিনিটে ping

            # ---- Local ping ----
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{local_port}/api/ping", timeout=10
                )
            except Exception:
                pass

            # ---- External ping (Render spin-down ঠেকাতে) ----
            if external_url:
                try:
                    req = urllib.request.Request(
                        f"{external_url}/api/ping",
                        headers={"User-Agent": "RenderKeepAlive/1.0"}
                    )
                    urllib.request.urlopen(req, timeout=15)
                except Exception:
                    pass
        except Exception:
            pass


threading.Thread(target=render_keep_alive, daemon=True).start()


@app.route("/api/ping")
def ping():
    return "pong", 200


# ==================== AUTO RESTART ====================
def auto_restart_server(name):
    try:
        data = load_data()
        cfg = data["servers"].get(name)
        if not cfg:
            return
        pid = cfg.get("pid")
        if pid and is_process_alive(pid):
            kill_process(pid)
        main_file = cfg.get("main_file") or "main.py"
        extract_dir = SERVERS_DIR / name / "extracted"
        main_path = extract_dir / main_file
        if not main_path.exists():
            return
        log_path = SERVERS_DIR / name / "logs.txt"
        cmd = cfg.get("main_command", "").split() or get_run_command(
            cfg.get("runtime", "python"), main_file)
        env = os.environ.copy()
        env["PORT"] = str(cfg.get("port", 8080))
        with open(log_path, "a") as f:
            f.write(f"\n[{datetime.now().isoformat()}] AUTO-RESTART\n")
        lf = open(log_path, "a")
        proc = subprocess.Popen(cmd, cwd=str(extract_dir), stdout=lf, stderr=lf,
                                env=env, preexec_fn=os.setsid)
        RUNNING_PROCESSES[name] = {"proc": proc, "log_file": lf}
        cfg["status"] = "running"
        cfg["pid"] = proc.pid
        save_data(data)
    except Exception as e:
        print(f"Auto-restart error: {e}")


def auto_restart_monitor():
    while True:
        try:
            data = load_data()
            interval = data.get("settings", {}).get("auto_restart_interval", 300)
            for name, cfg in list(data["servers"].items()):
                pid = cfg.get("pid")
                if pid and not is_process_alive(pid):
                    cfg["status"] = "stopped"
                    cfg["pid"] = None
                    save_data(data)
            time.sleep(interval)
        except Exception:
            time.sleep(30)


threading.Thread(target=auto_restart_monitor, daemon=True).start()


# ==================== LOGIN ====================
@app.route("/", methods=["GET", "POST"])
def login():
    if session.get("username"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        pw = request.form.get("password", "").strip()
        data = load_data()
        s = data.get("settings", {})
        correct = s.get("normal_password", NORMAL_PASSWORD)
        if pw != correct:
            return render_template("login.html", error="Invalid access key",
                theme_color=get_theme_color(),
                site_name=s.get("site_name", "RAFIN TxT"))
        u = "admin"
        if u not in data["users"]:
            data["users"][u] = {"joined": datetime.now().isoformat(),
                                "password_hash": hash_password(pw)}
            save_data(data)
        session["username"] = u
        session.permanent = True
        return redirect(url_for("dashboard"))
    data = load_data()
    return render_template("login.html", error=None,
        theme_color=get_theme_color(),
        site_name=data.get("settings", {}).get("site_name", "RAFIN TxT"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ==================== DASHBOARD ====================
@app.route("/dashboard")
@login_required
def dashboard():
    u = session["username"]
    data = load_data()
    s = data.get("settings", {})
    us = {k: v for k, v in data["servers"].items() if v.get("owner") == u}
    changed = False
    for n, c in us.items():
        if c.get("pid") and not is_process_alive(c["pid"]):
            c["status"] = "stopped"
            c["pid"] = None
            changed = True
    if changed:
        save_data(data)
    r = sum(1 for v in us.values() if v.get("status") == "running")
    return render_template("dashboard.html", servers=us, running=r, total=len(us),
        username=u, site_name=s.get("site_name", "RAFIN TxT"),
        theme_color=get_theme_color())


@app.route("/api/stats")
@login_required
def stats():
    return jsonify({
        "cpu": psutil.cpu_percent(interval=0.1),
        "ram": psutil.virtual_memory().percent,
        "disk": psutil.disk_usage("/").percent
    })


# ==================== CRUD ====================
@app.route("/server/create", methods=["POST"])
@login_required
def create_server():
    name = request.form.get("name", "").strip().replace(" ", "-")
    runtime = request.form.get("runtime", "python")
    if not name:
        return redirect(url_for("dashboard"))
    data = load_data()
    if name in data["servers"]:
        return redirect(url_for("dashboard"))
    data["servers"][name] = {
        "name": name, "owner": session["username"], "runtime": runtime,
        "status": "stopped", "main_file": "", "main_command": "",
        "port": 8080, "pid": None, "created": datetime.now().isoformat()
    }
    save_data(data)
    (SERVERS_DIR / name / "extracted").mkdir(parents=True, exist_ok=True)
    return redirect(url_for("server_detail", name=name))


@app.route("/server/delete/<name>", methods=["POST"])
@login_required
def delete_server(name):
    data = load_data()
    c = data["servers"].get(name)
    if c and c.get("owner") == session["username"]:
        if c.get("pid"):
            kill_process(c["pid"])
        if name in RUNNING_PROCESSES:
            try:
                RUNNING_PROCESSES[name]["proc"].terminate()
                RUNNING_PROCESSES[name]["log_file"].close()
            except Exception:
                pass
            del RUNNING_PROCESSES[name]
        del data["servers"][name]
        save_data(data)
        shutil.rmtree(SERVERS_DIR / name, ignore_errors=True)
    return redirect(url_for("dashboard"))


@app.route("/server/<name>")
@login_required
def server_detail(name):
    data = load_data()
    c = data["servers"].get(name)
    if not c:
        return "Not found", 404
    if c.get("owner") != session["username"]:
        return "Access denied", 403
    if c.get("pid") and not is_process_alive(c["pid"]):
        c["status"] = "stopped"
        c["pid"] = None
        save_data(data)
    if "main_command" not in c:
        c["main_command"] = ""
    ed = SERVERS_DIR / name / "extracted"
    return render_template("server.html", server_name=name, config=c,
        files=list_files(ed), theme_color=get_theme_color(),
        site_name=data.get("settings", {}).get("site_name", "RAFIN TxT"))


def list_files(d, base=""):
    r = []
    if not d.exists():
        return r
    try:
        for e in sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name)):
            rel = f"{base}/{e.name}" if base else e.name
            if e.is_dir():
                r.append({"name": e.name, "path": rel, "type": "dir", "size": 0})
                r.extend(list_files(e, rel))
            else:
                r.append({"name": e.name, "path": rel, "type": "file", "size": e.stat().st_size})
    except Exception:
        pass
    return r


@app.route("/server/<name>/upload", methods=["POST"])
@login_required
def upload_file(name):
    data = load_data()
    c = data["servers"].get(name)
    if not c:
        return jsonify({"success": False, "error": "Server not found"}), 404
    if c.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"success": False, "error": "Empty filename"}), 400
    ed = SERVERS_DIR / name / "extracted"
    ed.mkdir(parents=True, exist_ok=True)
    up = SERVERS_DIR / name / f"upload_{f.filename}"
    f.save(up)
    extracted = []
    try:
        if f.filename.lower().endswith(".zip"):
            with zipfile.ZipFile(up, "r") as z:
                for m in z.infolist():
                    if m.filename.startswith(("/", "\\", "..", "../")):
                        up.unlink(missing_ok=True)
                        return jsonify({"success": False, "error": "Invalid zip"})
                z.extractall(ed)
                for m in z.infolist():
                    if not m.is_dir():
                        extracted.append(m.filename)
            up.unlink(missing_ok=True)
            COMMON = ["app.py", "main.py", "bot.py", "run.py",
                      "index.js", "server.js", "main.js"]
            if not c.get("main_file"):
                for m in extracted:
                    if Path(m).name in COMMON:
                        c["main_file"] = m
                        data["servers"][name] = c
                        save_data(data)
                        break
        else:
            dest = ed / f.filename
            shutil.move(str(up), str(dest))
            extracted = [f.filename]
            if not c.get("main_file") and f.filename.endswith((".py", ".js", ".ts")):
                c["main_file"] = f.filename
                data["servers"][name] = c
                save_data(data)
    except Exception as e:
        up.unlink(missing_ok=True)
        return jsonify({"success": False, "error": f"Failed: {str(e)}"}), 500
    return jsonify({"success": True, "files": extracted, "count": len(extracted)})


@app.route("/server/<name>/settings", methods=["POST"])
@login_required
def save_settings(name):
    data = load_data()
    c = data["servers"].get(name)
    if not c:
        return jsonify({"success": False}), 404
    if c.get("owner") != session["username"]:
        return jsonify({"success": False}), 403
    p = request.get_json()
    c["main_file"] = p.get("main_file", c.get("main_file", ""))
    c["main_command"] = p.get("main_command", c.get("main_command", ""))
    c["port"] = p.get("port", c.get("port", 8080))
    save_data(data)
    return jsonify({"success": True})


@app.route("/server/<name>/start", methods=["POST"])
@login_required
def start_server(name):
    data = load_data()
    c = data["servers"].get(name)
    if not c:
        return jsonify({"success": False, "error": "Not found"}), 404
    if c.get("owner") != session["username"]:
        return jsonify({"success": False}), 403
    if c.get("pid") and is_process_alive(c["pid"]):
        return jsonify({"success": False, "error": "Already running"})
    mf = c.get("main_file") or "main.py"
    ed = SERVERS_DIR / name / "extracted"
    if not (ed / mf).exists():
        return jsonify({"success": False, "error": f"{mf} not found. Upload files first."})
    lp = SERVERS_DIR / name / "logs.txt"
    lp.parent.mkdir(parents=True, exist_ok=True)
    cmd = c.get("main_command", "").split() or get_run_command(c.get("runtime", "python"), mf)
    env = os.environ.copy()
    env["PORT"] = str(c.get("port", 8080))
    try:
        with open(lp, "a") as f:
            f.write(f"\n[{datetime.now().isoformat()}] START: {' '.join(cmd)}\n")
        lf = open(lp, "a")
        proc = subprocess.Popen(cmd, cwd=str(ed), stdout=lf, stderr=lf,
                                env=env, preexec_fn=os.setsid)
        RUNNING_PROCESSES[name] = {"proc": proc, "log_file": lf}
        c["status"] = "running"
        c["pid"] = proc.pid
        save_data(data)
        return jsonify({"success": True, "pid": proc.pid})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/server/<name>/stop", methods=["POST"])
@login_required
def stop_server(name):
    data = load_data()
    c = data["servers"].get(name)
    if not c:
        return jsonify({"success": False}), 404
    if c.get("owner") != session["username"]:
        return jsonify({"success": False}), 403
    stopped = False
    if name in RUNNING_PROCESSES:
        e = RUNNING_PROCESSES[name]
        try:
            os.killpg(os.getpgid(e["proc"].pid), signal.SIGTERM)
        except Exception:
            try:
                e["proc"].terminate()
            except Exception:
                pass
        try:
            e["proc"].wait(timeout=5)
        except Exception:
            try:
                e["proc"].kill()
            except Exception:
                pass
        try:
            e["log_file"].close()
        except Exception:
            pass
        del RUNNING_PROCESSES[name]
        stopped = True
    if c.get("pid") and not stopped:
        kill_process(c["pid"])
    c["status"] = "stopped"
    c["pid"] = None
    save_data(data)
    return jsonify({"success": True})


@app.route("/server/<name>/logs")
@login_required
def get_logs(name):
    data = load_data()
    c = data["servers"].get(name)
    if not c:
        return jsonify({"logs": "Not found"})
    lp = SERVERS_DIR / name / "logs.txt"
    if not lp.exists():
        return jsonify({"logs": "No logs yet."})
    try:
        content = lp.read_text(errors="replace")
        lines = content.splitlines()
        if len(lines) > 300:
            content = "... (last 300 lines) ...\n" + "\n".join(lines[-300:])
        return jsonify({"logs": content or "No output."})
    except Exception as e:
        return jsonify({"logs": f"Error: {e}"})


@app.route("/server/<name>/logs/clear", methods=["POST"])
@login_required
def clear_logs(name):
    lp = SERVERS_DIR / name / "logs.txt"
    try:
        lp.write_text("")
    except Exception:
        pass
    return jsonify({"success": True})


# ==================== RUN ====================
startup_recovery()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)