"""MCP additions to the existing isolated Edge harness."""

from pathlib import Path


def run(cdp, directory, *, check, close_settings, layout_check, focus_trap_check, send):
    cdp.eval(Path(__file__).with_name("mcp_ui_fixtures.js").read_text(encoding="utf-8"))
    cdp.viewport(1440, 900)
    cdp.click("设置")
    check(cdp.click("MCP"), "MCP 设置分类可导航")
    check(
        cdp.wait("document.body.textContent.includes('尚未添加服务')"), "MCP 状态已读取"
    )
    cdp.click("添加 Tavily 搜索")
    check(
        cdp.wait(
            "!!document.querySelector('[aria-label=\"编辑 MCP 服务\"] input[type=password]')"
        ),
        "Tavily 直接显示 API Key 密码输入",
    )
    check(
        cdp.eval("!document.querySelector('.mcp-advanced').open"),
        "工具与启动详情默认折叠",
    )
    cdp.screenshot(directory, "mcp-tavily-key-desktop")
    cdp.viewport(390, 844)
    layout_check(cdp, "Tavily 密钥手机", composer=False)
    cdp.screenshot(directory, "mcp-tavily-key-mobile")
    cdp.viewport(1440, 900)
    cdp.eval(
        "(()=>{const e=document.querySelector('[aria-label=\"编辑 MCP 服务\"] input[type=password]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,'synthetic-tavily-key');e.dispatchEvent(new Event('input',{bubbles:true}));window.__fixture.mcp.failConnect=true;})()"
    )
    cdp.click("保存并连接")
    check(
        cdp.wait(
            "document.querySelector('[role=alert]')?.textContent.includes('API Key')"
        ),
        "连接失败明确密钥修复入口",
    )
    check(
        cdp.eval(
            "document.querySelector('[aria-label=\"编辑 MCP 服务\"] input[type=password]').value==='synthetic-tavily-key'"
        ),
        "连接失败保留填写的密钥草稿",
    )
    cdp.eval("window.__fixture.mcp.failConnect=false")
    cdp.click("保存并连接")
    check(
        cdp.wait("!document.querySelector('[aria-label=\"编辑 MCP 服务\"]')"),
        "修正后可重新保存并连接",
    )
    cdp.click("配置")
    check(
        cdp.wait(
            "document.querySelector('[aria-label=\"编辑 MCP 服务\"] input[type=password]')?.value===''"
        ),
        "再次编辑不回传密钥原文",
    )
    cdp.eval(
        "[...document.querySelectorAll('label')].find(e=>e.textContent.includes('明确清除')).querySelector('input').click()"
    )
    cdp.click("保存并连接")
    check(
        cdp.wait("Object.keys(window.__fixture.mcpSubmitted.headers).length===0"),
        "明确清除 Tavily 密钥实际提交删除",
    )
    cdp.eval("window.__fixture.mcp.servers=[]")
    cdp.click("刷新状态")
    cdp.wait("document.body.textContent.includes('尚未添加服务')")
    cdp.click("添加 Exa 搜索")
    check(cdp.wait("document.activeElement?.value==='Exa 搜索'"), "新增服务聚焦名称")
    check(
        cdp.eval("window.__fixture.mcp.servers.length===0"), "示例只填草稿，不启动服务"
    )
    cdp.click("保存并连接")
    check(
        cdp.wait(
            "window.__fixture.mcp.servers.length===1&&!document.querySelector('[aria-label=\"编辑 MCP 服务\"]')"
        ),
        "保存 MCP 示例",
    )
    check(
        cdp.eval(
            "window.__fixture.mcp.enabled&&window.__fixture.mcp.servers[0].enabled"
        ),
        "明确保存并连接后协调全局与单服务开关",
    )
    check(
        cdp.eval("document.querySelectorAll('.mcp-tool').length===0"),
        "列表隐藏冗长工具详情",
    )
    cdp.click("配置")
    cdp.eval("document.querySelector('.mcp-advanced').open=true")
    check(
        cdp.wait("document.querySelectorAll('.mcp-tool').length===2"),
        "连接发现两个已选工具",
    )
    cdp.eval(
        "[...document.querySelectorAll('.mcp-tool label')].find(e=>e.textContent.includes('我明确授权')).querySelector('input').click()"
    )
    check(
        cdp.wait("window.__fixture.mcp.servers[0].tools[0].trusted"),
        "只读授权需要用户明确操作",
    )
    cdp.click("测试连接／刷新工具")
    cdp.wait("document.querySelectorAll('.mcp-tool').length===2")
    focus_trap_check(cdp, "设置")
    cdp.screenshot(directory, "mcp-settings-desktop")
    layout_check(cdp, "MCP 桌面设置", composer=False)
    cdp.viewport(390, 844)
    layout_check(cdp, "MCP 手机设置", composer=False)
    cdp.screenshot(directory, "mcp-settings-mobile")
    close_settings(cdp)
    cdp.viewport(1440, 900)
    cdp.click("查看工具")
    check(
        cdp.wait(
            "document.body.textContent.includes('MCP · Exa 联网 / web_search_exa')"
        ),
        "工具清单显示 MCP 来源",
    )
    cdp.click("关闭工具面板")
    for index, mode in enumerate(["自动推理", "先规划", "多专家"]):
        cdp.click(mode)
        cdp.eval("window.__fixture.case='mcp'")
        send(cdp, "使用公开搜索示例")
        card = "document.querySelector('.file-approval[data-approval-id=\"'+window.__fixture.mcpRun.id+'\"]')"
        check(cdp.wait(f"!!({card})"), f"{mode} 显示外部工具审批")
        check(
            cdp.eval(
                f"JSON.parse(({card}).querySelector('pre').textContent).query==='公开 MCP 文档'"
            ),
            "完整调用参数可见",
        )
        if index == 1:
            cdp.viewport(390, 844)
            cdp.eval(f"({card}).scrollIntoView()")
            layout_check(cdp, "MCP 手机确认")
            cdp.screenshot(directory, "mcp-approval-mobile")
            cdp.click("停止生成")
            check(cdp.wait("window.__fixture.mcpCancelled"), "停止确实取消 MCP 流")
            check(cdp.eval(f"!({card}).querySelector('button')"), "停止后不能继续批准")
            cdp.viewport(1440, 900)
        else:
            cdp.click("批准外部调用" if index == 0 else "拒绝外部调用", scope=card)
            check(
                cdp.wait(
                    f"({card}).dataset.status==={'"approved"' if index == 0 else '"rejected"'}"
                ),
                "决定返回后同步审批状态",
            )
            cdp.eval("window.__fixture.mcpRun.finish()")
            check(
                cdp.wait("!document.querySelector('.composer__stop')"), "MCP 流正常收尾"
            )
            check(
                cdp.eval("document.body.textContent.includes('会话未保存')"),
                "独立显示会话保存状态",
            )
    cdp.eval("window.__fixture.case='finished';window.__fixture.restoreMCP()")
