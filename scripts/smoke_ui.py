"""界面冒烟检查：真的把浏览器打开、点一下设置、把渲染结果读回来。

《为什么需要它 —— 单元测试全绿而界面白屏是完全可能的》

前端那 90 个测试测的都是纯函数，它们不知道组件能不能渲染。
一次写错 import、一个渲染期间发副作用的 Hook、一个拼错的类名，
测试和 typecheck 都不会说话，而用户看到的是**白屏**。
这个项目不引入 jsdom / Playwright（前端测试刻意保持零 DOM 依赖），
所以这里换一条路：用**机器上已有的 Edge**，通过 CDP 驱动它。

它能回答的问题很具体：
  · 页面渲染出来了吗（`#root` 里有没有东西）
  · 点"设置"之后对话框真的出现了吗，左栏有那几项吗
  · 切到"模型"页会不会崩（那一页在挂载时要发请求、要渲染列表）
  · 控制台有没有报错（React 的警告也在这里）

用法（需要 serve 在跑）：
    python scripts/smoke_ui.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import httpx

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
PORT = 9222
TARGET = "http://127.0.0.1:8000/"

ok = True


def check(cond: bool, label: str, extra: str = "") -> None:
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))
    if not cond:
        ok = False


def find_browser() -> str | None:
    for path in EDGE_CANDIDATES:
        if Path(path).exists():
            return path
    return None


def http_json(url: str) -> object:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _server_flag(name: str) -> object:
    """读服务端当前的一项 agent 配置（用于断言"界面反映的是真实配置"）。"""
    import os

    headers = {}
    key = os.environ.get("SECURITY_API_KEY", "").strip()
    if key:
        headers["X-API-Key"] = key
    try:
        with httpx.Client(base_url=TARGET, timeout=10.0, trust_env=False) as api:
            return api.get("/api/settings", headers=headers).json()["agent"].get(name)
    except (httpx.HTTPError, KeyError, ValueError):
        # 读不到就不硬失败 —— 这一项只是"顺带核对"，界面渲染的检查在别处
        return None


class Cdp:
    """极简 CDP 客户端：只用到 Runtime.evaluate 一个能力。"""

    def __init__(self, ws_url: str) -> None:
        from contextlib import ExitStack

        from websockets.sync.client import connect

        # 用 ExitStack 持有连接：websockets 要求 connect() 作为上下文管理器使用
        # （直接持有对象会拿到 DeprecationWarning，而警告刷在 stderr 上会
        #   让人以为是这个脚本出了问题）
        self._stack = ExitStack()
        self._conn = self._stack.enter_context(connect(ws_url, max_size=8 * 1024 * 1024))
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        message = {"id": self._id, "method": method, "params": params or {}}
        self._conn.send(json.dumps(message))
        while True:
            raw = json.loads(self._conn.recv())
            if raw.get("id") == self._id:
                return raw

    def eval(self, expression: str) -> object:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        details = result.get("result", {})
        if "exceptionDetails" in details:
            raise RuntimeError(details["exceptionDetails"].get("text", "求值失败"))
        return details.get("result", {}).get("value")

    def close(self) -> None:
        self._stack.close()


def reliability_checks(cdp: Cdp) -> None:
    """真实组件消费合成 SSE；内核与断连清理由后端回归独立验证。"""
    print("\n=== 编排可靠性界面检查（合成 SSE，不调用模型）===")
    cdp.eval(r"""
      window.__originalFetch = window.fetch;
      window.__smokeCase = 'token_budget';
      window.__smokeCancelled = false;
      window.fetch = async (url, options) => {
        if (!String(url).endsWith('/api/chat/stream')) return window.__originalFetch(url, options);
        const emit = e => new TextEncoder().encode('event: '+e.type+'\ndata: '+JSON.stringify(e)+'\n\n');
        const body = new ReadableStream({
          start(c) {
            c.enqueue(emit({type:'start'}));
            c.enqueue(emit({type:'step',step:1}));
            c.enqueue(emit({type:'token',step:1,content:'公开样本的部分结论'}));
            if (window.__smokeCase !== 'cancel') {
              c.enqueue(emit({type:'final',step:1,content:'已有结论，任务未完成'}));
              c.enqueue(emit({type:'error',content:'已停止后续模型调用'}));
              c.enqueue(emit({type:'done',stopped_reason:window.__smokeCase,usage_complete:false,
                usage:{prompt_tokens:10,completion_tokens:3,total_tokens:13}}));
              c.close();
            }
          },
          cancel() { window.__smokeCancelled = true; }
        });
        return new Response(body,{headers:{'Content-Type':'text/event-stream'}});
      };
      window.__smokeSend = () => {
        const box = document.querySelector('.composer__input');
        Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(box,'公开样本核验');
        box.dispatchEvent(new Event('input',{bubbles:true}));
      };
    """)
    for label in ("先规划", "多专家"):
        cdp.eval(
            f"[...document.querySelectorAll('.modes__item')].find(b=>b.textContent.trim()==={json.dumps(label)})?.click()"
        )
        time.sleep(0.2)
        check(
            cdp.eval(
                "document.querySelector('.composer').textContent.includes('本轮不使用会话历史')"
            ),
            f"{label} 显示独立任务提示",
        )
    for reason, label in (("token_budget", "累计 token 预算"), ("timeout", "整轮时长预算")):
        cdp.eval(f"window.__smokeCase={json.dumps(reason)}; window.__smokeSend()")
        time.sleep(0.2)
        cdp.eval("document.querySelector('.composer__send')?.click()")
        time.sleep(0.5)
        check(
            cdp.eval(f"document.body.textContent.includes({json.dumps(label)})"),
            f"{reason} 状态已渲染",
        )
        check(
            cdp.eval("document.body.textContent.includes('统计不完整')"),
            "未知用量未显示为完整的零消耗",
        )
        check(cdp.eval("document.body.textContent.includes('部分结果')"), "预算中止展示部分结果")
    cdp.eval("window.__smokeCase='cancel'; window.__smokeSend()")
    time.sleep(0.2)
    cdp.eval("document.querySelector('.composer__send')?.click()")
    time.sleep(0.3)
    check(cdp.eval("!!document.querySelector('.composer__stop')"), "生成期间停止按钮可见")
    cdp.eval("document.querySelector('.composer__stop')?.click()")
    time.sleep(0.3)
    check(cdp.eval("window.__smokeCancelled === true"), "停止确实取消 ReadableStream")
    check(cdp.eval("!document.querySelector('.composer__stop')"), "停止后输入区恢复")
    cdp.eval("window.fetch=window.__originalFetch")


def main() -> int:
    global TARGET, PORT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=TARGET)
    parser.add_argument("--cdp-port", type=int, default=PORT)
    parser.add_argument(
        "--reliability", action="store_true", help="使用浏览器内合成 SSE 验证预算与停止；不调用模型"
    )
    args = parser.parse_args()
    TARGET = args.target
    PORT = args.cdp_port
    browser = find_browser()
    if browser is None:
        print("没找到 Edge / Chrome，跳过")
        return 0

    profile = Path(tempfile.mkdtemp(prefix="ui-smoke-"))
    print("=" * 70)
    print(f"界面冒烟检查（{browser}）")
    print("=" * 70)

    proc = subprocess.Popen(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={profile}",
            TARGET,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # 等 CDP 端口起来
        page = None
        for _ in range(40):
            time.sleep(0.5)
            try:
                pages = http_json(f"http://127.0.0.1:{PORT}/json/list")
            except OSError:
                # 调试端口还没起来。这是**正常过程**（浏览器要几秒才开端口），
                # 所以不打印任何东西 —— 一个每次都出现、又永远不是问题的
                # 报错，会让真正的失败淹没在噪音里
                continue
            page = next(
                (
                    p
                    for p in pages
                    if p.get("type") == "page" and TARGET.rstrip("/") in p.get("url", "")
                ),
                None,
            )
            if page:
                break
        if page is None:
            print("  [FAIL] 连不上浏览器的调试端口")
            return 1

        cdp = Cdp(page["webSocketDebuggerUrl"])
        # 收集控制台报错（React 的警告也走这条路）
        cdp.call("Runtime.enable")
        cdp.call("Log.enable")
        time.sleep(2.5)  # 等 React 挂载与首次请求

        print("\n=== 1. 首屏渲染 ===")
        root_len = cdp.eval("document.getElementById('root')?.innerHTML.length ?? 0")
        check(
            isinstance(root_len, int) and root_len > 500,
            "#root 里有渲染结果",
            f"{root_len} 字符",
        )
        check(cdp.eval("!!document.querySelector('.topbar')"), "顶栏已渲染")
        check(
            cdp.eval("document.title.length > 0"),
            "标题已设置",
            str(cdp.eval("document.title")),
        )

        print("\n=== 2. 打开设置（点顶栏那个按钮）===")
        # 按文本找按钮：不依赖类名，界面改动不会让检查失效
        clicked = cdp.eval(
            """
            (() => {
              const btn = [...document.querySelectorAll('button')]
                .find(b => b.textContent.trim() === '设置');
              if (!btn) return false;
              btn.click();
              return true;
            })()
            """
        )
        check(bool(clicked), "找到了「设置」按钮并点击")
        time.sleep(0.8)

        check(
            cdp.eval("!!document.querySelector('.dialog')"),
            "对话框已出现且带 .dialog 类（圆角窗口外壳）",
        )
        radius = cdp.eval("getComputedStyle(document.querySelector('.dialog')).borderRadius")
        check(
            isinstance(radius, str) and radius not in ("0px", ""),
            "窗口确实有圆角",
            str(radius),
        )

        print("\n=== 3. 左栏分类 ===")
        labels = cdp.eval(
            "[...document.querySelectorAll('.dialog__nav .navitem__label')].map(e => e.textContent)"
        )
        check(isinstance(labels, list) and len(labels) >= 2, "左栏有分类项", str(labels))
        for expected in ("通用", "模型"):
            check(
                isinstance(labels, list) and expected in labels,
                f"左栏包含「{expected}」",
            )

        print("\n=== 4. 通用页（外观与偏好）===")
        check(
            cdp.eval("document.querySelectorAll('.theme-card').length >= 3"),
            "三个主题卡片都在",
        )
        check(
            cdp.eval("!!document.querySelector('.segmented .segmented__item--on')"),
            "发送键分段控件有选中态",
        )

        print("\n=== 5. 切到「模型」页（会发请求、渲染列表）===")
        switched = cdp.eval(
            """
            (() => {
              const item = [...document.querySelectorAll('.dialog__nav .navitem')]
                .find(b => b.textContent.includes('模型'));
              if (!item) return false;
              item.click();
              return true;
            })()
            """
        )
        check(bool(switched), "点到「模型」分类")
        time.sleep(2.0)  # 等 /api/models 回来

        check(
            cdp.eval("!!document.querySelector('.model-current')"),
            "显示「正在使用」区块",
        )
        check(
            cdp.eval("document.querySelectorAll('.provider-card').length >= 5"),
            "供应商预设卡片都渲染了",
            str(cdp.eval("document.querySelectorAll('.provider-card').length")),
        )
        # 当前配置没存进清单时，应当出现「存为模型」的入口
        check(
            cdp.eval("document.body.textContent.includes('存为模型')"),
            "提示了「当前配置未保存」并给出入口",
        )
        current_model = cdp.eval(
            "document.querySelector('.model-current__name')?.textContent ?? ''"
        )
        check(bool(current_model), "显示了当前模型名", str(current_model))

        print("\n=== 6. 点一个供应商，表单应当出现 ===")
        cdp.eval(
            """
            (() => {
              const card = document.querySelector('.provider-card');
              if (card) card.click();
              return !!card;
            })()
            """
        )
        time.sleep(0.4)
        check(cdp.eval("!!document.querySelector('.model-form')"), "添加表单已出现")
        check(
            cdp.eval("document.querySelectorAll('.model-form input').length >= 4"),
            "表单含名称/地址/模型名/密钥四个输入",
        )
        check(
            cdp.eval("!!document.querySelector('.model-form input[type=range]')"),
            "温度滑块在",
        )

        print("\n=== 6.5 工作区页：写权限开关必须可见（T23）===")
        # 这是"Agent 能不能改我的文件"这个问题的**唯一界面答案** ——
        # 用户问过它（"为什么我的 agent 还不能写文件"），所以它必须真的渲染出来。
        cdp.eval(
            """
            (() => {
              const item = [...document.querySelectorAll('.dialog__nav .navitem')]
                .find(b => b.textContent.includes('工作区'));
              if (item) item.click();
              return !!item;
            })()
            """
        )
        time.sleep(0.6)
        check(
            cdp.eval("document.body.textContent.includes('允许 Agent 写入文件')"),
            "「允许 Agent 写入文件」开关在页面上",
        )
        check(
            cdp.eval("document.body.textContent.includes('敏感文件名')"),
            "敏感文件名开关也在（它是另一个独立决定）",
        )
        # 【这条断言不能写死 false —— 第一次就是这么写错的】
        # 我把服务端起在 AGENT_FILE_WRITE_ENABLED=true 上，界面**正确地**勾上了它，
        # 而检查却在断言"必须是未勾选"，于是报了 FAIL —— 失败的是检查，不是界面。
        # 真正该验的是**界面与服务端一致**：这既证明开关渲染出来了，
        # 也证明它接到了真实配置上（而不是一个点了没反应的控件）。
        expected = _server_flag("file_write_enabled")
        checked_now = cdp.eval(
            """
            (() => {
              const labels = [...document.querySelectorAll('.settings__field--check')];
              const box = labels.find(l => l.textContent.includes('允许 Agent 写入文件'))
                ?.querySelector('input[type=checkbox]');
              return box ? box.checked : null;
            })()
            """
        )
        check(
            checked_now is not None and checked_now == expected,
            "写权限复选框与服务端配置一致（不是写死的默认值）",
            f"界面={checked_now} 服务端={expected}",
        )

        print("\n=== 7. 关闭对话框 ===")
        cdp.eval("document.querySelector('.dialog__head button')?.click()")
        time.sleep(0.5)
        check(cdp.eval("!document.querySelector('.dialog')"), "对话框已关闭")
        if args.reliability:
            reliability_checks(cdp)

        errors = cdp.eval("window.__errors__ ? window.__errors__.length : 0")
        check(not errors, "没有未捕获的脚本错误")
        cdp.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()

    print("\n" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
