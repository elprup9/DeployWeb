#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeployWeb —— 把本地静态网页一键穿透部署到公网（基于 cloudflared Quick Tunnel）

用法：
    python deploy_web.py                # 交互式输入路径
    python deploy_web.py D:\\mysite      # 直接指定路径（也支持把文件夹拖到 exe 上）

特性：
    * 完全免费，不需要 Cloudflare 账号，不需要域名（公网地址形如 https://xxx.trycloudflare.com）
    * 整个文件夹搬到任何路径都能用（运行时解析程序所在目录，无任何硬编码绝对路径）
    * 自动在目录中定位 index.html，并以它所在目录作为网页根目录，保证相对路径资源可加载
    * 退出时确保清理 cloudflared 子进程，不留残余

依赖：仅 Python 标准库。
"""

from __future__ import annotations

import argparse
import atexit
import ctypes
import functools
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import deque
from pathlib import Path

APP_NAME = "DeployWeb"
APP_VERSION = "1.0"

# 同目录下按顺序查找的 cloudflared 文件名
CLOUDFLARED_NAMES = (
    "cloudflared-windows-amd64.exe",
    "cloudflared.exe",
    "cloudflared",
)

# cloudflared 输出的公网地址形如 https://olive-keith-feels-rides.trycloudflare.com
TUNNEL_URL_RE = re.compile(r"https://([a-z0-9][a-z0-9-]*[a-z0-9])\.trycloudflare\.com")

# 这些是 Cloudflare 自己的服务子域名，不是隧道地址。
# 踩过的坑：隧道申请失败时 cloudflared 会打印
#   failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": ...
# 如果不排除，就会被误当成公网地址，造成"假成功"。
TUNNEL_HOST_BLACKLIST = {
    "api", "www", "dns", "update", "cloudflared", "static", "cdn",
    "docs", "dash", "one", "blog", "developers", "trycloudflare",
}


def extract_tunnel_url(line: str) -> str | None:
    """从一行日志中提取隧道地址，过滤掉 Cloudflare 自身的服务域名。"""
    for m in TUNNEL_URL_RE.finditer(line):
        host = m.group(1)
        if host in TUNNEL_HOST_BLACKLIST:
            continue
        if "-" not in host:
            # Quick Tunnel 名称形如 olive-keith-feels-rides，一定带连字符
            continue
        return m.group(0)
    return None

# 扫描 index.html 时跳过的目录
SKIP_DIRS = {
    "node_modules", ".git", ".svn", ".hg", "__pycache__", ".idea", ".vscode",
    "venv", ".venv", "env", ".env", ".cache", ".next", ".nuxt", "vendor",
}

# 判定"网络连不上"的日志特征
NETWORK_ERROR_HINTS = (
    "failed to request quick tunnel",
    "context deadline exceeded",
    "no such host",
    "connection refused",
    "connection reset",
    "i/o timeout",
    "tls handshake timeout",
    "network is unreachable",
    "failed to dial",
    "unable to connect",
)

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
IS_WINDOWS = os.name == "nt"


# --------------------------------------------------------------------------
# 终端输出
# --------------------------------------------------------------------------

def _enable_ansi() -> bool:
    """在 Windows 控制台上开启 ANSI 转义支持。"""
    if not IS_WINDOWS:
        return sys.stdout.isatty()
    if not sys.stdout.isatty():
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


USE_COLOR = _enable_ansi()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def cyan(t: str) -> str:
    return _c(t, "36")


def green(t: str) -> str:
    return _c(t, "32")


def yellow(t: str) -> str:
    return _c(t, "33")


def red(t: str) -> str:
    return _c(t, "31")


def bold(t: str) -> str:
    return _c(t, "1")


def dim(t: str) -> str:
    return _c(t, "90")


def say(msg: str = "") -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    print(f"{green('[OK]')} {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"{yellow('[!]')} {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"{red('[x]')} {msg}", flush=True)


def info(msg: str) -> None:
    print(f"{cyan('[*]')} {msg}", flush=True)


# --------------------------------------------------------------------------
# 清理机制：atexit + try/finally + 控制台关闭事件 三重保障
# --------------------------------------------------------------------------

_cleanup_lock = threading.Lock()
_cleanup_done = False
_cleanup_fns: list = []
_console_handler_ref = None  # 必须持有引用，否则会被 GC 回收导致回调失效


def register_cleanup(fn) -> None:
    _cleanup_fns.append(fn)


def run_cleanup(verbose: bool = False) -> None:
    """幂等清理，重复调用只生效一次。"""
    global _cleanup_done
    with _cleanup_lock:
        if _cleanup_done:
            return
        _cleanup_done = True
    for fn in reversed(_cleanup_fns):
        try:
            fn()
        except Exception as exc:  # 清理阶段绝不抛异常
            if verbose:
                print(f"  (清理项失败: {exc})", flush=True)


def _install_console_close_handler() -> None:
    """捕获"控制台正在关闭"事件，抢在进程消失前清理子进程。

    注意只接管关闭类事件：Ctrl+C / Ctrl+Break 要留给 Python 抛 KeyboardInterrupt，
    那样才能走正常退出流程（打印收尾信息、执行 finally）。
    """
    global _console_handler_ref
    if not IS_WINDOWS:
        return
    try:
        handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
        close_events = (2, 5, 6)  # CTRL_CLOSE_EVENT / CTRL_LOGOFF_EVENT / CTRL_SHUTDOWN_EVENT

        def _handler(ctrl_type):
            if ctrl_type in close_events:
                run_cleanup()
            return 0  # 交回默认处理

        _console_handler_ref = handler_type(_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler_ref, True)
    except Exception:
        pass


_job_handles: list = []


def bind_child_lifetime(proc: subprocess.Popen) -> None:
    """把子进程放进 Job Object，父进程无论如何消失，Windows 都会连带结束它。

    这是唯一能覆盖"任务管理器强杀 / 程序崩溃"的兜底手段 —— 那些情况下
    atexit 和 finally 都没有机会执行，如果不管，cloudflared 会变成孤儿进程
    继续占着公网入口。
    """
    if not IS_WINDOWS:
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        info = EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return
        if not kernel32.AssignProcessToJobObject(job, ctypes.c_void_p(int(proc._handle))):
            kernel32.CloseHandle(job)
            return
        _job_handles.append(job)  # 句柄必须活到进程结束，否则 Job 提前关闭会杀掉子进程
    except Exception:
        pass


# --------------------------------------------------------------------------
# 路径解析（便携性核心）
# --------------------------------------------------------------------------

def app_dir() -> Path:
    """程序所在目录。

    打包后必须用 sys.executable —— onefile 模式下 __file__ 指向临时解压目录
    (_MEIPASS)，退出即删，用它定位 cloudflared 必然失败。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_cloudflared(explicit: str | None = None) -> Path | None:
    """按 显式指定 → 程序目录 → PATH → 当前目录 的顺序查找 cloudflared。"""
    if explicit:
        p = clean_user_path(explicit)
        return p if p.is_file() else None

    base = app_dir()
    for name in CLOUDFLARED_NAMES:
        p = base / name
        if p.is_file():
            return p.resolve()

    for name in CLOUDFLARED_NAMES:
        found = shutil.which(name)
        if found:
            return Path(found).resolve()

    for name in CLOUDFLARED_NAMES:
        p = Path.cwd() / name
        if p.is_file():
            return p.resolve()

    return None


