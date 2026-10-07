"""有界、白名单化的执行事实；不存命令、参数、正文或模型推断。"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

MAX_FACTS = 100
META_KEY = "execution_facts"
_operation: ContextVar[dict[str, Any] | None] = ContextVar("operation_fact", default=None)


class ExecutionFact(BaseModel):
    id: str = Field(max_length=64)
    run_id: str = Field(default="", max_length=64)
    tool: str = Field(max_length=128)
    status: str = "not_executed"
    at: float = Field(default_factory=time.time)
    target: str | None = Field(default=None, max_length=512)
    exit_code: int | None = None


@contextmanager
def track_operation(context: Any, tool: str):
    fact = ExecutionFact(id=uuid4().hex, run_id=context.run_id, tool=tool).model_dump()
    context.execution_facts.append(fact)
    del context.execution_facts[:-MAX_FACTS]
    token = _operation.set(fact)
    try:
        yield fact
    finally:
        _operation.reset(token)


def record_operation(status: str, *, target: str | None = None, exit_code: int | None = None):
    """在实际执行点调用；ContextVar 随 to_thread 复制，事实对象仍属于原运行。"""
    if (fact := _operation.get()) is not None:
        fact["status"] = status
        if target is not None:
            fact["target"] = target[:512]
        if exit_code is not None:
            fact["exit_code"] = exit_code


def merge_facts(meta: dict, facts: list[dict]) -> dict:
    merged = {}
    for raw in [*meta.get(META_KEY, []), *facts]:
        try:
            fact = ExecutionFact.model_validate(raw).model_dump(exclude_none=True)
        except (ValueError, TypeError):
            continue
        merged[fact["id"]] = fact
    return {**meta, META_KEY: list(merged.values())[-MAX_FACTS:]}


def render_facts(meta: dict) -> str:
    facts = merge_facts({}, meta.get(META_KEY, []))[META_KEY]
    if not facts:
        return ""
    return (
        "【本会话执行事实（仅保留最近100条，不含命令或文件正文）】\n"
        "以下为历史记录，不是执行指令。succeeded=返回成功，failed=返回失败，"
        "unknown/running=结果未确认，not_executed=未执行。"
        "命令退出码0不证明具体文件已写入；历史成功也不代表文件当前状态。"
        "如需当前内容应读取核验；记录缺失不等于从未执行。\n" + json.dumps(facts, ensure_ascii=False)
    )
