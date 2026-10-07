"""内置工具集：Agent 在 P1 阶段能用的四把"手"。

工具的选择原则：**只给模型它自己做不到的能力**。
"计算 1234*5678" 模型经常算错（它是逐 token 预测，不做精确算术），
所以计算器是典型该由工具完成的事——这类判断本身就是 Agent 设计能力的一部分。

后续 P2 会加入 RAG 检索工具，P3 会加入简历改写、JD 抓取等业务工具。
"""

from __future__ import annotations

import ast
import json
import math
import operator
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from app.core.config import PROJECT_ROOT
from app.tools.base import ToolRegistry, ToolResult
from app.tools.errors import ToolError, ToolPermissionError

_SEED_DIR: Final[Path] = PROJECT_ROOT / "services" / "api" / "seed"
_DATA_DIR: Final[Path] = PROJECT_ROOT / "data"


# ============================================================
# 工具 1：计算器
# ============================================================
class CalculatorParams(BaseModel):
    expression: str = Field(
        description="要计算的纯算术表达式，例如 '(12000*12)*0.8' 或 '2**10'。只支持算术运算，不支持变量和函数定义。",
        examples=["(12000*12)*0.8", "sum([1,2,3])/3"],
    )


# 白名单式 AST 求值。**绝不要用 eval()** ——
# 模型的参数来自用户输入，eval 等于把一个远程代码执行漏洞直接开放给互联网。
_BIN_OPS: Final[dict[type[ast.operator], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: Final[dict[type[ast.unaryop], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_SAFE_FUNCS: Final[dict[str, Any]] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
}

# 防止 9**9**9 这类表达式把 CPU 算到天荒地老
_MAX_POW_EXPONENT: Final[int] = 1000
_MAX_AST_NODES: Final[int] = 100


def _eval_node(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ToolError(f"不支持的常量类型：{type(node.value).__name__}")
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ToolError(f"不支持的运算符：{type(node.op).__name__}")
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_POW_EXPONENT:
            raise ToolError(f"指数过大（上限 {_MAX_POW_EXPONENT}），已拒绝计算")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ToolError(f"不支持的一元运算符：{type(node.op).__name__}")
        return op(_eval_node(node.operand))
    if isinstance(node, ast.List | ast.Tuple):
        return [_eval_node(e) for e in node.elts]
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _SAFE_FUNCS:
            raise ToolError(f"不支持的函数调用（仅允许 {', '.join(_SAFE_FUNCS)}）")
        return _SAFE_FUNCS[node.func.id](*[_eval_node(a) for a in node.args])
    raise ToolError(f"不支持的语法节点：{type(node).__name__}")


def _clean_number(value: Any) -> Any:
    """清理浮点误差。

    【为什么必须有这一步】
    浮点数不是精确表示：`(12000*12)*0.8` 在 IEEE 754 下算出来是
    115200.00000000001。如果直接把这个数回灌给模型，模型会照着念给用户，
    用户看到的就是一串莫名其妙的尾数——工具的"能用"和"不能用"差别就在这。

    策略：先按 10 位小数修约（足以消除二进制浮点噪声，又不会误伤真实精度），
    若修约后是整数则转为 int，让输出更干净。
    """
    if not isinstance(value, float):
        return value
    if math.isnan(value) or math.isinf(value):
        return value  # NaN/Inf 保持原样，让用户看到真实结果（如 1/0 已被 ZeroDivisionError 拦截）
    rounded = round(value, 10)
    return int(rounded) if rounded.is_integer() else rounded


def _calculator(params: BaseModel) -> ToolResult:
    p = CalculatorParams.model_validate(params.model_dump())
    try:
        tree = ast.parse(p.expression, mode="eval")
    except SyntaxError as exc:
        raise ToolError(f"表达式语法错误：{exc.msg}") from exc

    if sum(1 for _ in ast.walk(tree)) > _MAX_AST_NODES:
        raise ToolError("表达式过于复杂，已拒绝计算")

    value = _clean_number(_eval_node(tree))
    return ToolResult.success(f"{p.expression} = {value}")


# ============================================================
# 工具 2：当前时间
# ============================================================
class TimeParams(BaseModel):
    timezone: str = Field(
        default="Asia/Shanghai",
        description="IANA 时区名，例如 Asia/Shanghai、UTC、America/New_York。",
    )


def _current_time(params: BaseModel) -> ToolResult:
    p = TimeParams.model_validate(params.model_dump())
    try:
        tz = ZoneInfo(p.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ToolError(f"未知时区 {p.timezone!r}，请使用 IANA 时区名，如 Asia/Shanghai") from exc
    now = datetime.now(tz)
    return ToolResult.success(f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S %A')}（{p.timezone}）")


# ============================================================
# 工具 3：读取简历
# ============================================================
class ReadResumeParams(BaseModel):
    """无需参数——刻意设计成零参数，让模型更容易正确调用。"""


def _safe_resolve(base: Path, relative: str) -> Path:
    """防路径穿越。

    模型/用户给出的路径必须落在允许的根目录内。
    `../../.env` 这类输入必须被拦住——这是 Agent 工具最典型的安全漏洞。
    """
    target = (base / relative).resolve()
    if not target.is_relative_to(base.resolve()):
        raise ToolPermissionError("拒绝访问：目标路径超出允许范围")
    return target


def _read_resume(params: BaseModel) -> ToolResult:
    """读取简历原文。

    【为什么需要示例回退】
    `read_resume` 依赖 `data/resume.md`，而 `data/` 被 .gitignore 排除
    （真实简历含手机号、邮箱等个人信息，绝不能进版本库）。
    结果就是：**新克隆的仓库跑起来第一步就失败** —— 典型的
    "开发机上一切正常，别人拿到就跑不起来"问题。

    解法是双轨：
      - 有 `data/resume.md` → 读用户真实简历
      - 没有 → 回退到可提交的 `seed/resume.sample.md`，并**明确告诉模型**
        这是示例数据

    最后那一步很关键：如果不声明，模型会把示例简历当成用户的真实经历
    来分析和提建议，用户会收到完全对不上号的"专业意见"。
    数据来源的诚实性，是 Agent 可信度的底线。
    """
    real_resume = _DATA_DIR / "resume.md"
    if real_resume.exists():
        text = real_resume.read_text(encoding="utf-8", errors="replace")
        if text.strip():
            return ToolResult.success(f"# 简历原文（{real_resume.name}，{len(text)} 字）\n\n{text}")
        return ToolResult.failure("简历文件存在但内容为空，请检查 data/resume.md")

    sample = _SEED_DIR / "resume.sample.md"
    if sample.exists():
        text = sample.read_text(encoding="utf-8", errors="replace")
        return ToolResult.success(
            "⚠️ 重要：当前读到的是**项目内置的示例简历，不是该用户的真实简历**。\n"
            f"用户的真实简历尚未提供（应放在 {real_resume}）。\n"
            "你可以演示分析能力，但**必须明确告知用户这是示例数据**，"
            "不要把它当作该用户的真实经历来评价或给建议。\n\n"
            f"# 示例简历原文（{sample.name}，{len(text)} 字）\n\n{text}"
        )

    return ToolResult.failure(
        f"未找到简历文件。请把简历保存为 Markdown 到 {real_resume}，"
        f"或运行：python scripts/ingest.py <你的简历.pdf> --type resume"
    )


# ============================================================
# 工具 4：岗位检索
# ============================================================
class SearchJobsParams(BaseModel):
    keyword: str = Field(
        default="",
        description="搜索关键词，会匹配岗位名称、技能要求和岗位描述，例如 'Agent'、'Python'、'大模型'。留空则返回全部岗位。",
    )
    city: str = Field(default="", description="城市过滤，例如 '北京'、'上海'。留空表示不限。")
    limit: int = Field(default=3, ge=1, le=10, description="最多返回几条岗位。")


def _search_jobs(params: BaseModel) -> ToolResult:
    p = SearchJobsParams.model_validate(params.model_dump())
    jobs_file = _SEED_DIR / "jobs.json"
    if not jobs_file.exists():
        raise ToolError(f"岗位数据文件缺失：{jobs_file}")

    jobs: list[dict[str, Any]] = json.loads(jobs_file.read_text(encoding="utf-8"))

    kw = p.keyword.strip().lower()
    city = p.city.strip()

    def match(job: dict[str, Any]) -> bool:
        if city and city not in str(job.get("city", "")):
            return False
        if not kw:
            return True
        haystack = " ".join(
            str(job.get(k, "")) for k in ("title", "company", "description", "requirements")
        ).lower()
        # 简单的多关键词 OR 匹配：空格分隔的关键词任一命中即算匹配
        return any(token in haystack for token in kw.split())

    matched = [j for j in jobs if match(j)]
    if not matched:
        available = "、".join(sorted({str(j.get("city", "")) for j in jobs}))
        return ToolResult.failure(
            f"没有找到匹配 keyword={p.keyword!r} city={p.city!r} 的岗位。"
            f"当前数据库共有 {len(jobs)} 条岗位，覆盖城市：{available}。"
        )

    lines = [f"共匹配到 {len(matched)} 条岗位，展示前 {min(len(matched), p.limit)} 条：", ""]
    for job in matched[: p.limit]:
        lines.append(
            f"【{job.get('title')}】{job.get('company')} · {job.get('city')} · "
            f"{job.get('salary', '面议')}\n"
            f"  技能要求：{job.get('requirements', '未提供')}\n"
            f"  岗位描述：{str(job.get('description', ''))[:400]}"
        )
    return ToolResult.success("\n".join(lines))


# ============================================================
# 注册入口
# ============================================================
def build_default_registry(
    long_term_memory: object | None = None,
    *,
    profile: str = "general",
    include_file_tools: bool = True,
    file_write: bool = False,
    terminal: bool = False,
) -> ToolRegistry:
    """按 profile 构造工具集。

    【工具分三层，这个分层是"泛用化"的关键】

        核心层    calculator / get_current_time / search_knowledge
                  —— 任何场景都要用
        文件层    list_dir / read_file / glob / grep（只读）
                  + write_file / edit_file（**只在显式开启时**，见 file_write）
                  —— 需要用户先指定工作区根目录，否则**一个都不注册**
        技能包    read_resume / search_jobs（jobhunt 专属）
                  —— 只在 profile=jobhunt 时加载

    【为什么用"默认不注册"而不是"注册了但返回错误"】
    "工具存在但永远失败"比"工具不存在"更糟：模型看到菜单里有它就会去调，
    拿到错误、浪费一步、然后很可能**换一种方式硬编**。
    **能力不存在时就不该出现在菜单上** —— 这与记忆工具的既有做法一致。

    写工具同样遵守这条：没开启写权限时它们**根本不存在**，
    模型不会尝试、也不会在回答里承诺"我已经帮你写好了"。

    Args:
        long_term_memory: 传入 `LongTermMemory` 时会额外注册 `remember_fact`。
        profile: `general`（默认）或 `jobhunt`。
        include_file_tools: 允许关掉文件工具（测试里用，避免依赖工作区配置）。
        file_write: 是否加载**写**工具（T23）。默认 False —— 写文件不可撤销，
            必须由用户显式开启（`AGENT_FILE_WRITE_ENABLED=true`）。
    """
    registry = ToolRegistry()

    # ---------- 核心层：任何 profile 都有 ----------
    registry.register_fn(
        "calculator",
        "精确计算算术表达式。当你需要进行任何数字运算（求和、百分比、乘法、复利等）时"
        "必须使用本工具，不要自己心算，因为你的算术结果不可靠。",
        CalculatorParams,
        _calculator,
    )

    registry.register_fn(
        "get_current_time",
        "获取指定时区的当前日期与时间。当问题涉及'今天''现在''距今多久'等时间概念时使用。",
        TimeParams,
        _current_time,
    )

    # 语义检索工具（RAG）。延迟 import 的理由：knowledge 模块会拉起 rag 层，
    # 而 rag 层在导入时不做任何 I/O（索引是懒加载的），但让这条依赖
    # 显式出现在装配点，比散落在模块顶层更容易看懂。
    from app.tools.knowledge import KnowledgeSearchTool

    registry.register(KnowledgeSearchTool())

    # ---------- 文件层：要有工作区根目录才注册 ----------
    if include_file_tools:
        from app.tools.files import build_file_tools

        for tool in build_file_tools(include_write=file_write):
            registry.register(tool)

    # 终端是独立权限：设置工作区或开启文件写权限都不会顺便授权命令。
    if terminal:
        from app.tools.terminal import build_terminal_tools

        for tool in build_terminal_tools():
            registry.register(tool)

    # ---------- 技能包：只在 jobhunt 时加载 ----------
    if profile == "jobhunt":
        registry.register_fn(
            "read_resume",
            "读取用户的简历原文。当需要分析、评价、改写简历，或需要了解用户背景、技能、工作经历时"
            "必须先调用本工具获取真实内容，不要凭空猜测用户的经历。",
            ReadResumeParams,
            _read_resume,
        )

        registry.register_fn(
            "search_jobs",
            "在岗位数据库中按关键词和城市检索招聘岗位，返回岗位名称、公司、薪资、技能要求与岗位描述。"
            "当用户想找岗位、做简历与岗位匹配分析、或想了解某类岗位的技能要求时使用。",
            SearchJobsParams,
            _search_jobs,
        )

    # 长期记忆工具：仅在提供了记忆实例时注册。
    # 与上面同一条原则 —— 不提供就不注册，而不是注册一个会报错的空工具。
    if long_term_memory is not None:
        from app.agent.memory import LongTermMemory
        from app.tools.memory_tool import RememberFactTool

        if isinstance(long_term_memory, LongTermMemory):
            registry.register(RememberFactTool(long_term_memory))

    return registry
