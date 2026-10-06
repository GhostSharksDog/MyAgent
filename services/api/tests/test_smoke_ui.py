"""CDP 检查的对照组：没装监听、误匹配按钮或泄露 API 都必须变红。"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("ui_smoke", ROOT / "scripts" / "smoke_ui.py")
assert _spec is not None and _spec.loader is not None
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)


def node_eval(script: str) -> object:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node 未安装，JS 浏览器 fixture 对照组未验证")
    result = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=10,
    )
    return json.loads(result.stdout)


def test_error_collector_reports_all_three_error_sources() -> None:
    result = node_eval(
        "const vm=require('node:vm');const listeners={};const context={"
        "console:{error(){}},addEventListener:(name,cb)=>listeners[name]=cb};"
        "context.window=context;vm.createContext(context);"
        f"vm.runInContext({json.dumps(smoke.ERROR_CAPTURE)},context);"
        "listeners.error({message:'component crashed'});"
        "listeners.unhandledrejection({reason:new Error('request rejected')});"
        "context.console.error('React reported',new Error('render failed'));"
        "process.stdout.write(JSON.stringify(context.__errors__));"
    )
    assert [entry["kind"] for entry in result] == [
        "error",
        "unhandledrejection",
        "console.error",
    ]
    assert result[0]["message"] == "component crashed"
    assert result[1]["message"] == "request rejected"
    assert "render failed" in result[2]["message"]


def test_semantic_locator_does_not_confuse_settings_with_model_settings() -> None:
    result = node_eval(
        "const buttons=["
        "{textContent:'模型设置',getAttribute:()=>null,id:'wrong'},"
        "{textContent:'',getAttribute:()=> '设置',id:'icon-only'},"
        "{textContent:'设置',getAttribute:()=>null,id:'visible-text'}];"
        "const document={querySelectorAll:()=>buttons};"
        f"process.stdout.write(JSON.stringify(({smoke.button_expression('设置')}).id));"
    )
    assert result == "icon-only"


def test_visual_fixtures_block_unknown_api_and_never_reach_real_server() -> None:
    source = (ROOT / "scripts" / "ui_fixtures.js").read_text(encoding="utf-8")
    result = node_eval(
        "const vm=require('node:vm');let network=0;"
        "const context={Response,URL,TextEncoder,ReadableStream,setTimeout,clearTimeout,"
        "location:{href:'http://127.0.0.1:8000/'},sessionStorage:{getItem:()=>null},fetch:async()=>{network++;throw new Error('real fetch forbidden')}};"
        "context.window=context;vm.createContext(context);"
        f"vm.runInContext({json.dumps(source)},context);"
        "(async()=>{const settings=await (await context.fetch('/api/settings')).json();"
        "await context.fetch('/api/settings',{method:'PUT',body:JSON.stringify({workspace_root:'synthetic-only'})});"
        "const unknown=await context.fetch('/api/undeclared',{method:'POST'});"
        "const chat=await context.fetch('/api/chat/stream',{method:'POST',body:'{\"mode\":\"react\"}'});"
        "await chat.text();process.stdout.write(JSON.stringify({network,unknown:unknown.status,"
        "privatePaths:settings.agent.corpus_paths,workspace:context.__fixture.settings.agent.workspace_root,"
        "blocked:context.__fixture.blocked.length}));})().catch(e=>{console.error(e);process.exit(1)});"
    )
    assert result == {
        "network": 0,
        "unknown": 501,
        "privatePaths": [],
        "workspace": "synthetic-only",
        "blocked": 1,
    }
