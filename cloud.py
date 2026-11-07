"""Метроном · сервер владельца на бесплатном Render: бот-пульт и Страж в одном сервисе.

Запуск: python -u cloud.py (команда запуска в Render). Только стандартный Python 3.9+.

Что делает:
• держит веб-страницу на $PORT (/health — для проверки Render, / — состояние);
• сам себя пингует раз в ~10 минут, чтобы бесплатный Render не усыплял сервер (усыпляет через 15);
• запускает hub/hub.py и guard/guard.py и перезапускает, если упали;
• при остановке (SIGTERM от Render) даёт им дописать базу в Turso.

Настройки — переменные окружения сервиса (их заполняет мастер установки):
  HUB_BOT_TOKEN, REPO, ADMIN_CODE, PRICE, HUB_DB_URL, HUB_DB_TOKEN          — бот-пульт
  GUARD_BOT_TOKEN, GUARD_PASSWORD, GUARD_TZ, GUARD_NEW_PASSWORD,
  GUARD_DB_URL, GUARD_DB_TOKEN                                               — Страж (необязательно)
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.2"
HERE = os.path.dirname(os.path.abspath(__file__))
PING_EVERY = 10 * 60
STOP_WAIT = 25
FIRST_BACKOFF = 5

APPS = [
    {"id": "hub", "name": "бот-пульт", "script": os.path.join("hub", "hub.py"), "need": "HUB_BOT_TOKEN",
     "env": {"BOT_TOKEN": "HUB_BOT_TOKEN", "DB_URL": "HUB_DB_URL", "DB_TOKEN": "HUB_DB_TOKEN"}},
    {"id": "guard", "name": "Страж", "script": os.path.join("guard", "guard.py"), "need": "GUARD_BOT_TOKEN",
     "env": {"BOT_TOKEN": "GUARD_BOT_TOKEN", "PASSWORD": "GUARD_PASSWORD", "NEW_PASSWORD": "GUARD_NEW_PASSWORD",
             "TZ_NAME": "GUARD_TZ", "DB_URL": "GUARD_DB_URL", "DB_TOKEN": "GUARD_DB_TOKEN"}},
]
MAPPED = {src for a in APPS for src in a["env"].values()} | {dst for a in APPS for dst in a["env"]}


def say(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S") + " [сервер] " + msg, flush=True)


def child_env(app, environ=None):
    """Окружение для программы: только её ключи, чужие токены не передаются."""
    environ = os.environ if environ is None else environ
    env = {k: v for k, v in environ.items() if k not in MAPPED}
    for dst, src in app["env"].items():
        if environ.get(src):
            env[dst] = environ[src]
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("PORT", None)
    return env


class Proc:
    def __init__(self, app, cmd=None, environ=None):
        self.app = app
        self.cmd = cmd or [sys.executable, "-u", os.path.join(HERE, app["script"])]
        self.environ = environ
        self.p = None
        self.started = 0.0
        self.restarts = 0
        self.last_exit = None
        self.stopping = False

    def start(self):
        self.started = time.time()
        self.p = subprocess.Popen(self.cmd, cwd=os.path.dirname(self.cmd[-1]) or HERE, env=child_env(self.app, self.environ),
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1,
                                  universal_newlines=True, encoding="utf-8", errors="replace")
        threading.Thread(target=self._pump, args=(self.p,), daemon=True).start()

    def _pump(self, p):
        tag = " [" + self.app["id"] + "] "
        for line in p.stdout:
            print(time.strftime("%H:%M:%S") + tag + line.rstrip("\n"), flush=True)

    def alive(self):
        return bool(self.p and self.p.poll() is None)

    def supervise(self, stop):
        backoff = FIRST_BACKOFF
        while not stop.is_set():
            self.start()
            say(f"запущен {self.app['name']}")
            code = self.p.wait()
            self.last_exit = code
            if stop.is_set() or self.stopping:
                break
            ran = time.time() - self.started
            backoff = FIRST_BACKOFF if ran > 300 else min(backoff * 2, 300)
            if code == 2:
                say(f"{self.app['name']}: Telegram не принял токен бота — проверь его в настройках (Environment)")
                backoff = 300
            self.restarts += 1
            say(f"{self.app['name']} остановился (код {code}), перезапуск через {backoff} с")
            stop.wait(backoff)

    def terminate(self):
        self.stopping = True
        if self.alive():
            try:
                self.p.send_signal(signal.SIGTERM)
            except OSError:
                pass

    def status(self):
        return {"name": self.app["name"], "running": self.alive(), "restarts": self.restarts,
                "uptime": int(time.time() - self.started) if self.alive() else 0, "last_exit": self.last_exit}


class Cloud:
    def __init__(self, environ=None, cmds=None):
        self.environ = os.environ if environ is None else environ
        self.stop = threading.Event()
        self.started = time.time()
        self.procs = [Proc(a, (cmds or {}).get(a["id"]), self.environ) for a in APPS if self.environ.get(a["need"])]
        self.pings = 0

    def status(self):
        return {"metronom": VERSION, "uptime": int(time.time() - self.started), "pings": self.pings,
                "apps": [p.status() for p in self.procs]}

    def run_apps(self):
        if not self.procs:
            say("Нечего запускать: не задан HUB_BOT_TOKEN (и GUARD_BOT_TOKEN). Запусти мастер установки.")
        for p in self.procs:
            threading.Thread(target=p.supervise, args=(self.stop,), daemon=True).start()

    def pinger(self, url):
        if not url:
            say("Адрес сервера неизвестен (нет RENDER_EXTERNAL_URL) — самопинг выключен")
            return
        url = url.rstrip("/") + "/health"
        while not self.stop.wait(PING_EVERY):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "metronom-keepalive"}),
                                            timeout=30) as r:
                    r.read()
                self.pings += 1
            except Exception as e:
                say(f"самопинг не прошёл: {e}")

    def shutdown(self, *_):
        if self.stop.is_set():
            return
        say("остановка: дописываю базу и выхожу")
        self.stop.set()
        for p in self.procs:
            p.terminate()
        end = time.time() + STOP_WAIT
        for p in self.procs:
            while p.alive() and time.time() < end:
                time.sleep(0.2)
            if p.alive():
                p.p.kill()

    def handler(self):
        cloud = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="text/plain; charset=utf-8"):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/health":
                    return self._send(200, "ok")
                if path == "/status.json":
                    return self._send(200, json.dumps(cloud.status(), ensure_ascii=False), "application/json")
                if path == "/":
                    st = cloud.status()
                    lines = [f"Метроном {VERSION} · сервер владельца", ""]
                    for a in st["apps"]:
                        lines.append(f"{'🟢' if a['running'] else '🔴'} {a['name']}: "
                                     f"{'работает' if a['running'] else 'перезапускается'} · перезапусков: {a['restarts']}")
                    if not st["apps"]:
                        lines.append("Ничего не запущено — не задан токен бота.")
                    lines += ["", f"Работает {st['uptime'] // 60} мин · самопингов: {st['pings']}"]
                    return self._send(200, "\n".join(lines))
                self._send(404, "not found")

            do_HEAD = do_GET

        return H

    def serve(self, port):
        srv = ThreadingHTTPServer(("0.0.0.0", port), self.handler())
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv


def main():
    cloud = Cloud()
    port = int(os.environ.get("PORT") or 10000)
    srv = cloud.serve(port)
    say(f"Метроном {VERSION}: веб на порту {port}")
    signal.signal(signal.SIGTERM, cloud.shutdown)
    signal.signal(signal.SIGINT, cloud.shutdown)
    cloud.run_apps()
    threading.Thread(target=cloud.pinger, args=(os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_URL"),),
                     daemon=True).start()
    while not cloud.stop.wait(1):
        pass
    srv.shutdown()
    say("готово")


if __name__ == "__main__":
    main()
