"""首次引导、密钥表单与记忆管理的真实 Edge DOM 验收。"""

from pathlib import Path


def run(cdp, directory, *, check, close_settings, layout_check, focus_trap_check, send):
    source = (
        Path(__file__).with_name("desktop_ui_fixtures.js").read_text(encoding="utf-8")
    )
    script = cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": source})[
        "result"
    ]["identifier"]
    try:
        cdp.eval("sessionStorage.setItem('smoke-first-setup','true')")
        cdp.reload()
        check(
            cdp.wait("!!document.querySelector('.setup-dialog')"),
            "首次无模型显示简短引导",
        )
        check(
            cdp.eval("document.querySelectorAll('.setup-dialog input').length===3"),
            "首次引导仅 API 地址、模型名、API Key",
        )
        check(
            cdp.eval(
                "document.querySelector('.setup-dialog input[type=password]')!==null"
            ),
            "密钥使用密码输入",
        )
        check(
            cdp.eval("document.body.textContent.includes('保存在本机')"),
            "首次告知本机存储",
        )
        focus_trap_check(cdp, "首次配置", selector=".setup-dialog")
        cdp.viewport(1440, 900)
        layout_check(cdp, "首次配置桌面", composer=False)
        cdp.screenshot(directory, "desktop-setup-1440")
        cdp.viewport(390, 844)
        layout_check(cdp, "首次配置手机", composer=False)
        cdp.screenshot(directory, "desktop-setup-390")
        cdp.eval(
            "(()=>{const e=document.querySelector('.setup-dialog input[type=password]');const s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;s.call(e,'synthetic-api-key');e.dispatchEvent(new Event('input',{bubbles:true}));})()"
        )
        check(cdp.click("保存并开始使用"), "首次保存按钮直白可见")
        check(
            cdp.wait("!document.querySelector('.setup-dialog')"), "保存模型后关闭引导"
        )
        check(
            cdp.eval(
                "window.__fixture.desktop.setup.length===1&&!window.__fixture.requests.some(v=>v.path==='/api/settings/test'||v.path==='/api/chat/stream')"
            ),
            "保存不触发模型测试或聊天",
        )
        cdp.viewport(1440, 900)
        cdp.click("设置")
        check(cdp.click("记忆与存储"), "记忆管理可导航")
        check(
            cdp.wait("document.body.textContent.includes('对话已持久保存')"),
            "展示实际存储状态",
        )
        cdp.eval(
            "(()=>{const e=document.querySelector('[aria-label=\"记忆与存储\"] textarea');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(e,'用户偏好简短中文回复');e.dispatchEvent(new Event('input',{bubbles:true}));})()"
        )
        cdp.click("确认并保存记忆")
        check(
            cdp.wait(
                "document.querySelector('.memory-fact')?.textContent.includes('简短中文')"
            ),
            "用户明确新增记忆",
        )
        cdp.screenshot(directory, "desktop-memory-1440")
        cdp.viewport(390, 844)
        layout_check(cdp, "记忆管理手机", composer=False)
        cdp.screenshot(directory, "desktop-memory-390")
        cdp.eval(
            "document.querySelector('.memory-fact').scrollIntoView({block:'center'})"
        )
        cdp.screenshot(directory, "desktop-memory-fact-390")
        cdp.click("编辑")
        check(
            cdp.wait(
                "document.querySelector('[aria-label=\"记忆与存储\"] textarea')?.value.includes('简短中文')"
            ),
            "记忆可编辑",
        )
        cdp.eval(
            "(()=>{const e=document.querySelector('[aria-label=\"记忆与存储\"] textarea');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(e,'用户偏好详细中文回复');e.dispatchEvent(new Event('input',{bubbles:true}));})()"
        )
        cdp.click("保存修改")
        check(
            cdp.wait(
                "document.querySelector('.memory-fact')?.textContent.includes('详细中文')"
            ),
            "记忆修改反馈真实更新",
        )
        cdp.viewport(1440, 900)
        cdp.eval(
            "document.querySelector('.memory-fact').scrollIntoView({block:'center'})"
        )
        cdp.screenshot(directory, "desktop-memory-fact-1440")
        cdp.eval(
            "[...document.querySelectorAll('label')].find(e=>e.textContent.includes('使用长期记忆')).querySelector('input').click()"
        )
        check(
            cdp.wait(
                "!window.__fixture.desktop.enabled&&window.__fixture.desktop.facts.length===1"
            ),
            "关闭记忆保留已有数据",
        )
        cdp.eval(
            "[...document.querySelectorAll('label')].find(e=>e.textContent.includes('使用长期记忆')).querySelector('input').click()"
        )
        check(
            cdp.wait(
                "window.__fixture.desktop.enabled&&window.__fixture.desktop.facts.length===1"
            ),
            "重新开启记忆保留原内容",
        )
        focus_trap_check(cdp, "设置")
        close_settings(cdp)
        cdp.viewport(1440, 900)
        cdp.click("自动推理")
        cdp.eval("window.__fixture.case='memory-confirmation'")
        send(cdp, "请确认后记住公开表达偏好")
        card = "document.querySelector('.file-approval[data-approval-id=\"'+window.__fixture.desktop.memoryRun.id+'\"]')"
        check(cdp.wait(f"!!({card})"), "记忆调用显示完整内容确认卡")
        check(
            cdp.eval("window.__fixture.desktop.facts.length===1"),
            "批准前没有保存模型提出的记忆",
        )
        cdp.viewport(390, 844)
        layout_check(cdp, "记忆确认手机")
        cdp.screenshot(directory, "desktop-memory-approval-390")
        cdp.click("确认保存记忆", scope=card)
        check(
            cdp.wait(f"({card}).dataset.status==='approved'"), "批准状态不冒充保存成功"
        )
        cdp.eval("window.__fixture.desktop.memoryRun.finish()")
        check(
            cdp.wait(f"({card}).dataset.status==='applied'"), "提交成功才显示记忆已保存"
        )
        check(
            cdp.eval("window.__fixture.desktop.facts.length===2"),
            "合成记忆只在完成保存时新增",
        )
        send(cdp, "公开记忆拒绝测试")
        check(cdp.wait(f"!!({card})"), "下一轮记忆确认重新展示")
        cdp.click("不保存", scope=card)
        check(
            cdp.wait(f"({card}).dataset.status==='rejected'"), "记忆拒绝明确显示未保存"
        )
        cdp.eval("window.__fixture.desktop.memoryRun.finish()")
        check(
            cdp.wait("!document.querySelector('.composer__stop')")
            and cdp.eval("window.__fixture.desktop.facts.length===2"),
            "拒绝后未新增记忆",
        )
        send(cdp, "公开记忆取消测试")
        check(cdp.wait(f"!!({card})"), "停止前记忆仍待确认")
        cdp.click("停止生成")
        check(cdp.wait("window.__fixture.desktop.cancelled"), "停止实际取消记忆确认流")
        check(
            cdp.eval(
                f"!({card}).querySelector('button')&&window.__fixture.desktop.facts.length===2"
            ),
            "停止后确认失效且未保存",
        )
        cdp.viewport(1440, 900)
        cdp.eval("window.__fixture.case='finished'")
    finally:
        cdp.call("Page.removeScriptToEvaluateOnNewDocument", {"identifier": script})
        cdp.eval(
            "sessionStorage.removeItem('smoke-first-setup');window.__fixture.desktop?.restore()"
        )
