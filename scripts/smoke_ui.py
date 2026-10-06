"""用已有 Edge + CDP 验证真实组件；--visual 全程使用公开合成 API/SSE。

先启动服务，再运行 .venv\\Scripts\\python.exe -X utf8 scripts\\smoke_ui.py。
--visual --screenshots-dir docs/screenshots 会检查多种布局并保存截图。
--reliability 仅拦截聊天 SSE，其他接口仍读取当前服务，不修改配置。
不依赖 jsdom / Playwright；报错监听在页面挂载之前安装，避免白屏漏检。
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import httpx
from websockets.exceptions import ConnectionClosed

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
PORT = 9222
TARGET = "http://127.0.0.1:8000/"
ok = True

ERROR_CAPTURE = r"""
(() => {
  window.__errors__ = [];
  const record = (kind,value) => window.__errors__.push({kind,message:String(value)});
  addEventListener('error',e=>record('error',e.message));
  addEventListener('unhandledrejection',e=>record('unhandledrejection',e.reason?.message || e.reason));
  const original = console.error.bind(console);
  console.error = (...args) => {record('console.error',args.map(a=>String(a)).join(' '));original(...args);};
})();
"""


def check(cond: bool, label: str, extra: str = "") -> None:
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))
    if not cond:
        ok = False


def find_browser() -> str | None:
    return next((path for path in EDGE_CANDIDATES if Path(path).exists()), None)


def http_json(url: str) -> object:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _server_flag(name: str) -> object:
    import os

    key = os.environ.get("SECURITY_API_KEY", "").strip()
    headers = {"X-API-Key": key} if key else {}
    try:
        with httpx.Client(base_url=TARGET, timeout=10.0, trust_env=False) as api:
            return api.get("/api/settings", headers=headers).json()["agent"].get(name)
    except (httpx.HTTPError, KeyError, ValueError):
        return None


def button_expression(label: str, *, scope: str = "document") -> str:
    """Prefer accessible names, then exact visible text; never partial-match Settings."""
    name = json.dumps(label, ensure_ascii=False)
    return (
        f"[...{scope}.querySelectorAll('button')].find(b=>"
        f"b.getAttribute('aria-label')==={name}||b.textContent.trim()==={name}||"
        f"b.querySelector?.('.navitem__label')?.textContent.trim()==={name})"
    )


class Cdp:
    def __init__(self, ws_url: str) -> None:
        from contextlib import ExitStack

        from websockets.sync.client import connect

        self._stack = ExitStack()
        self._conn = self._stack.enter_context(
            connect(ws_url, max_size=16 * 1024 * 1024)
        )
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self._conn.send(
            json.dumps({"id": self._id, "method": method, "params": params or {}})
        )
        while True:
            raw = json.loads(self._conn.recv(timeout=30))
            if raw.get("id") == self._id:
                if "error" in raw:
                    raise RuntimeError(f"CDP {method}: {raw['error'].get('message')}")
                return raw

    def eval(self, expression: str) -> object:
        response = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        details = response.get("result", {})
        if "exceptionDetails" in details:
            info = details["exceptionDetails"]
            raise RuntimeError(
                info.get("exception", {}).get("description") or info.get("text")
            )
        return details.get("result", {}).get("value")

    def wait(self, expression: str, timeout: float = 5) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.eval(expression):
                return True
            time.sleep(0.08)
        return False

    def click(self, label: str, *, scope: str = "document") -> bool:
        found = self.eval(
            f"(()=>{{const b={button_expression(label, scope=scope)};"
            "if(!b||b.disabled)return false;b.focus();b.click();return true;})()"
        )
        time.sleep(0.15)
        return bool(found)

    def viewport(self, width: int, height: int) -> None:
        self.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
        )
        time.sleep(0.2)

    def screenshot(self, directory: Path, name: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        raw = self.call(
            "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False}
        )
        (directory / f"{name}.png").write_bytes(base64.b64decode(raw["result"]["data"]))
        print(f"  [IMAGE] {directory / (name + '.png')}")

    def reload(self) -> None:
        # New-document listeners start a new array. Check before discarding it.
        errors = self.eval("window.__errors__")
        check(
            isinstance(errors, list) and not errors,
            "刷新前没有被遗忘的脚本错误",
            str(errors),
        )
        self.call("Page.reload")
        check(self.wait("!!document.querySelector('.composer')"), "刷新后重新挂载")

    def close(self) -> None:
        self._stack.close()


def composer_expression() -> str:
    return "document.querySelector('textarea[aria-label=\"消息\"]') || document.querySelector('.composer__input')"


def fill(cdp: Cdp, text: str) -> None:
    value = json.dumps(text, ensure_ascii=False)
    cdp.eval(
        f"(()=>{{const box={composer_expression()};"
        f"Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(box,{value});"
        "box.dispatchEvent(new Event('input',{bubbles:true}));box.focus();})()"
    )
    time.sleep(0.1)


def send(cdp: Cdp, text: str = "请核验 24 × 24，并整理一个清晰的行动计划。") -> None:
    fill(cdp, text)
    check(
        cdp.click("发送消息")
        or bool(
            cdp.eval(
                "(()=>{const b=document.querySelector('.composer__send');if(!b||b.disabled)return false;b.click();return true;})()"
            )
        ),
        "发送公开问题",
    )


def close_settings(cdp: Cdp) -> None:
    if not cdp.click("关闭设置"):
        cdp.eval("document.querySelector('.dialog__head button')?.click()")
    cdp.wait("!document.querySelector('[role=dialog][aria-label=\"设置\"]')")


def focus_trap_check(cdp: Cdp, label: str) -> None:
    scope = f"document.querySelector('[role=dialog][aria-label={json.dumps(label, ensure_ascii=False)}]')"
    targets = (
        f"[...{scope}.querySelectorAll('button,input,select,textarea,a[href],summary,[tabindex]')]"
        ".filter(e=>e.tabIndex>=0&&!e.matches(':disabled')&&!e.closest('[inert]')&&e.getClientRects().length>0)"
    )
    cdp.eval(f"({targets}).at(-1)?.focus()")
    cdp.call(
        "Input.dispatchKeyEvent",
        {"type": "keyDown", "key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
    )
    check(
        cdp.eval(f"document.activeElement===({targets})[0]"),
        f"{label} Tab 从末项循环到首项",
    )
    cdp.call(
        "Input.dispatchKeyEvent",
        {
            "type": "keyDown",
            "key": "Tab",
            "code": "Tab",
            "windowsVirtualKeyCode": 9,
            "modifiers": 8,
        },
    )
    check(
        cdp.eval(f"document.activeElement===({targets}).at(-1)"),
        f"{label} Shift+Tab 从首项循环到末项",
    )
    # A deliberate focus escape is the negative control for the modal owner check.
    cdp.eval("document.querySelector('.topbar button')?.focus()")
    check(
        cdp.eval(f"!{scope}.contains(document.activeElement)"),
        f"{label} 对照组确实把焦点放到背景",
    )
    cdp.call(
        "Input.dispatchKeyEvent",
        {"type": "keyDown", "key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
    )
    check(
        cdp.eval(f"document.activeElement===({targets})[0]"),
        f"{label} 把背景 Tab 拉回当前模态",
    )


def settings_checks(cdp: Cdp, *, visual: bool) -> None:
    print("\n=== 设置与模型渲染（不保存真实配置）===")
    check(cdp.click("设置"), "设置入口可操作")
    check(
        cdp.wait("!!document.querySelector('[role=dialog][aria-label=\"设置\"]')"),
        "设置对话框出现",
    )
    check(
        cdp.eval("!!document.activeElement?.closest('[role=dialog][aria-label=设置]')"),
        "设置打开后焦点进入对话框",
    )
    focus_trap_check(cdp, "设置")
    cdp.call(
        "Input.dispatchKeyEvent",
        {
            "type": "keyDown",
            "key": "Tab",
            "code": "Tab",
            "windowsVirtualKeyCode": 9,
            "modifiers": 8,
        },
    )
    check(
        cdp.eval("!!document.activeElement?.closest('[role=dialog][aria-label=设置]')"),
        "Shift+Tab 焦点保持在设置窗口",
    )
    for label in ("通用", "模型", "Agent", "工作区", "访问控制"):
        check(
            cdp.eval(
                f"!!{button_expression(label, scope='document.querySelector("[aria-label=设置分类]")')}"
            ),
            f"分类：{label}",
        )
    check(
        cdp.eval("document.querySelectorAll('.theme-card').length===3"), "三个主题选项"
    )
    cdp.click("模型")
    check(cdp.wait("!!document.querySelector('.model-current')"), "模型清单已渲染")
    check(
        cdp.eval("document.querySelectorAll('.provider-card').length>0"),
        "供应商入口按实际列表显示",
    )
    check(
        cdp.eval("!!document.querySelector('.model-current__name')?.textContent"),
        "显示当前模型名",
    )
    cdp.eval("document.querySelector('.provider-card')?.click()")
    check(cdp.wait("!!document.querySelector('.model-form')"), "添加模型表单可展开")
    check(
        cdp.eval("document.querySelectorAll('.model-form input').length>=4"),
        "模型表单可填写",
    )
    cdp.click("工作区")
    check(
        cdp.wait("document.body.textContent.includes('允许 Agent 写入文件')"),
        "写权限设置明确展示",
    )
    check(
        cdp.eval(
            "[...document.querySelectorAll('label')].some(l=>l.textContent.includes('敏感文件')&&!!l.querySelector('input[type=checkbox]'))"
        ),
        "敏感文件权限明确展示",
    )
    actual = cdp.eval(
        "[...document.querySelectorAll('label')].find(l=>l.textContent.includes('允许 Agent 写入文件'))?.querySelector('input[type=checkbox]')?.checked"
    )
    expected = False if visual else _server_flag("file_write_enabled")
    if expected is not None:
        check(actual is expected, "写权限与配置一致", f"实际={actual} 预期={expected}")
    close_settings(cdp)
    check(
        cdp.eval("!document.querySelector('[role=dialog][aria-label=\"设置\"]')"),
        "设置关闭",
    )
    check(
        cdp.eval("document.activeElement?.getAttribute('aria-label')==='设置'"),
        "设置关闭后焦点回到入口",
    )


def reliability_checks(cdp: Cdp, *, visual: bool) -> None:
    print("\n=== 预算终态与流式取消（合成 SSE，零模型调用）===")
    if not visual:
        cdp.eval(r"""
          window.__fixture={case:'token_budget',cancelled:false};
          window.__originalFetch=window.fetch;
          window.fetch=async(input,options)=>{
            if(new URL(String(input),location.href).pathname!=='/api/chat/stream')return window.__originalFetch(input,options);
            const emit=e=>new TextEncoder().encode('event: '+e.type+'\ndata: '+JSON.stringify(e)+'\n\n');
            const body=new ReadableStream({start(c){
              for(const e of [{type:'start'},{type:'step',step:1},{type:'token',step:1,content:'公开样本的部分结论'}])c.enqueue(emit(e));
              if(window.__fixture.case!=='cancel'){
                for(const e of [{type:'final',step:1,content:'已有结论，任务未完成'},{type:'error',content:'已停止后续模型调用'},{type:'done',stopped_reason:window.__fixture.case,usage_complete:false,context_trimmed:true,usage:{prompt_tokens:10,completion_tokens:3,total_tokens:13}}])c.enqueue(emit(e));
                c.close();
              }
            },cancel(){window.__fixture.cancelled=true;}});
            return new Response(body,{headers:{'Content-Type':'text/event-stream'}});
          };
        """)
    for label in ("先规划", "多专家"):
        check(cdp.click(label), f"切换{label}")
        check(
            cdp.eval(
                "document.querySelector('.composer').textContent.includes('本轮不使用会话历史')"
            ),
            f"{label} 明确独立任务",
        )
    for reason, label in (
        ("token_budget", "累计 token 预算"),
        ("timeout", "整轮时长预算"),
    ):
        cdp.eval(f"window.__fixture.case={json.dumps(reason)}")
        send(cdp, "公开样本预算保护核验")
        check(
            cdp.wait(
                f"document.body.textContent.includes({json.dumps(label)}) && !document.querySelector('.composer__stop')"
            ),
            f"{reason} 已显示",
        )
        check(
            cdp.eval("document.body.textContent.includes('统计不完整')"),
            "缺失用量有明确说明",
        )
        check(
            cdp.eval("document.body.textContent.includes('部分结果')"),
            "已有结论标为部分结果",
        )
    check(
        cdp.eval("document.body.textContent.includes('上下文已裁剪')"),
        "上下文裁剪仍可见",
    )
    cdp.eval("window.__fixture.case='cancel'")
    send(cdp, "公开样本中断验证")
    check(cdp.wait(f"!!{button_expression('停止生成')}"), "生成时停止按钮出现")
    check(cdp.click("停止生成"), "停止按钮可操作")
    check(cdp.wait("window.__fixture.cancelled===true"), "停止确实取消 ReadableStream")
    check(cdp.wait(f"!{button_expression('停止生成')}"), "停止后恢复输入")
    if not visual:
        cdp.eval("window.fetch=window.__originalFetch")


def layout_check(cdp: Cdp, label: str, *, composer: bool = True) -> None:
    check(
        cdp.eval(
            "document.documentElement.scrollWidth<=innerWidth+1 && document.body.scrollWidth<=innerWidth+1"
        ),
        f"{label} 无横向页面溢出",
    )
    if composer:
        bounds = cdp.eval(
            f"(()=>{{const r=({composer_expression()}).getBoundingClientRect();return {{top:r.top,bottom:r.bottom,left:r.left,right:r.right}};}})()"
        )
        visible = cdp.eval(
            f"(()=>{{const r=({composer_expression()}).getBoundingClientRect();return r.height>0&&r.top>=0&&r.bottom<=innerHeight+1&&r.left>=0&&r.right<=innerWidth+1;}})()"
        )
        check(bool(visible), f"{label} 输入框在视口内", str(bounds))


def error_capture_control(cdp: Cdp) -> None:
    """A negative control proves that the collector cannot pass when uninstalled."""
    cdp.eval("setTimeout(()=>{throw new Error('smoke-negative-control')},0)")
    check(
        cdp.wait(
            "window.__errors__?.some(e=>e.kind==='error'&&e.message.includes('smoke-negative-control'))"
        ),
        "错误监听对故意抛错会失败（对照组）",
    )
    cdp.eval("Promise.reject(new Error('smoke-negative-rejection'));void 0")
    check(
        cdp.wait(
            "window.__errors__?.some(e=>e.kind==='unhandledrejection'&&e.message.includes('smoke-negative-rejection'))"
        ),
        "未处理 rejection 监听有效",
    )
    cdp.eval("console.error('smoke-negative-console');void 0")
    check(
        cdp.wait(
            "window.__errors__?.some(e=>e.kind==='console.error'&&e.message==='smoke-negative-console')"
        ),
        "console.error 监听有效",
    )
    cdp.eval(
        "window.__errors__=window.__errors__.filter(e=>!e.message.includes('smoke-negative-'))"
    )


def service_state_checks(cdp: Cdp) -> None:
    print("\n=== 真实状态分支（只改变浏览器合成返回值）===")
    for case, action in (
        ("missing-model", "配置模型"),
        ("missing-key", "填写访问密钥"),
    ):
        cdp.eval(f"sessionStorage.setItem('smoke-health-case',{json.dumps(case)})")
        cdp.reload()
        check(cdp.wait(f"!!{button_expression(action)}"), f"{case} 提供明确操作入口")
        check(cdp.click(action), f"{action} 可操作")
        if case == "missing-model":
            check(
                cdp.wait("!!document.querySelector('.model-current')"),
                "缺模型入口定位模型分类",
            )
        else:
            check(
                cdp.eval(
                    "document.querySelector('[role=dialog]')?.textContent.includes('保存到本浏览器')"
                ),
                "缺密钥入口定位访问控制",
            )
        close_settings(cdp)
    cdp.eval("sessionStorage.setItem('smoke-health-case','offline')")
    cdp.reload()
    check(
        cdp.wait("document.body.textContent.includes('暂时无法连接服务')"),
        "离线状态明确展示",
    )
    cdp.eval(
        "window.__fixture.healthCase='ready';sessionStorage.removeItem('smoke-health-case')"
    )
    check(cdp.click("重试连接"), "离线状态可以重试")
    check(
        cdp.wait("!document.body.textContent.includes('暂时无法连接服务')"),
        "重试成功恢复服务",
    )
    cdp.eval("sessionStorage.setItem('smoke-settings-failure','true')")
    cdp.reload()
    check(
        cdp.wait("document.body.textContent.includes('工作区信息暂不可用')"),
        "读取工作区失败与未配置区分",
    )
    check(cdp.eval(f"!{button_expression('选择工作区')}"), "读取失败没有伪装为未配置")
    cdp.eval(
        "window.__fixture.settingsFailure=false;sessionStorage.removeItem('smoke-settings-failure')"
    )
    check(cdp.click("刷新服务状态"), "工作区信息可刷新")
    check(cdp.wait(f"!!{button_expression('工作区文件')}"), "声明的工作区重新展示")


def scroll_to_answer(cdp: Cdp, *, collapse: bool = False) -> None:
    if collapse:
        cdp.eval(
            "document.querySelectorAll('button[aria-label=\"收起执行过程\"]').forEach(b=>b.click())"
        )
    cdp.eval(
        "(()=>{const el=document.querySelector('.chat__scroll');if(el)el.scrollTop=el.scrollHeight;})()"
    )
    time.sleep(0.15)


def workspace_race_checks(cdp: Cdp) -> None:
    print("\n=== 工作区切换与延迟旧响应（公开合成数据）===")
    original = cdp.eval("window.__fixture.settings.agent.workspace_root")
    cdp.eval("window.__fixture.deferPreview=true")
    cdp.eval(
        "[...document.querySelectorAll('.fileside button')].find(b=>b.textContent.includes('demo-notes.md'))?.click()"
    )
    check(
        cdp.wait("window.__fixture.pendingFiles.length===1"), "旧工作区预览请求确实在途"
    )
    cdp.eval("window.__fixture.deferListing=true")
    check(cdp.click("刷新工作区文件"), "旧工作区目录刷新可触发")
    check(
        cdp.wait("window.__fixture.pendingFiles.length===2"),
        "旧预览与目录两个请求均尚未返回",
    )
    cdp.eval("window.__fixture.settings.agent.workspace_root='C:/Legacy/demo-next'")
    check(cdp.click("刷新服务状态"), "读取新的声明工作区")
    check(
        cdp.wait(
            "document.querySelector('.fileside__root')?.textContent.includes('demo-next')&&document.querySelector('.fileside__entries')?.textContent.includes('new-workspace.md')"
        ),
        "新工作区目录生效",
    )
    check(
        cdp.eval("!document.querySelector('.fileside__preview')"),
        "切换根目录后旧预览立即清除",
    )
    cdp.eval(
        "[...document.querySelectorAll('.fileside button')].find(b=>b.textContent.includes('new-workspace.md'))?.click()"
    )
    check(
        cdp.wait(
            "document.querySelector('.fileside__preview')?.textContent.includes('新工作区资料')"
        ),
        "新工作区预览生效",
    )
    cdp.eval("window.__fixture.releaseFiles()")
    check(
        cdp.wait("window.__fixture.releasedFiles===2"),
        "迟到的旧响应确实已释放（对照组）",
    )
    time.sleep(0.2)
    check(
        cdp.eval(
            "document.querySelector('.fileside__preview')?.textContent.includes('新工作区资料')&&!document.querySelector('.fileside__preview')?.textContent.includes('公开演示资料')"
        ),
        "迟到旧预览没有覆盖新资料",
    )
    check(
        cdp.eval(
            "document.querySelector('.fileside__entries')?.textContent.includes('new-workspace.md')&&!document.querySelector('.fileside__entries')?.textContent.includes('demo-notes.md')"
        ),
        "迟到旧目录没有覆盖新目录",
    )
    cdp.eval(f"window.__fixture.settings.agent.workspace_root={json.dumps(original)}")
    cdp.click("刷新服务状态")
    check(
        cdp.wait(
            "document.querySelector('.fileside__entries')?.textContent.includes('demo-notes.md')"
        ),
        "演示工作区已恢复",
    )


def refresh_snapshot(cdp: Cdp) -> dict:
    return cdp.eval(
        "Object.fromEntries(['/healthz','/api/meta','/api/tools'].map(p=>[p,window.__fixture.requests.filter(r=>r.path===p).length]))"
    )


def assert_configuration_refresh(cdp: Cdp, before: dict, label: str) -> None:
    check(
        cdp.wait(
            "&&".join(
                f"window.__fixture.requests.filter(r=>r.path==={json.dumps(path)}).length>{count}"
                for path, count in before.items()
            )
        ),
        f"{label} 后刷新服务状态、模型信息与工具清单",
    )


def press_key(cdp: Cdp, key: str = "Enter", *, modifiers: int = 0) -> None:
    code = 13 if key == "Enter" else 9
    payload = {
        "type": "keyDown",
        "key": key,
        "code": key,
        "windowsVirtualKeyCode": code,
        "modifiers": modifiers,
    }
    if key == "Enter":
        payload["text"] = "\r"
        payload["unmodifiedText"] = "\r"
    cdp.call("Input.dispatchKeyEvent", payload)
    cdp.call(
        "Input.dispatchKeyEvent",
        {
            "type": "keyUp",
            "key": key,
            "code": key,
            "windowsVirtualKeyCode": code,
            "modifiers": modifiers,
        },
    )
    time.sleep(0.15)


def configuration_refresh_checks(cdp: Cdp) -> None:
    print("\n=== 模型与权限保存后的真实组件刷新（仅 mock API）===")
    cdp.click("模型设置")
    check(
        cdp.wait("document.querySelectorAll('.model-card').length===2"),
        "两条公开模型可用于切换对照",
    )
    before = refresh_snapshot(cdp)
    check(cdp.click("切换"), "切换到另一公开模型")
    check(
        cdp.wait(
            "document.querySelector('.topbar')?.textContent.includes('legacy-check')"
        ),
        "切换后顶栏模型立即更新",
    )
    assert_configuration_refresh(cdp, before, "模型切换")
    check(cdp.click("切换"), "恢复公开演示模型")
    check(
        cdp.wait(
            "document.querySelector('.topbar')?.textContent.includes('legacy-demo')"
        ),
        "恢复后顶栏同步",
    )
    close_settings(cdp)
    cdp.click("设置")
    cdp.click("工作区")
    cdp.eval(
        "[...document.querySelectorAll('label')].find(l=>l.textContent.includes('允许 Agent 写入文件'))?.querySelector('input')?.click()"
    )
    before = refresh_snapshot(cdp)
    check(cdp.click("保存此页") or cdp.click("保存并生效"), "保存浏览器内模拟权限")
    check(
        cdp.wait(
            "window.__fixture.settings.agent.file_write_enabled===true&&document.querySelector('.sidebar')?.textContent.includes('允许读写')"
        ),
        "保存后侧栏权限立即更新",
    )
    assert_configuration_refresh(cdp, before, "权限保存")
    close_settings(cdp)
    check(cdp.click("查看工具"), "工具入口可打开")
    check(
        cdp.wait(
            "document.querySelector('.drawer')?.textContent.includes('write_file')"
        ),
        "工具清单确实包含新模拟能力",
    )
    cdp.eval(
        "(()=>{const d=document.querySelector('.drawer');const summary=d.querySelector('summary');const targets=[...d.querySelectorAll('button,input,select,textarea,a[href],summary,[tabindex]')].filter(e=>e.tabIndex>=0&&!e.matches(':disabled')&&e.getClientRects().length>0);const index=targets.indexOf(summary);window.__summaryInTabOrder=index>0;targets[index-1]?.focus();})()"
    )
    check(
        cdp.eval("window.__summaryInTabOrder===true"),
        "服务详情 summary 在可用焦点列表中",
    )
    press_key(cdp, "Tab")
    check(
        cdp.eval("document.activeElement===document.querySelector('.drawer summary')"),
        "Tab 能到达服务详情 summary",
    )
    press_key(cdp)
    check(
        cdp.eval(
            "document.querySelector('.drawer summary')?.parentElement.open===true"
        ),
        "Enter 可以展开服务详情",
    )
    focus_trap_check(cdp, "已注册的工具")
    cdp.click("关闭工具面板", scope="document.querySelector('.drawer')")
    cdp.click("设置")
    cdp.click("工作区")
    cdp.eval(
        "[...document.querySelectorAll('label')].find(l=>l.textContent.includes('允许 Agent 写入文件'))?.querySelector('input')?.click()"
    )
    check(cdp.click("保存此页") or cdp.click("保存并生效"), "恢复模拟只读权限")
    check(
        cdp.wait(
            "window.__fixture.settings.agent.file_write_enabled===false&&document.querySelector('.sidebar')?.textContent.includes('只读访问')"
        ),
        "模拟工作区恢复只读",
    )
    close_settings(cdp)
    check(
        cdp.eval("!window.__fixture.requests.some(r=>r.path==='/api/settings/test')"),
        "所有验证没有触发模型连接测试接口",
    )


def composer_focus_checks(cdp: Cdp) -> None:
    """Exercise real pointer/Tab focus; typing fields also match :focus-visible."""
    print("\n=== 输入框焦点边界（真实鼠标与键盘）===")
    cdp.eval("document.activeElement?.blur()")
    time.sleep(0.2)
    blur_border = cdp.eval(
        "getComputedStyle(document.querySelector('.composer')).borderTopColor"
    )
    bounds = cdp.eval(
        f"(()=>{{const r=({composer_expression()}).getBoundingClientRect();"
        "return {x:r.left+Math.min(r.width/2,80),y:r.top+r.height/2};})()"
    )
    for event_type in ("mouseMoved", "mousePressed", "mouseReleased"):
        params = {"type": event_type, "x": bounds["x"], "y": bounds["y"]}
        if event_type != "mouseMoved":
            params.update({"button": "left", "clickCount": 1})
        cdp.call("Input.dispatchMouseEvent", params)
    check(
        cdp.eval(
            f"document.activeElement===({composer_expression()})"
            f"&&({composer_expression()}).matches(':focus-visible')"
        ),
        "真实鼠标点击聚焦文本框并匹配 focus-visible",
    )
    check(
        cdp.eval(f"getComputedStyle({composer_expression()}).boxShadow==='none'"),
        "鼠标输入没有把框分成两段的内部焦点阴影",
        str(cdp.eval(f"getComputedStyle({composer_expression()}).boxShadow")),
    )
    check(
        cdp.wait(
            "document.querySelector('.composer').matches(':focus-within')"
            "&&getComputedStyle(document.querySelector('.composer')).borderTopColor!=="
            + json.dumps(blur_border)
        ),
        "整个输入容器的边框仍明确标示焦点",
    )
    # Start immediately before the textarea in the actual visible Tab order.
    prepared = cdp.eval(
        f"(()=>{{const box=({composer_expression()});"
        "const targets=[...document.querySelectorAll('button,input,select,textarea,a[href],summary,[tabindex]')]"
        ".filter(e=>e.tabIndex>=0&&!e.matches(':disabled')&&!e.closest('[inert]')&&e.getClientRects().length>0);"
        "const previous=targets[targets.indexOf(box)-1];"
        "if(!previous)return false;previous.focus();return true;})()"
    )
    check(bool(prepared), "输入框前一项存在于真实 Tab 顺序")
    press_key(cdp, "Tab")
    check(
        cdp.eval(
            f"document.activeElement===({composer_expression()})"
            f"&&({composer_expression()}).matches(':focus-visible')"
            f"&&getComputedStyle({composer_expression()}).boxShadow==='none'"
        ),
        "Tab 聚焦输入时也只使用整个容器的焦点边框",
    )
    press_key(cdp, "Tab")
    check(
        cdp.eval(
            "document.activeElement?.tagName==='BUTTON'"
            "&&document.activeElement.matches(':focus-visible')"
            "&&getComputedStyle(document.activeElement).boxShadow!=='none'"
        ),
        "对照组：Tab 到按钮仍保留可见键盘焦点环",
    )
    cdp.eval("document.activeElement?.blur()")
    time.sleep(0.2)


def keyboard_checks(cdp: Cdp) -> None:
    print("\n=== 中文输入保护与两种发送键（合成 SSE）===")
    cdp.eval("window.__fixture.case='finished'")
    cdp.click("自动推理")
    count_expr = (
        "window.__fixture.requests.filter(r=>r.path==='/api/chat/stream').length"
    )
    before = cdp.eval(count_expr)
    fill(cdp, "中文输入保护")
    cdp.eval(
        f"(()=>{{const box={composer_expression()};box.dispatchEvent(new CompositionEvent('compositionstart',{{bubbles:true,data:'核验'}}));box.dispatchEvent(new KeyboardEvent('keydown',{{key:'Enter',code:'Enter',keyCode:13,isComposing:true,bubbles:true,cancelable:true}}));}})()"
    )
    time.sleep(0.15)
    check(cdp.eval(count_expr) == before, "中文 composition 中 Enter 没有发送")
    check(
        cdp.eval(f"({composer_expression()}).value.includes('中文输入保护')"),
        "中文输入中的草稿保持",
    )
    cdp.eval(
        f"({composer_expression()}).dispatchEvent(new CompositionEvent('compositionend',{{bubbles:true,data:'核验'}}))"
    )
    press_key(cdp, modifiers=8)
    check(
        cdp.eval(count_expr) == before
        and cdp.eval(f"({composer_expression()}).value.includes('\\n')"),
        "默认 Shift+Enter 只换行",
    )
    press_key(cdp)
    check(
        cdp.wait(
            f"{count_expr}==={before + 1}&&!document.querySelector('.composer__stop')"
        ),
        "默认 Enter 发出并完成一次合成请求",
    )
    cdp.click("设置")
    cdp.click("通用")
    check(cdp.click("Ctrl/⌘ + Enter 发送"), "切换为组合键发送")
    close_settings(cdp)
    before = cdp.eval(count_expr)
    fill(cdp, "长文本组合键核验")
    press_key(cdp)
    check(
        cdp.eval(count_expr) == before
        and cdp.eval(f"({composer_expression()}).value.includes('\\n')"),
        "组合键模式 Enter 只换行",
    )
    press_key(cdp, modifiers=2)
    check(
        cdp.wait(
            f"{count_expr}==={before + 1}&&!document.querySelector('.composer__stop')"
        ),
        "Ctrl+Enter 发出并完成一次合成请求",
    )
    cdp.click("设置")
    cdp.click("通用")
    cdp.click("Enter 发送")
    close_settings(cdp)


def run_history_checks(cdp: Cdp, directory: Path) -> None:
    print("\n=== 运行记录验收（合成 API，无模型请求）===")
    cdp.viewport(1440, 900)
    check(
        cdp.click(
            "查看运行摘要",
            scope="[...document.querySelectorAll('article.turn')].at(-1)",
        ),
        "答案附近可直接打开本轮摘要",
    )
    check(cdp.wait("!!document.querySelector('.runs-detail h3')"), "本轮执行摘要已读取")
    check(
        cdp.eval(
            "document.querySelector('.runs-detail')?.textContent.includes('已取消')"
        ),
        "停止后的轮次保留取消状态",
    )
    check(
        cdp.eval(
            "document.querySelector('.runs-detail')?.textContent.includes('用量统计不完整')"
        ),
        "取消后的未知用量明确显示",
    )
    focus_trap_check(cdp, "运行记录")
    check(cdp.click("关闭运行记录"), "关闭运行记录")
    if cdp.eval(
        "document.querySelector('.sidebar')?.getAttribute('aria-hidden')==='true'"
    ):
        cdp.click("切换会话列表")
    check(
        cdp.click("运行记录", scope="document.querySelector('.sidebar')"),
        "会话栏可进入所有运行记录",
    )
    check(
        cdp.wait("document.querySelectorAll('.runs-row').length>0"),
        "历史运行列表有可点击记录",
    )
    cdp.eval(
        "(()=>{const s=document.querySelector('[aria-label=\"运行状态\"]');s.value='token_budget';s.dispatchEvent(new Event('change',{bubbles:true}));})()"
    )
    check(
        cdp.wait(
            "document.querySelectorAll('.runs-row').length>0&&[...document.querySelectorAll('.runs-row')].every(e=>e.textContent.includes('累计 token'))"
        ),
        "状态筛选只显示 token 预算终止",
    )
    cdp.eval(
        "[...document.querySelectorAll('.runs-row')].find(e=>e.textContent.includes('public-archi'))?.click()"
    )
    check(
        cdp.wait(
            "document.querySelector('.runs-detail')?.textContent.includes('public-archive-budget')"
        ),
        "旧预算终止可以重新查看",
    )
    check(
        cdp.eval(
            "document.querySelector('.runs-detail')?.textContent.includes('上下文已裁剪')&&document.querySelector('.runs-detail')?.textContent.includes('执行摘要已截断')"
        ),
        "裁剪与摘要截断保持可见",
    )
    check(
        cdp.eval(
            "document.querySelector('.runs-timeline')?.textContent.includes('子任务')&&document.querySelector('.runs-timeline')?.textContent.includes('结果已截断')"
        ),
        "子任务工具摘要可查看",
    )
    layout_check(cdp, "1440×900 运行记录")
    cdp.screenshot(directory, "11-runs-light-1440")
    cdp.viewport(1024, 768)
    layout_check(cdp, "1024×768 运行记录")
    cdp.screenshot(directory, "12-runs-light-1024")
    cdp.viewport(390, 844)
    layout_check(cdp, "390×844 运行记录")
    cdp.screenshot(directory, "13-runs-light-390")
    check(
        cdp.eval(
            "document.querySelector('.runs-detail').clientHeight>100&&getComputedStyle(document.querySelector('.runs-detail')).overflowY==='auto'"
        ),
        "手机摘要有独立滚动区",
    )
    cdp.click("关闭运行记录")
    check(cdp.wait("!document.querySelector('.runs-dialog')"), "手机运行记录可以关闭")
    check(cdp.click("切换会话列表"), "手机重新打开会话抽屉")
    check(
        cdp.click("运行记录", scope="document.querySelector('.sidebar')"),
        "手机会话入口可打开运行记录",
    )
    check(
        cdp.wait(
            "!!document.querySelector('.runs-dialog')&&document.querySelector('.sidebar').getAttribute('aria-hidden')==='true'"
        ),
        "手机打开记录时会话抽屉关闭",
    )
    cdp.click("关闭运行记录")
    check(
        cdp.wait("!document.querySelector('.runs-dialog')"),
        "手机会话入口打开的记录可以关闭",
    )
    check(
        cdp.eval("document.activeElement?.getAttribute('aria-label')==='切换会话列表'"),
        "手机关闭记录后焦点回到可见入口",
    )
    cdp.viewport(1440, 900)
    cdp.click("切换会话列表")
    cdp.click("运行记录", scope="document.querySelector('.sidebar')")
    check(
        cdp.wait("document.querySelectorAll('.runs-row').length>3"),
        "桌面重新打开记录列表",
    )
    # The first delayed detail must not replace the subsequently selected run.
    cdp.eval(
        "(()=>{const s=document.querySelector('[aria-label=\"运行状态\"]');s.value='';s.dispatchEvent(new Event('change',{bubbles:true}));})()"
    )
    check(cdp.wait("document.querySelectorAll('.runs-row').length>3"), "恢复全部记录")
    cdp.eval(
        "window.__fixture.deferRunDetail=true;[...document.querySelectorAll('.runs-row')].find(e=>e.textContent.includes('执行出错'))?.click()"
    )
    check(cdp.wait("window.__fixture.pendingRuns.length===1"), "对照组挂起旧详情请求")
    cdp.eval(
        "[...document.querySelectorAll('.runs-row')].find(e=>e.textContent.includes('正常结束'))?.click()"
    )
    check(
        cdp.wait(
            "document.querySelector('.runs-detail h3')?.textContent.includes('正常结束')"
        ),
        "新选择读取正常结束摘要",
    )
    cdp.eval("window.__fixture.releaseRuns()")
    check(
        cdp.wait(
            "window.__fixture.pendingRuns.length===0&&document.querySelector('.runs-detail h3')?.textContent.includes('正常结束')"
        ),
        "迟到的旧详情不能覆盖新选择",
    )
    before = cdp.eval("window.__fixture.runs.length")
    check(
        cdp.click("删除记录", scope="document.querySelector('.runs-detail')"),
        "删除先进入确认状态",
    )
    check(cdp.eval(f"window.__fixture.runs.length==={before}"), "首次点击不删除")
    check(cdp.click("确认删除这条摘要"), "确认后删除摘要")
    check(
        cdp.wait(
            f"window.__fixture.runs.length==={before - 1}&&!document.querySelector('.runs-detail h3')"
        ),
        "删除完成后列表和详情更新",
    )
    cdp.eval("window.__fixture.runsFailure=true")
    check(
        cdp.click("刷新", scope="document.querySelector('.runs-dialog')"),
        "刷新运行列表",
    )
    check(
        cdp.wait(
            "document.querySelector('.runs-dialog')?.textContent.includes('运行记录暂不可用')"
        ),
        "读取失败显示可操作错误",
    )
    check(
        cdp.eval(
            "!document.querySelector('.runs-dialog').textContent.includes('没有符合条件')"
        ),
        "读取失败不能冒充空记录",
    )
    cdp.eval("window.__fixture.runsFailure=false")
    check(
        cdp.click("刷新", scope="document.querySelector('.runs-dialog')"),
        "失败后可以重试",
    )
    check(
        cdp.wait(
            "document.querySelectorAll('.runs-row').length>0&&!document.querySelector('.runs-dialog').textContent.includes('暂不可用')"
        ),
        "重试恢复列表",
    )
    cdp.click("关闭运行记录")
    check(
        cdp.wait("!document.querySelector('.runs-dialog')"), "运行记录关闭后回到主界面"
    )
    check(cdp.click("设置"), "运行记录关闭后可打开设置")
    check(
        cdp.wait("!!document.querySelector('[aria-label=\"设置\"] .dialog__nav')"),
        "设置分类已挂载",
    )
    check(
        cdp.click(
            "Agent",
            scope="document.querySelector('[role=dialog][aria-label=\"设置\"]')",
        ),
        "定位 Agent 设置分类",
    )
    check(
        cdp.wait(
            "[...document.querySelectorAll('[role=dialog][aria-label=\"设置\"] button')].some(b=>b.textContent.trim()==='保存此页'&&!b.disabled)"
        ),
        "设置读取完成且可保存",
    )
    check(
        cdp.eval(
            "document.querySelector('[role=dialog][aria-label=\"设置\"]')?.textContent.includes('持久保存运行摘要')"
        ),
        "Agent 设置包含明确持久化开关",
    )
    cdp.eval(
        "(()=>{const l=[...document.querySelectorAll('label')].find(l=>l.textContent.includes('持久保存运行摘要'));l.querySelector('input').click();})()"
    )
    check(cdp.click("保存此页"), "保存持久化配置")
    check(
        cdp.wait(
            "document.querySelector('[role=dialog][aria-label=\"设置\"]')?.textContent.includes('等待重启')"
        ),
        "已保存配置与当前内存存储区分",
    )
    check(
        cdp.eval(
            "document.querySelector('[role=dialog][aria-label=\"设置\"]')?.textContent.includes('存储切换需重启')"
        ),
        "保存反馈说明存储切换需要重启",
    )
    # Restore only the fixture state, never the user's settings.
    cdp.eval(
        "(()=>{const l=[...document.querySelectorAll('label')].find(l=>l.textContent.includes('持久保存运行摘要'));l.querySelector('input').click();})()"
    )
    cdp.click("保存此页")
    check(
        cdp.wait(
            "!document.querySelector('[role=dialog][aria-label=\"设置\"]').textContent.includes('等待重启')"
        ),
        "临时演示配置恢复内存",
    )
    cdp.click(
        "通用", scope="document.querySelector('[role=dialog][aria-label=\"设置\"]')"
    )
    check(
        cdp.click(
            "深色", scope="document.querySelector('[role=dialog][aria-label=\"设置\"]')"
        ),
        "切换真实深色偏好",
    )
    check(
        cdp.wait("document.documentElement.dataset.theme==='dark'"),
        "记录截图使用实际深色主题",
    )
    close_settings(cdp)
    cdp.click("运行记录", scope="document.querySelector('.sidebar')")
    check(
        cdp.wait("document.querySelectorAll('.runs-row').length>0"),
        "深色模式打开运行记录",
    )
    cdp.eval(
        "[...document.querySelectorAll('.runs-row')].find(e=>e.textContent.includes('执行出错'))?.click()"
    )
    check(
        cdp.wait(
            "document.querySelector('.runs-detail h3')?.textContent.includes('执行出错')&&document.documentElement.dataset.theme==='dark'"
        ),
        "深色主题的失败摘要已渲染",
    )
    cdp.screenshot(directory, "14-runs-dark-1440")
    cdp.click("关闭运行记录")
    cdp.wait("!document.querySelector('.runs-dialog')")
    cdp.click("设置")
    cdp.wait("!!document.querySelector('[role=dialog][aria-label=\"设置\"]')")
    cdp.click("通用")
    cdp.click("浅色")
    close_settings(cdp)


def visual_checks(cdp: Cdp, directory: Path) -> None:
    print("\n=== 简约界面视觉验收（公开 fixture API，零服务器数据）===")
    cdp.viewport(1440, 900)
    check(
        cdp.eval("document.documentElement.dataset.theme==='light'"), "首次打开默认浅色"
    )
    check(
        cdp.eval("document.body.textContent.includes('有什么需要一起解决？')"),
        "通用欢迎文案",
    )
    check(
        cdp.eval("document.body.textContent.includes('重启后历史不会保留')"),
        "内存历史提示",
    )
    check(
        cdp.eval(
            "(()=>{const el=document.querySelector('.sidebar__list');return !!el&&getComputedStyle(el).overflowY==='auto'&&el.scrollHeight>el.clientHeight;})()"
        ),
        "历史列表具有独立滚动区域",
    )
    layout_check(cdp, "1440×900 首屏")
    composer_focus_checks(cdp)
    cdp.screenshot(directory, "01-home-light-1440")
    # Suggestion controls fill a draft, they must not trigger an API call.
    before = cdp.eval(
        "window.__fixture.requests.filter(r=>r.path==='/api/chat/stream').length"
    )
    clicked = cdp.eval(
        "(()=>{const b=[...document.querySelectorAll('button')].find(b=>b.textContent.includes('计算核验'));if(!b)return false;b.click();return true;})()"
    )
    check(bool(clicked), "通用示例可点击")
    check(
        cdp.eval(
            f"document.activeElement===({composer_expression()})&&({composer_expression()}).value.length>0"
        ),
        "示例只填入草稿并聚焦",
    )
    check(
        cdp.eval(
            "window.__fixture.requests.filter(r=>r.path==='/api/chat/stream').length"
        )
        == before,
        "点击示例未发出聊天请求",
    )
    check(cdp.click("模型设置"), "模型名直接打开模型设置")
    check(
        cdp.wait("!!document.querySelector('.model-current')"),
        "模型设置直接定位正确分类",
    )
    close_settings(cdp)
    settings_checks(cdp, visual=True)
    configuration_refresh_checks(cdp)
    cdp.click("设置")
    cdp.click("Agent")
    cdp.screenshot(directory, "02-settings-light-1440")
    close_settings(cdp)
    check(cdp.click("自动推理"), "ReAct 模式可选择")
    cdp.eval("window.__fixture.case='finished'")
    send(cdp)
    check(
        cdp.wait(
            "document.body.textContent.includes('576')&&!document.querySelector('.composer__stop')"
        ),
        "完整答案渲染",
    )
    scroll_to_answer(cdp)
    cdp.screenshot(directory, "03-chat-light-1440")
    check(cdp.click("展开执行过程"), "已完成过程默认收起且可展开")
    check(cdp.eval("!!document.querySelector('.toolcard')"), "执行过程包含工具调用")
    check(
        cdp.click("calculator", scope="document.querySelector('.toolcard')")
        or bool(
            cdp.eval(
                "(()=>{const b=document.querySelector('.toolcard button');if(!b)return false;b.click();return true;})()"
            )
        ),
        "工具详情可展开",
    )
    check(
        cdp.eval("document.querySelector('.toolcard')?.textContent.includes('576')"),
        "工具观察结果可查看",
    )
    layout_check(cdp, "1440×900 聊天")
    cdp.eval("document.querySelector('.toolcard')?.scrollIntoView({block:'start'})")
    cdp.screenshot(directory, "03-chat-tools-light-1440")
    for mode, selector in (
        ("先规划", "[aria-label=执行计划]"),
        ("多专家", "[aria-label=专家协作]"),
    ):
        check(cdp.click(mode), f"切换{mode}")
        send(cdp, "以三个步骤核验公开资料并形成建议")
        check(cdp.wait("!document.querySelector('.composer__stop')"), f"{mode} 结束")
        check(cdp.click("展开执行过程"), f"{mode} 过程可展开")
        check(
            cdp.eval(f"!!document.querySelector({json.dumps(selector)})"),
            f"{mode} 计划／专家内容渲染",
        )
        if mode == "先规划":
            check(
                cdp.eval(
                    "document.querySelector('[aria-label=执行计划]')?.textContent.includes('核验公开样本')"
                ),
                "计划详情与完成结论可查看",
            )
        else:
            cdp.eval("document.querySelector('[aria-label=专家协作] button')?.click()")
            check(
                cdp.eval(
                    "document.querySelector('[aria-label=专家协作]')?.textContent.includes('计算结果为 576')"
                ),
                "专家独立结论可展开",
            )
    scroll_to_answer(cdp, collapse=True)
    files_open = (
        cdp.click("查看工作区文件") or cdp.click("工作区文件") or cdp.click("文件")
    )
    check(files_open, "工作区文件入口")
    check(
        cdp.wait("!!document.querySelector('[aria-label=\"工作区文件\"]')"),
        "工作区文件面板可见",
    )
    check(
        cdp.click("demo-notes.md")
        or bool(
            cdp.eval(
                "(()=>{const b=[...document.querySelectorAll('.fileside button')].find(b=>b.textContent.includes('demo-notes.md'));if(!b)return false;b.click();return true;})()"
            )
        ),
        "公开文件可预览",
    )
    check(
        cdp.wait(
            "document.querySelector('.fileside')?.textContent.includes('公开演示资料')"
        ),
        "Markdown 文件已渲染",
    )
    layout_check(cdp, "1440×900 文件并列")
    scroll_to_answer(cdp, collapse=True)
    cdp.screenshot(directory, "04-files-light-1440")
    workspace_race_checks(cdp)
    cdp.click("收起文件栏")
    cdp.click("设置")
    cdp.click("通用")
    check(
        cdp.eval(
            "(()=>{const b=[...document.querySelectorAll('.theme-card')].find(b=>b.textContent.includes('深色'));if(!b)return false;b.click();return true;})()"
        ),
        "深色主题可选择",
    )
    close_settings(cdp)
    check(
        cdp.eval(
            "document.documentElement.dataset.theme==='dark'&&localStorage.getItem('legacy.theme')==='dark'"
        ),
        "深色选择持久化到原存储键",
    )
    scroll_to_answer(cdp, collapse=True)
    cdp.screenshot(directory, "05-chat-dark-1440")
    cdp.reload()
    check(
        cdp.eval("document.documentElement.dataset.theme==='dark'"), "刷新保留深色偏好"
    )
    cdp.click("设置")
    cdp.click("通用")
    cdp.eval(
        "[...document.querySelectorAll('.theme-card')].find(b=>b.textContent.includes('浅色'))?.click()"
    )
    close_settings(cdp)
    cdp.viewport(1024, 768)
    layout_check(cdp, "1024×768 首屏")
    cdp.screenshot(directory, "06-home-light-1024")
    cdp.viewport(390, 844)
    layout_check(cdp, "390×844 首屏")
    cdp.screenshot(directory, "07-home-light-390")
    send(cdp, "请核验公开样本，给出简洁结果。")
    check(cdp.wait("!document.querySelector('.composer__stop')"), "手机聊天结束")
    layout_check(cdp, "390×844 聊天")
    scroll_to_answer(cdp, collapse=True)
    cdp.screenshot(directory, "08-chat-light-390")
    check(cdp.click("切换会话列表"), "手机会话抽屉可打开")
    check(
        cdp.eval(
            "document.querySelector('[aria-label=会话列表]')?.getBoundingClientRect().left>=0"
        ),
        "手机会话列表进入视口",
    )
    focus_trap_check(cdp, "会话列表")
    check(
        cdp.click("收起会话列表", scope="document.querySelector('.sidebar')"),
        "手机会话抽屉可关闭",
    )
    check(
        cdp.eval("document.activeElement?.getAttribute('aria-label')==='切换会话列表'"),
        "手机会话关闭后焦点归还入口",
    )
    check(cdp.click("切换会话列表"), "手机会话抽屉可再次打开")
    check(
        cdp.click("查看工作区文件") or cdp.click("工作区文件") or cdp.click("文件"),
        "手机文件抽屉可打开",
    )
    check(
        cdp.eval(
            "(()=>{const a=document.querySelector('[aria-label=会话列表]');return !a||a.getBoundingClientRect().right<=0||getComputedStyle(a).visibility==='hidden';})()"
        ),
        "手机同时只打开一个侧栏",
    )
    cdp.screenshot(directory, "09-files-light-390")
    focus_trap_check(cdp, "工作区文件")
    check(cdp.click("收起文件栏"), "手机文件抽屉可关闭")
    check(
        cdp.eval("document.activeElement?.getAttribute('aria-label')==='切换会话列表'"),
        "文件原触发入口收起后焦点归还可见会话入口",
    )
    check(cdp.click("切换会话列表"), "文件关闭后可再打开会话列表")
    check(
        cdp.eval(
            "!document.querySelector('.fileside')&&document.querySelector('.sidebar')?.getAttribute('aria-hidden')==='false'"
        ),
        "手机从文件回到会话时也只有一个侧栏",
    )
    cdp.click("收起会话列表", scope="document.querySelector('.sidebar')")
    cdp.viewport(1440, 900)
    reliability_checks(cdp, visual=True)
    run_history_checks(cdp, directory)
    # Terminal warnings must survive the presentation-only preference.
    cdp.click("设置")
    cdp.click("通用")
    cdp.eval(
        "(()=>{const l=[...document.querySelectorAll('label')].find(l=>l.textContent.includes('显示耗时')||l.textContent.includes('显示运行统计'));const b=l?.querySelector('input');if(b?.checked)b.click();})()"
    )
    close_settings(cdp)
    check(
        cdp.eval(
            "document.body.textContent.includes('统计不完整')&&document.body.textContent.includes('上下文已裁剪')"
        ),
        "隐藏统计仍显示重要终态说明",
    )
    check(
        cdp.eval("window.__fixture.blocked.length===0"),
        "没有未声明的 API 请求",
        str(cdp.eval("window.__fixture.blocked")),
    )
    service_state_checks(cdp)
    keyboard_checks(cdp)
    check(
        cdp.eval("window.__fixture.blocked.length===0"),
        "状态与键盘验证后仍没有未声明的 API 请求",
        str(cdp.eval("window.__fixture.blocked")),
    )


def main() -> int:
    global TARGET, PORT, ok
    ok = True
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=TARGET)
    parser.add_argument("--cdp-port", type=int, default=PORT)
    parser.add_argument(
        "--reliability", action="store_true", help="仅合成聊天 SSE 验证预算与停止"
    )
    parser.add_argument(
        "--visual",
        action="store_true",
        help="全部 API 合成，验证重设计与响应式布局；不读私人资料、不调用模型",
    )
    parser.add_argument(
        "--screenshots-dir", type=Path, default=Path("data/ui-redesign")
    )
    args = parser.parse_args()
    TARGET, PORT = args.target, args.cdp_port
    browser = find_browser()
    if browser is None:
        print("没找到 Edge / Chrome，界面验证未完成")
        return 1
    # 不接管已经运行的用户浏览器或别的 CDP 测试实例。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", PORT))
        except OSError:
            print(f"调试端口 {PORT} 已占用，请用 --cdp-port 指定一个空闲端口")
            return 1
    profile = Path(tempfile.mkdtemp(prefix="ui-smoke-"))
    proc = subprocess.Popen(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-component-extensions-with-background-pages",
            "--disable-background-networking",
            "--disable-features=Translate,msEdgeSidebarV2,msEdgeWallet",
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cdp = None
    try:
        page = None
        for _ in range(40):
            time.sleep(0.5)
            try:
                pages = http_json(f"http://127.0.0.1:{PORT}/json/list")
            except OSError:
                continue
            page = next((p for p in pages if p.get("type") == "page"), None)
            if page:
                break
        if page is None:
            print("  [FAIL] 连不上浏览器调试端口")
            return 1
        cdp = Cdp(page["webSocketDebuggerUrl"])
        cdp.call("Runtime.enable")
        cdp.call("Page.enable")
        cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": ERROR_CAPTURE})
        if args.visual:
            source = (
                Path(__file__).with_name("ui_fixtures.js").read_text(encoding="utf-8")
            )
            cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": source})
        cdp.viewport(1440, 900)
        cdp.call("Page.navigate", {"url": TARGET})
        check(
            cdp.wait("!!document.querySelector('.composer')", timeout=12),
            "真实 React 组件已挂载",
        )
        check(cdp.eval("!!document.querySelector('.topbar')"), "顶栏已渲染")
        check(cdp.eval("document.title.length>0"), "页面标题存在")
        extensions = cdp.eval(
            "[...document.querySelectorAll('script[src],link[href]')].map(e=>e.src||e.href).filter(u=>u.startsWith('chrome-extension:')||u.startsWith('edge-extension:'))"
        )
        check(
            isinstance(extensions, list) and not extensions,
            "隔离浏览器没有扩展注入的脚本或样式",
            str(extensions),
        )
        error_capture_control(cdp)
        if args.visual:
            visual_checks(cdp, args.screenshots_dir.resolve())
        else:
            settings_checks(cdp, visual=False)
            if args.reliability:
                reliability_checks(cdp, visual=False)
        errors = cdp.eval("window.__errors__")
        check(
            isinstance(errors, list) and len(errors) == 0,
            "没有未捕获错误、Promise rejection 或 console.error",
            str(errors),
        )
    finally:
        if cdp:
            try:
                cdp.call("Browser.close")
            except (ConnectionClosed, OSError, RuntimeError, TimeoutError):
                # Browser.close 可能先关闭 WebSocket，再来得及返回响应。
                pass
            cdp.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        # 仅清理这次明确创建的临时目录；不碰用户的 Edge 配置目录。
        if profile.resolve().parent == Path(
            tempfile.gettempdir()
        ).resolve() and profile.name.startswith("ui-smoke-"):
            shutil.rmtree(profile, ignore_errors=True)
    print("\n" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