def clean_user_path(raw: str) -> Path:
    """清洗用户输入的路径。

    处理 Windows 上两种常见污染：资源管理器"复制为路径"和拖拽进控制台
    都会给路径套上英文双引号。
    """
    s = (raw or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    s = os.path.expandvars(os.path.expanduser(s))
    return Path(s)


def ask(prompt: str, default: str = "") -> str | None:
    """安全读取一行输入；EOF 返回 None（视为退出）。"""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        say()
        return None


# --------------------------------------------------------------------------
# 定位 index.html 与网页根目录
# --------------------------------------------------------------------------

def scan_index_files(root: Path, max_depth: int = 4, limit: int = 30) -> list[Path]:
    """在 root 下广度优先查找 index.html，优先返回层级最浅的。"""
    found: list[Path] = []
    base_depth = len(root.parts)

    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        depth = len(current.parts) - base_depth

        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = sorted(
                d for d in dirnames
                if d.lower() not in SKIP_DIRS and not d.startswith(".")
            )

        if any(fn.lower() == "index.html" for fn in filenames):
            found.append(current / "index.html")
            if len(found) >= limit:
                break

    found.sort(key=lambda p: (len(p.parts), str(p).lower()))
    return found


def count_files(root: Path, limit: int = 20000) -> int:
    total = 0
    for _dirpath, _dirnames, filenames in os.walk(root):
        total += len(filenames)
        if total >= limit:
            return limit
    return total


def resolve_web_root(raw_path: str) -> Path | None:
    """由用户输入解析出最终的网页根目录，失败返回 None。"""
    path = clean_user_path(raw_path)

    if not path.exists():
        fail(f"路径不存在：{path}")
        return None

    # 输入的是文件
    if path.is_file():
        if path.name.lower() != "index.html":
            warn(f"你输入的是文件 {path.name}，将改为部署它所在的目录。")
        root = path.parent
        if not (root / "index.html").is_file():
            warn(f"该目录下没有 index.html：{root}")
        return root

    # 输入的是目录：优先看当前层
    direct = path / "index.html"
    if direct.is_file():
        return path.resolve()

    info("当前目录没有 index.html，正在向下扫描（最多 4 层）…")
    candidates = scan_index_files(path)

    if not candidates:
        fail(f"在 {path} 及其子目录中都没找到 index.html。")
        ans = ask("是否仍以该目录为根部署？（将开启目录文件浏览）[y/N]: ")
        if ans is not None and ans.strip().lower() in ("y", "yes"):
            return path.resolve()
        return None

    if len(candidates) == 1:
        root = candidates[0].parent
        ok(f"找到唯一入口，网页根目录已设为：{root}")
        return root.resolve()

    # 多个候选，交给用户选
    say()
    warn(f"找到 {len(candidates)} 个 index.html，请选择要部署哪一个：")
    for i, p in enumerate(candidates, 1):
        try:
            rel = p.parent.relative_to(path)
            label = str(rel) if str(rel) != "." else "(当前目录)"
        except ValueError:
            label = str(p.parent)
        say(f"   [{i}] {label}")

    while True:
        ans = ask(f"请输入序号 [1-{len(candidates)}]，或 q 退出: ")
        if ans is None:
            return None
        ans = ans.strip()
        if ans.lower() == "q":
            return None
        if ans.isdigit() and 1 <= int(ans) <= len(candidates):
            root = candidates[int(ans) - 1].parent
            ok(f"网页根目录已设为：{root}")
            return root.resolve()
        warn("输入无效，请重新输入。")


# --------------------------------------------------------------------------
# 本地 HTTP 服务
# --------------------------------------------------------------------------

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """在标准静态文件服务基础上：安静日志 + 补全 MIME + 可选 SPA 回退。"""

    server_version = f"{APP_NAME}/{APP_VERSION}"
    sys_version = ""
    verbose = False
    spa_fallback = False

    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".wasm": "application/wasm",
        ".mjs": "text/javascript",
        ".js": "text/javascript",
        ".json": "application/json",
        ".webp": "image/webp",
        ".avif": "image/avif",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".ttf": "font/ttf",
        ".svg": "image/svg+xml",
        ".webmanifest": "application/manifest+json",
    }

    def log_message(self, fmt, *args):  # noqa: A003
        if self.verbose:
            print(f"  {dim('[http]')} {fmt % args}", flush=True)

    def send_error(self, code, message=None, explain=None):
        # SPA 回退：路径无扩展名却 404 时，交还 index.html 由前端路由接管
        if self.spa_fallback and code == 404 and not getattr(self, "_spa_retry", False):
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
            if "." not in os.path.basename(path):
                self._spa_retry = True
                self.path = "/index.html"
                return self.do_GET()
        return super().send_error(code, message, explain)


