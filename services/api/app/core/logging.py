"""日志配置。

Agent 项目的日志比普通服务更重要：一次失败请求涉及「模型调用 → 工具执行 → 再调用」
多跳，没有结构化日志根本无法定位是哪一跳出了问题。

【两种输出形态，以及它们各自的用途】

    text（默认）—— 带颜色的单行文本，给人看。
    json        —— 每行一个 JSON 对象，给机器看。

**"给人看"和"给机器看"必须分开。** 一个折中方案（比如"带颜色的 JSON"）
两边都不好用：颜色转义会污染字段值，而纯文本无法聚合查询。
所以这里不是把文本日志改造成 JSON，而是**加一个 formatter**，
形态由 LOG_FORMAT 决定 —— 本地开发看文本，部署到有日志系统的地方看 JSON。

【为什么值得做（技术债 T05 的另一半）】

trace id 已经做完了（ContextVar + Filter），但 JSON 这一半一直是空的：
`logging.py` 的注释里写着"生产环境（json 模式）不带颜色"，而代码里
只有彩色文本 formatter —— **注释承诺了一个不存在的能力**。

代价很具体：一次请求会产生十几行日志（HTTP → Agent 多步 → 工具 → LLM），
纯文本要回答"这次请求里工具失败了几次"只能 grep + 人工数；
而结构化之后它是一句聚合查询。日志的价值不在于"有没有记"，
而在于**能不能被聚合**。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, ClassVar

from app.core.telemetry import TraceIdFilter

_CONFIGURED = False

# 日志记录上除标准字段之外的东西，都是调用方通过 `extra=` 附加的。
# 把它们全部带进 JSON，结构化日志才有意义 —— 只输出 message 的话，
# 它和纯文本没有区别，只是多了一层引号。
_LOG_RECORD_BUILTINS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"message", "asctime", "taskName"}


class _ColorFormatter(logging.Formatter):
    """本地开发用彩色日志，生产环境（json 模式）不带颜色。"""

    COLORS: ClassVar[dict[int, str]] = {
        logging.DEBUG: "\033[90m",
        logging.INFO: "\033[36m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelno, "")
        record.levelname = f"{color}{record.levelname:<8}{self.RESET}"
        record.name = f"\033[35m{record.name}\033[0m"
        return super().format(record)


class JsonFormatter(logging.Formatter):
    """每行一个 JSON 对象（JSON Lines）。

    【为什么是 JSON Lines 而不是一个 JSON 数组】
    日志是**流式追加**的：一个数组要么得在退出时补上结尾，要么就不是合法 JSON；
    而 JSON Lines 可以边写边被采集（filebeat / fluentd / 向量数据库）逐行读走。
    崩溃时也只丢最后一行，而不是整个文件不可解析。

    【为什么字段名固定用这几个】
    `ts` / `level` / `logger` / `trace_id` / `message` 与主流采集器的默认约定一致，
    不改配置就能被识别。异常信息放 `exc`（而不是混进 message）：
    message 是要被聚合的短文本，堆栈塞进去会让它每一行都不同，
    聚合结果变成一堆基数极高的"唯一值" —— 那是日志系统里最贵的反模式。

    【序列化必须用 default=str 兜底】
    调用方可能通过 `extra=` 传进来任意对象（比如一个 Pydantic 模型）。
    没有兜底时 `json.dumps` 会抛 TypeError，而**抛出点在一个 formatter 里** ——
    后果是那条日志丢失，并且报错文本与被记录的事件毫无关系。
    降级成字符串虽然不好看，但至少不丢日志；丢日志才是真正的问题。
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": getattr(record, "trace_id", ""),
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key in _LOG_RECORD_BUILTINS or key == "trace_id":
                continue
            payload[key] = value

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # ensure_ascii=False：中文日志保持可读（日志系统按 UTF-8 存即可）。
        # 开着它会把每条中文日志膨胀成 \uXXXX，白占一半体积，人也没法直接看。
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO", *, colorful: bool = True, fmt: str = "text") -> None:
    """配置根 logger。

    Args:
        fmt: `text`（默认，给人看）或 `json`（给机器看）。
             JSON 模式下**忽略** colorful —— 给机器看的东西不该带 ANSI 转义。
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    handler = logging.StreamHandler(sys.stderr)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        # trace_id 放在时间之后、级别之前：它是排查时**第一眼要找的东西**，
        # 埋在行尾会让人习惯性地跳过它
        text_fmt = "%(asctime)s [%(trace_id)s] %(levelname)s %(name)s | %(message)s"
        handler.setFormatter(
            _ColorFormatter(text_fmt, datefmt="%H:%M:%S")
            if colorful
            else logging.Formatter(text_fmt)
        )
    # 【关键】Filter 挂在 handler 上而不是 logger 上。
    # 挂在 logger 上时，通过 propagate 冒泡上来的记录**不会**经过 root logger
    # 的 filter，于是第三方库打的日志里没有 trace_id 字段，
    # 格式化时直接 KeyError 把日志系统打挂。挂在 handler 上才是正确的。
    #
    # JSON 模式下这个 filter 更关键：它是 `trace_id` **字段**的唯一来源。
    handler.addFilter(TraceIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # 第三方库太吵，压到 WARNING
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "watchfiles"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
