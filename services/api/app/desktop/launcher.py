"""双击入口：单实例、回环服务、浏览器与托盘；资源和数据互相独立。"""

from __future__ import annotations

import ctypes
import json
import logging
import secrets
import socket
import sys
import threading
import time
import webbrowser

from app.core.config import DATA_ROOT


def reserve_socket(preferred: int = 8000):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", preferred))
    except OSError:
        sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    return sock


def message(text):
    ctypes.windll.user32.MessageBoxW(None, text, "Legacy", 0x10)


def run():
    import msvcrt

    lock = None
    server = None
    icon = None
    thread = None
    owned = False
    log_path = DATA_ROOT / "logs" / "legacy.log"
    state_path = DATA_ROOT / "instance.json"
    try:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # windowed bootloader 没有标准流。库和异常输出都写入 UTF-8 日志。
        sys.stdout = sys.stderr = log_path.open("a", encoding="utf-8", buffering=1)
        if sys.stdin is None:
            import os

            sys.stdin = open(os.devnull, encoding="utf-8")
        lock = (DATA_ROOT / "instance.lock").open("a+b")
        if not lock.seek(0, 2):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            owned = True
        except OSError:
            import httpx

            for _ in range(150):
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    response = httpx.get(
                        state["url"] + "_desktop/instance", timeout=1, trust_env=False
                    )
                    if response.json().get("instance") == state["instance"]:
                        webbrowser.open(state["url"])
                        return
                except (OSError, ValueError, KeyError, httpx.HTTPError):
                    pass
                time.sleep(0.2)
            raise RuntimeError(
                "另一个 Legacy 正在启动或无响应。请稍后重试，或从托盘退出旧实例。"
            ) from None

        import pystray
        import uvicorn
        from PIL import Image, ImageDraw

        from app.main import app

        sock = reserve_socket()
        url = f"http://127.0.0.1:{sock.getsockname()[1]}/"
        instance = secrets.token_hex(24)
        server = uvicorn.Server(
            uvicorn.Config(app, log_config=None, access_log=False, loop="asyncio")
        )
        stopped = threading.Event()

        def exit_app(*_args):
            server.should_exit = True
            stopped.set()
            if icon is not None:
                icon.stop()

        app.state.desktop_control = {"instance": instance, "exit": exit_app}

        image = Image.new("RGBA", (64, 64), (247, 246, 242, 255))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((6, 6, 58, 58), radius=14, fill=(85, 113, 83, 255))
        draw.line((23, 19, 23, 44, 43, 44), fill="white", width=6)
        icon = pystray.Icon(
            "Legacy",
            image,
            "Legacy",
            menu=pystray.Menu(
                pystray.MenuItem("打开界面", lambda *_: webbrowser.open(url), default=True),
                pystray.MenuItem("退出", exit_app),
            ),
        )

        def serve():
            try:
                server.run(sockets=[sock])
            except BaseException:
                logging.exception("Legacy 服务启动失败")
            finally:
                stopped.set()
                if icon.visible:
                    icon.stop()

        thread = threading.Thread(target=serve, name="Legacy API")
        thread.start()
        deadline = time.monotonic() + 45
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not server.started:
            raise RuntimeError("本机服务未能启动。请检查用户数据目录的写权限与数据库状态。")
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"url": url, "instance": instance}), encoding="utf-8")
        temporary.replace(state_path)
        webbrowser.open(url)
        icon.run()
        server.should_exit = True
        thread.join(timeout=40)
        if thread.is_alive():
            raise RuntimeError("服务仍在清理任务，请稍后检查日志。")
    except BaseException as exc:
        logging.exception("Legacy 启动或退出失败")
        if server:
            server.should_exit = True
        if icon:
            icon.stop()
        if thread and thread.is_alive():
            thread.join(timeout=40)
        message(
            f"Legacy 无法正常运行：{exc}\n\n日志位置：{log_path}\n请检查目录权限；保留数据库和日志后再重试。"
        )
    finally:
        if owned:
            state_path.unlink(missing_ok=True)
        if lock:
            if owned:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            lock.close()