def make_handler(root: Path, spa: bool, verbose: bool):
    class BoundHandler(QuietHandler):
        pass

    BoundHandler.spa_fallback = spa
    BoundHandler.verbose = verbose
    return functools.partial(BoundHandler, directory=str(root))


class LocalServer:
    """本地静态文件服务，端口由系统分配，避免 8080 被占用的老问题。"""

    def __init__(self, root: Path, port: int = 0, spa: bool = False, verbose: bool = False):
        handler = make_handler(root, spa, verbose)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.port = self.httpd.server_address[1]
        self.root = root
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# cloudflared 隧道
# --------------------------------------------------------------------------

def kill_process_tree(pid: int) -> None:
    """Windows 上兜底强杀整棵进程树。"""
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW, timeout=10,
            )
            return
        except Exception:
            pass
    try:
        os.kill(pid, 9)
    except Exception:
        pass


class Tunnel:
    """包装 cloudflared 子进程，从其日志中提取公网地址。"""

    def __init__(self, exe: Path, port: int, isolate_config: bool = False,
                 protocol: str | None = None, verbose: bool = False):
        self.exe = exe
        self.port = port
        self.isolate_config = isolate_config
        self.protocol = protocol
        self.verbose = verbose
        self.proc: subprocess.Popen | None = None
        self.url: str | None = None
        self.log_tail: deque[str] = deque(maxlen=40)
        self._url_event = threading.Event()
        self._out_lock = threading.Lock()

    def start(self) -> None:
        args = [str(self.exe)]
        if self.isolate_config:
            # NUL 是 Windows 的空设备，等价于 Linux 的 /dev/null，
            # 用它把用户自己的 ~/.cloudflared/config.yml 挡在外面。
            args += ["--config", "NUL"]
        args += [
            "tunnel",
            "--url", f"http://127.0.0.1:{self.port}",
            "--no-autoupdate",
        ]
        if self.protocol:
            args += ["--protocol", self.protocol]

        if self.verbose:
            say(dim("  $ " + " ".join(args)))

        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
        bind_child_lifetime(self.proc)

        threading.Thread(target=self._read_output, daemon=True).start()

    def _read_output(self) -> None:
        """持续消费子进程输出。

        拿到 URL 之后仍然必须继续读 —— 否则管道缓冲区写满会让 cloudflared 阻塞，
        隧道随即掉线。这是最容易踩的坑。
        """
        stream = self.proc.stdout if self.proc else None
        if stream is None:
            self._url_event.set()
            return
        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                line = line.rstrip("\r\n")
                with self._out_lock:
                    self.log_tail.append(line)
                    if self.verbose:
                        print(f"  {dim('[cf]')} {line}", flush=True)
                    if self.url is None:
                        found = extract_tunnel_url(line)
                        if found:
                            self.url = found
                            self._url_event.set()
        except Exception:
            pass
        finally:
            self._url_event.set()  # 无论何种原因结束，都要唤醒等待方

    def wait_for_url(self, timeout: float) -> str | None:
        self._url_event.wait(timeout)
        return self.url

    def tail(self, n: int = 12) -> str:
        with self._out_lock:
            lines = list(self.log_tail)[-n:]
        return "\n".join("    " + ln for ln in lines)

    def looks_like_network_problem(self) -> bool:
        with self._out_lock:
            blob = "\n".join(self.log_tail).lower()
        return any(h in blob for h in NETWORK_ERROR_HINTS)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            # 先给个优雅退出的机会，让 cloudflared 自己断开边缘连接
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                kill_process_tree(proc.pid)
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass
            except Exception:
                kill_process_tree(proc.pid)
        elif IS_WINDOWS:
            # 进程已退出，但可能留有子进程
            kill_process_tree(proc.pid)
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass
        self.proc = None


