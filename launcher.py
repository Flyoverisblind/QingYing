from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

# Make source checkouts and PyInstaller builds both resolve the package.
BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app import config  # noqa: E402


def _redirect_output() -> None:
    config.ensure_dirs()
    if getattr(sys, "frozen", False) and (sys.stdout is None or sys.stderr is None):
        log_path = config.LOG_DIR / "launcher.log"
        stream = open(log_path, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass


def _setup_logging() -> None:
    try:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[
                logging.FileHandler(config.LOG_DIR / "app.log", encoding="utf-8"),
                logging.StreamHandler(),
            ],
        )
    except Exception:
        logging.basicConfig(level=logging.INFO)


def _runtime_path() -> Path:
    return config.DATA_DIR / "runtime.json"


def _health(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def _open_url(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _existing_instance() -> str | None:
    path = _runtime_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        port = int(payload["port"])
    except Exception:
        return None
    health = _health(port)
    if health and health.get("ok"):
        return f"http://127.0.0.1:{port}"
    return None


def _pick_port(host: str, preferred: int) -> int:
    candidates = [preferred] + [preferred + offset for offset in range(1, 16)]
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError("没有可用的本地端口")


def _open_output_dir() -> None:
    path = config.OUTPUT_DIR
    try:
        if config.IS_WINDOWS:
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif config.IS_MACOS:
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def _run_tray(url: str, stop_callback) -> bool:
    try:
        import pystray
        from PIL import Image
    except Exception:
        return False
    icon_path = config.ASSETS_DIR / "icon.png"
    if not icon_path.exists():
        return False
    try:
        image = Image.open(icon_path)
        menu = pystray.Menu(
            pystray.MenuItem("打开清影", lambda: _open_url(url), default=True),
            pystray.MenuItem("打开输出目录", lambda: _open_output_dir()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda: stop_callback()),
        )
        icon = pystray.Icon("qingying", image, "清影 · 视频超分与补帧", menu)
        icon.run()
        return True
    except Exception as exc:
        print(f"托盘图标不可用: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="清影 · 本地视频超分与补帧")
    parser.add_argument("--host", default=os.environ.get("QINGYING_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("QINGYING_PORT", str(config.DEFAULT_PORT))))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-tray", action="store_true")
    parser.add_argument("--tray", action="store_true")
    parser.add_argument("--ai-probe", action="store_true")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args()

    if args.version:
        print(f"清影 QingYing {config.VERSION}")
        return 0

    if args.ai_probe:
        from app.ai import ai_probe_cli

        return ai_probe_cli()

    _redirect_output()
    config.ensure_dirs()

    _setup_logging()

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        logging.warning(
            "服务绑定在 %s，局域网内其他设备可访问本机文件与任务接口。"
            "如无必要，请使用默认的 127.0.0.1。", args.host
        )

    existing = _existing_instance()
    if existing:
        if not args.no_browser and not os.environ.get("QINGYING_NO_BROWSER"):
            _open_url(existing)
        print(f"清影已经在运行：{existing}")
        return 0

    port = _pick_port(args.host, args.port)
    url = f"http://{'127.0.0.1' if args.host in {'0.0.0.0', '::'} else args.host}:{port}"

    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(
            "app.main:app",
            host=args.host,
            port=port,
            log_level="info",
            access_log=False,
            timeout_keep_alive=15,
        )
    )
    thread = threading.Thread(target=server.run, name="qingying-http", daemon=True)
    thread.start()

    ready = False
    for _ in range(120):
        if _health(port):
            ready = True
            break
        if not thread.is_alive():
            break
        time.sleep(0.25)

    if not ready:
        logging.error("本地服务启动失败，请查看日志：%s", config.LOG_DIR / "app.log")
        if getattr(sys, "frozen", False) and config.IS_WINDOWS:
            try:
                import ctypes

                ctypes.windll.user32.MessageBoxW(0, "清影启动失败，请查看 data\\logs\\app.log", "清影", 0x10)
            except Exception:
                pass
        return 1

    runtime = {
        "url": url,
        "host": args.host,
        "port": port,
        "pid": os.getpid(),
        "version": config.VERSION,
        "started_at": time.time(),
    }
    try:
        _runtime_path().write_text(json.dumps(runtime, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    print(f"\n  清影 QingYing {config.VERSION}\n  运行地址：{url}\n  输出目录：{config.OUTPUT_DIR}\n")
    if not args.no_browser and not os.environ.get("QINGYING_NO_BROWSER"):
        threading.Timer(0.4, _open_url, args=(url,)).start()

    exit_started = threading.Event()

    def shutdown() -> None:
        if exit_started.is_set():
            return
        exit_started.set()
        server.should_exit = True
        try:
            _runtime_path().unlink(missing_ok=True)
        except Exception:
            pass
        time.sleep(0.8)
        os._exit(0)

    use_tray = not args.no_tray and (args.tray or getattr(sys, "frozen", False))
    tray_started = False
    if use_tray:
        tray_started = _run_tray(url, shutdown)

    if not tray_started:
        try:
            while thread.is_alive():
                time.sleep(0.8)
        except KeyboardInterrupt:
            shutdown()
        finally:
            if not exit_started.is_set():
                shutdown()
    return 0


if __name__ == "__main__":
    if getattr(sys, "frozen", False):
        import multiprocessing

        multiprocessing.freeze_support()
    sys.exit(main())
