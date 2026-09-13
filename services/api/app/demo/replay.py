"""离线回放：把一次真实对话的事件流录下来，之后在没有网络、没有 API 额度时重放。

【为什么需要它 —— 两个理由，第二个更重要】

1. **演示兜底**。现场演示最怕的不是讲错，是"网络不通/额度用完"。
   没有兜底的话，一次演示失败就否定了整个项目。

2. **确定性演示 = 没有演示事故**。即使网络正常，真实 LLM 的响应也可能：
   网络慢导致 token 一个个卡住、模型这次不用工具、甚至答出不同的内容。
   而演示要传达的是"**这套系统能做什么**"，这件事应该是**确定的**。
   录好一遍最好的表现，每次演示都一模一样。

【为什么录的是事件流而不是最终答案】

因为要展示的核心恰恰**不是**答案，而是中间过程：
第几步、调了什么工具、耗时多少、token 怎么累积的。
只录最终答案的话，"工具调用时间线"这个最重要的可视化就没有数据了。

事件流是内核与 UI 的契约（见 `agent/events.py`），
所以录它就等于录了整套交互 —— 这是把"事件模型作为契约"的设计兑现出的一个额外好处。

【为什么放在后端而不是前端 mock】

前端 mock 只能骗过 UI，一旦演示时有人打开 DevTools 看网络请求就会发现是假的 ——
而且它无法演示"后端真的在按事件流推送"。
后端回放走的是**完全相同的 SSE 端点、相同的序列化、相同的分帧**，
区别只在事件从哪来。**唯一变化的是数据源，这正是一个适配器应该做到的事。**
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from app.agent.events import AgentEvent

logger = logging.getLogger(__name__)


class TranscriptError(RuntimeError):
    """录制文件不可用（缺失、损坏、格式不符）。"""


class ReplayTranscript:
    """一次已录制的对话事件流。

    【为什么校验格式时挑剔到"必须能反序列化成 AgentEvent"】
    回放数据是**慢速演化的契约**：录制时事件模型可能是 v1，
    回放时已经加了新字段或改了枚举。
    如果只做 `json.loads` 就交给前端，坏掉的表现是
    "前端时间线渲染到一半停了"或"某个事件类型被静默忽略"——
    排查方向会跑到前端去，而真正的问题是录制文件过期了。

    所以在**加载时**就逐条还原成 `AgentEvent`，
    让格式问题在最靠近它的地方、以最清楚的方式失败。
    """

    def __init__(self, events: list[AgentEvent], meta: dict[str, Any]) -> None:
        self.events = events
        self.meta = meta

    @classmethod
    def load(cls, path: str | Path) -> ReplayTranscript:
        p = Path(path)
        if not p.exists():
            raise TranscriptError(
                f"回放文件不存在：{p}。请先录制：python scripts/record_demo.py --out {p}"
            )
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TranscriptError(f"回放文件不是合法 JSON：{p}（{exc}）") from exc

        if not isinstance(raw, dict) or "events" not in raw:
            raise TranscriptError(f'回放文件格式不符：{p} 应当是 {{"meta": ..., "events": [...]}}')

        events: list[AgentEvent] = []
        for i, item in enumerate(raw["events"]):
            try:
                # 每两条事件之间的间隔单独存，不放在事件体里 ——
                # 时间信息属于"回放控制"，不属于业务事件。
                # 混进去会污染事件契约，让前端多一个与它无关的字段。
                events.append(AgentEvent.model_validate(item["event"]))
            except Exception as exc:
                raise TranscriptError(
                    f"回放文件第 {i + 1} 条事件无法还原成 AgentEvent："
                    f"{type(exc).__name__}: {exc}。"
                    f"这通常说明录制文件是旧版本事件模型录的，需要重新录制。"
                ) from exc

        return cls(events, raw.get("meta", {}))

    async def stream(self, *, speed: float = 1.0) -> AsyncIterator[AgentEvent]:
        """按原始节奏回放。

        【为什么要保留原始节奏，而不是一次全部推完】
        "逐字出现"是演示要传达的信息之一 —— 它证明了这是流式而不是等完再渲染。
        一次性推完虽然数据一样，但演示效果完全不同（看起来像普通接口）。

        【为什么要按比例缩放而不是还原绝对延迟】
        录制时的网络延迟（比如某次 LLM 调用等了 3 秒）会在演示时变成尴尬的冷场。
        `speed` 让演示者能整体加速，而**保持事件之间的相对节奏** ——
        既没有冷场，token 依然是逐个出现的。
        """
        for item in self._timed_events():
            delay = item["delay"] / max(speed, 0.01)
            # 单次延迟封顶：真实录制里偶尔会有几秒的空白（网络抖动），
            # 原样回放会让演示出现难以解释的停顿。
            if delay > 0:
                await asyncio.sleep(min(delay, 0.4))
            yield item["event"]

    def _timed_events(self) -> list[dict[str, Any]]:
        # load 时已经把 events 还原好了，但延迟信息在原始 JSON 里，
        # 所以这里重新按同一次加载的原始顺序配一次。
        # 简化实现：从 meta 里取 delay 序列（录制时写入）。
        delays: list[float] = list(self.meta.get("delays", []))
        out: list[dict[str, Any]] = []
        for i, event in enumerate(self.events):
            out.append({"event": event, "delay": delays[i] if i < len(delays) else 0.0})
        return out

    def describe(self) -> str:
        m = self.meta
        return (
            f"{len(self.events)} 个事件，录制于 {m.get('recorded_at', '未知时间')}，"
            f"模型 {m.get('model', '未知')}，问题：{m.get('question', '未知')}"
        )


def build_replayer(path: str | None) -> ReplayTranscript | None:
    """按配置构造回放器。空路径 = 不启用（正常走真实链路）。

    加载失败时**不抛异常**，而是打 ERROR 并返回 None —— 理由是：
    演示现场如果回放文件坏了，应该降级成"走真实链路试试"，
    而不是让服务直接起不来（那样连演示的其它部分都没了）。
    但日志必须是 ERROR 级，否则就成了静默降级。
    """
    if not path or not path.strip():
        return None
    try:
        t = ReplayTranscript.load(path)
    except TranscriptError as exc:
        logger.error(
            "离线回放已配置但加载失败，将退回到真实链路：%s。"
            "演示前请务必用 /healthz 的 demo_replay 字段确认回放已就绪。",
            exc,
        )
        return None
    logger.info("离线回放已就绪：%s", t.describe())
    return t