# --------------------------------------------------------------------------
# 辅助功能
# --------------------------------------------------------------------------

def copy_to_clipboard(text: str) -> bool:
    """用 Windows 自带的 clip 命令写剪贴板，避免引入第三方依赖。"""
    if not IS_WINDOWS:
        return False
    try:
        r = subprocess.run(
            ["clip"],
            input=text.encode("ascii", "ignore"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def check_public_url(url: str, attempts: int = 4, delay: float = 2.0) -> int | None:
    """从本机请求一次公网地址，确认边缘已把流量转发回来。"""
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code  # 能拿到 HTTP 状态码就说明链路是通的
        except Exception:
            if i < attempts - 1:
                time.sleep(delay)
    return None


def resolve_local(host: str) -> list[str]:
    """用系统 DNS 解析域名。"""
    try:
        return socket.gethostbyname_ex(host)[2]
    except Exception:
        return []


def resolve_doh(host: str, timeout: float = 6.0) -> list[str]:
    """用国内公共 DoH 解析，绕开本机可能故障的 DNS。

    实测背景：部分国内网络（含某些路由器/运营商 DNS）会对 *.trycloudflare.com
    子域名错误地返回 NXDOMAIN，而该域名在全球 DNS 里其实是正常的。这里用
    AliDNS / DNSPod 的 DoH 接口交叉验证，好把"隧道坏了"和"你本机 DNS 坏了"区分开。
    """
    endpoints = (
        "https://dns.alidns.com/resolve?name={host}&type=1",
        "https://doh.pub/dns-query?name={host}&type=A",
    )
    for tpl in endpoints:
        try:
            req = urllib.request.Request(
                tpl.format(host=urllib.parse.quote(host)),
                headers={"accept": "application/dns-json",
                         "User-Agent": f"{APP_NAME}/{APP_VERSION}"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            ips = [a.get("data") for a in (data.get("Answer") or []) if a.get("type") == 1]
            ips = [ip for ip in ips if ip]
            if ips:
                return ips
        except Exception:
            continue
    return []


def verify_public_url(url: str) -> tuple[str, object]:
    """验证公网可达性。

    返回 (结果, 详情)：
        ("ok", 状态码)        —— 一切正常
        ("dns", [公网解析到的IP]) —— 隧道是好的，是本机 DNS 解析不了
        ("unreachable", None) —— 暂时访问不到，可能是边缘尚未生效
    """
    host = urllib.parse.urlsplit(url).hostname or ""

    if resolve_local(host):
        status = check_public_url(url)
        return ("ok", status) if status is not None else ("unreachable", None)

    # 本机 DNS 解析不了。用公共 DoH 交叉验证，区分"隧道没生效"和"你本机 DNS 有问题"。
    # 刚创建时记录可能还没传播到公共 DNS，所以多试几次。
    for i in range(4):
        ips = resolve_doh(host)
        if ips:
            return "dns", ips
        if i < 3:
            time.sleep(2.5)
    return "unreachable", None


def print_url_box(url: str, origin: str, root: Path) -> None:
    line = "=" * 68
    say()
    say(bold(line))
    say(bold(f"  公网地址   {green(url)}"))
    say(f"  本地地址   {origin}")
    say(f"  网页根目录 {root}")
    say(bold(line))


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog=APP_NAME,
        description="把本地静态网页一键穿透部署到公网（基于 cloudflared Quick Tunnel，免费无需域名）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            f"  {APP_NAME}.exe                    交互式输入路径\n"
            f"  {APP_NAME}.exe D:\\mysite          直接部署指定目录\n"
            f"  {APP_NAME}.exe D:\\mysite --spa    单页应用(SPA)路由回退\n"
        ),
    )
    p.add_argument("path", nargs="?", help="网页目录或 index.html 的路径；留空则运行时提示输入")
    p.add_argument("--port", type=int, default=0,
                   help="本地服务端口，默认 0 表示由系统自动分配空闲端口")
    p.add_argument("--spa", action="store_true",
                   help="单页应用模式：无扩展名的 404 路径回退到 index.html")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="显示 cloudflared 与 HTTP 请求的完整日志")
    p.add_argument("--open", action="store_true",
                   help="部署成功后自动用默认浏览器打开公网地址")
    p.add_argument("--cloudflared", metavar="PATH", default=None,
                   help="手动指定 cloudflared 可执行文件路径")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    return p.parse_args()


def prompt_for_path() -> str | None:
    say()
    info("请输入要部署的网页路径（目录或 index.html 均可），直接回车确认。")
    say(dim("   提示：可以直接把文件夹从资源管理器拖进这个窗口，路径带引号也没关系。"))
    while True:
        ans = ask(cyan("路径 > "))
        if ans is None:
            return None
        ans = ans.strip()
        if ans.lower() in ("q", "quit", "exit"):
            return None
        if not ans:
            warn("没有输入内容，请粘贴或拖入一个路径（输入 q 退出）。")
            continue
        return ans


def resolve_cloudflared(explicit: str | None) -> Path | None:
    exe = find_cloudflared(explicit)
    if exe:
        return exe
    fail("没有找到 cloudflared 可执行文件。")
    say(f"   请把 {CLOUDFLARED_NAMES[0]} 放到本程序同一目录下：")
    say(f"   {app_dir()}")
    while True:
        ans = ask("或手动输入它的完整路径（q 退出）: ")
        if ans is None or ans.strip().lower() == "q":
            return None
        exe = find_cloudflared(ans.strip())
        if exe:
            return exe
        fail("该路径下没有可执行文件，请重试。")


def needs_config_isolation() -> bool:
    """判断是否需要屏蔽用户自己的 cloudflared 配置。

    cloudflared 默认会读取 ~/.cloudflared/config.yml，一旦该文件存在，
    Quick Tunnel 会被破坏（隧道建得起来，但访问一律返回 404）。
    只有检测到这类文件时才加 --config NUL 隔离，平时保持命令行干净。
    """
    home = Path.home()
    for sub in (".cloudflared", ".cloudflare-warp", "cloudflare-warp"):
        for name in ("config.yml", "config.yaml"):
            try:
                if (home / sub / name).is_file():
                    return True
            except OSError:
                continue
    return False


def wait_with_spinner(tunnel: Tunnel, timeout: float) -> str | None:
    """等待公网地址，同时给用户一点进度反馈。"""
    frames = "|/-\\"
    deadline = time.time() + timeout
    idx = 0
    while time.time() < deadline:
        if tunnel.url:
            say()
            return tunnel.url
        if not tunnel.alive():
            say()
            return None
        remaining = int(deadline - time.time())
        sys.stdout.write(f"\r  {frames[idx % 4]} 正在向 Cloudflare 申请公网地址… {remaining}s ")
        sys.stdout.flush()
        idx += 1
        time.sleep(0.3)
    say()
    return tunnel.url


def main() -> int:
    try:  # 输出被重定向时，避免个别字符触发编码异常
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass

    atexit.register(run_cleanup)
    _install_console_close_handler()

    args = parse_args()

    say()
    say(bold(f"  {APP_NAME} v{APP_VERSION}") + dim("  —— 本地网页一键穿透部署"))
    say(dim("  基于 Cloudflare Quick Tunnel：免费、无需账号、无需域名"))

    # 1) 定位 cloudflared
    cf_exe = resolve_cloudflared(args.cloudflared)
    if not cf_exe:
        return 1
    info(f"cloudflared: {cf_exe}")

    # 2) 确定网页根目录
    raw = args.path if args.path else prompt_for_path()
    if raw is None:
        say("已取消。")
        return 0

    root = resolve_web_root(raw)
    if root is None:
        return 1

    n_files = count_files(root)
    ok(f"网页根目录：{root}")
    info(f"共 {n_files}{'+' if n_files >= 20000 else ''} 个文件待部署")

    # 3) 启动本地服务
    try:
        server = LocalServer(root, port=args.port, spa=args.spa, verbose=args.verbose)
    except OSError as exc:
        fail(f"启动本地服务失败：{exc}")
        return 1
    server.start()
    register_cleanup(server.stop)
    ok(f"本地服务已就绪：{server.origin}")

    isolate = needs_config_isolation()
    if isolate:
        info("检测到本机已有 cloudflared 配置，将自动隔离以免影响 Quick Tunnel。")

    # 4) 拉起隧道，失败时给出针对性的提示并允许重试
    tunnel: Tunnel | None = None
    url: str | None = None
    attempt = 0
    timeout = 45.0

    while True:
        attempt += 1
        # 第一次用默认协议（QUIC 优先），失败后降级为 http2
        protocol = None if attempt == 1 else "http2"
        label = "默认(QUIC 优先)" if protocol is None else "http2"
        info(f"第 {attempt} 次尝试建立隧道，协议：{label}")

        tunnel = Tunnel(cf_exe, server.port, isolate_config=isolate,
                        protocol=protocol, verbose=args.verbose)
        register_cleanup(tunnel.stop)
        try:
            tunnel.start()
        except OSError as exc:
            fail(f"无法启动 cloudflared：{exc}")
            return 1

        url = wait_with_spinner(tunnel, timeout)
        if url:
            break

        # 失败：打印日志尾部辅助排查
        fail("未能在限定时间内取得公网地址。")
        tail = tunnel.tail()
        if tail:
            say(dim("  ---- cloudflared 日志 ----"))
            say(tail)
            say(dim("  --------------------------"))

        if tunnel.looks_like_network_problem():
            say()
            warn("看起来是网络无法连接 Cloudflare 边缘节点。")
            warn("注意：cloudflared 不读取 HTTP_PROXY / HTTPS_PROXY 环境变量，")
            warn("      普通 HTTP 代理对它无效。请开启「TUN 模式 / 全局模式」代理，")
            warn("      或切换到手机热点等其它网络后重试。")
        else:
            warn("可尝试重试，或加 -v 参数查看完整日志。")

        tunnel.stop()
        ans = ask(yellow("准备好后按回车重试，或输入 q 退出: "))
        if ans is None or ans.strip().lower() == "q":
            say("已取消。")
            return 1

    # 5) 成功
    print_url_box(url, server.origin, root)

    if copy_to_clipboard(url):
        info("公网地址已复制到剪贴板。")

    info("正在验证公网可达性…")
    result, detail = verify_public_url(url)
    if result == "ok":
        ok(f"公网访问正常（HTTP {detail}）—— 现在就可以把地址发给别人了。")
    elif result == "dns":
        # 隧道没问题，是本机 DNS 解析不了这个域名（国内部分网络会误返回 NXDOMAIN）
        say()
        warn("隧道已经建好了，但你本机 DNS 解析不了这个域名。")
        say(f"   已通过公共 DoH 交叉验证，该域名在全球 DNS 中是正常的：{', '.join(detail)}")
        say(f"   {bold('解决办法：把本机 DNS 改成 223.5.5.5（阿里）或 119.29.29.29（腾讯）')}")
        say(dim("   Windows 设置 → 网络和 Internet → 你的网卡 → 编辑 DNS 服务器 → 手动 → IPv4 填 223.5.5.5"))
        say(dim("   这只是你本机 DNS 的问题，别人用正常 DNS 一般能直接打开。"))
    else:
        warn("本机暂时还访问不到该地址，Cloudflare 边缘通常需要几秒钟生效，稍后重试即可。")

    if args.open:
        webbrowser.open(url)

    say()
    say(dim("  提示：这个地址每次重启都会变；关闭本窗口或按 Ctrl+C 即可停止部署。"))

    # 6) 保持运行，顺便盯着 cloudflared，掉了就报出来
    try:
        while True:
            time.sleep(1)
            if tunnel and not tunnel.alive():
                say()
                fail("cloudflared 进程已退出，隧道中断。")
                tail = tunnel.tail(8)
                if tail:
                    say(tail)
                return 1
    except KeyboardInterrupt:
        say()
        info("收到 Ctrl+C，正在停止…")

    return 0


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        say()
        info("已中断。")
        code = 130
    finally:
        run_cleanup()
        say(dim("  已停止：隧道关闭，本地服务已退出。"))
    sys.exit(code)
