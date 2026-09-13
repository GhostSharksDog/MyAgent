"""离线回放测试。

【为什么回放值得单独测】

回放是**演示当天唯一不能失败的东西** —— 它存在的全部意义就是"别的时候都可能出问题，
但这个不会"。所以它的失败模式必须被逐个验证，而不是"跑通一次就算"。

三个关键失败模式：
1. 文件缺失/损坏 → 必须给出**可操作**的提示（怎么重新录制），而不是一个 JSONDecodeError
2. 录制文件是**旧版本事件模型**录的 → 必须在加载时明确失败，
   而不是让前端渲染到一半停下来（那种表现会把排查方向引到前端去）
3. 事件顺序/延迟信息丢失 → 演示时"token 不再是逐个出现"，看起来像普通接口
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.agent.events import AgentEvent, EventType
from app.demo.replay import ReplayTranscript, TranscriptError, build_replayer


def _event(t: EventType, **kw: object) -> dict:
    return json.loads(AgentEvent(type=t, **kw).model_dump_json())


def _write(path: Path, events: list[dict], delays: list[float] | None = None) -> Path:
    path.write_text(
        json.dumps(
            {
                "meta": {
                    "recorded_at": "2026-01-01T00:00:00",
                    "question": "测试问题",
                    "model": "test-model",
                    "delays": delays if delays is not None else [0.0] * len(events),
                },
                "events": [{"event": e} for e in events],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


class TestLoading:
    def test_roundtrip(self, tmp_path: Path) -> None:
        events = [
            _event(EventType.START),
            _event(EventType.TOKEN, content="你"),
            _event(EventType.FINAL, content="你好"),
            _event(EventType.DONE),
        ]
        t = ReplayTranscript.load(_write(tmp_path / "t.json", events))
        assert [e.type for e in t.events] == [
            EventType.START,
            EventType.TOKEN,
            EventType.FINAL,
            EventType.DONE,
        ]

    def test_missing_file_gives_actionable_message(self, tmp_path: Path) -> None:
        """文件不存在时必须告诉人**怎么录一个**，而不是只说"不存在"。

        这里刻意用 `re.escape`：`match` 的参数是**正则**，而 `.` 是元字符 ——
        写成 `match="record_demo.py"` 时它匹配的是 "record_demo" + 任意字符 + "py"，
        断言比看上去的松。这类"以为在断言字面量、实际在断言正则"的偏差
        是测试里很常见的隐性弱化，ruff 的 RUF043 就是专门抓它的。
        """
        import re

        with pytest.raises(TranscriptError, match=re.escape("record_demo.py")):
            ReplayTranscript.load(tmp_path / "nope.json")

    def test_broken_json_is_reported_as_such(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("{ 这不是 json", encoding="utf-8")
        with pytest.raises(TranscriptError, match="不是合法 JSON"):
            ReplayTranscript.load(p)

    def test_wrong_shape_is_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "shape.json"
        p.write_text(json.dumps({"foo": 1}), encoding="utf-8")
        with pytest.raises(TranscriptError, match="格式不符"):
            ReplayTranscript.load(p)

    def test_stale_event_model_fails_loudly(self, tmp_path: Path) -> None:
        """**最重要的一条。**

        录制文件是慢速演化的契约：录的时候事件模型可能是 v1，
        回放时已经改了。如果只做 json.loads 就交给前端，
        坏掉的表现是"前端时间线渲染到一半停了"或"某类事件被静默忽略" ——
        排查方向会跑到前端去，而真正的问题是录制文件过期了。

        所以必须在**加载时**逐条还原成 AgentEvent，让格式问题
        在最靠近它的地方、以最清楚的方式失败。
        """
        p = tmp_path / "stale.json"
        p.write_text(
            json.dumps(
                {
                    "meta": {},
                    # 缺 type 字段（旧模型可能是别的字段名）
                    "events": [{"event": {"content": "旧格式"}}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with pytest.raises(TranscriptError, match="无法还原成 AgentEvent"):
            ReplayTranscript.load(p)

    def test_error_message_names_the_bad_index(self, tmp_path: Path) -> None:
        """报错要指出**第几条**坏了 —— 否则文件有 1000 条事件时无从查起。"""
        events = [_event(EventType.START), {"内容": "坏的"}]
        p = tmp_path / "idx.json"
        p.write_text(
            json.dumps({"meta": {}, "events": [{"event": e} for e in events]}, ensure_ascii=False),
            encoding="utf-8",
        )
        with pytest.raises(TranscriptError, match="第 2 条"):
            ReplayTranscript.load(p)


class TestStreaming:
    async def test_preserves_event_order(self, tmp_path: Path) -> None:
        events = [_event(EventType.TOKEN, content=c) for c in "逐字出现"]
        t = ReplayTranscript.load(_write(tmp_path / "o.json", events))
        got = [e.content async for e in t.stream(speed=1000.0)]
        assert got == list("逐字出现")

    async def test_delays_are_scaled_not_dropped(self, tmp_path: Path) -> None:
        """速度倍率必须**整体缩放**，而不是"延迟全部归零"。

        一次性把所有事件推完虽然数据一样，但演示效果完全不同 ——
        看起来像普通接口返回，而不是流式。
        """
        events = [_event(EventType.FINAL, content="x")] * 3
        p = _write(tmp_path / "d.json", events, delays=[0.2, 0.2, 0.2])
        t = ReplayTranscript.load(p)

        import time

        started = time.perf_counter()
        _ = [e async for e in t.stream(speed=1.0)]
        slow = time.perf_counter() - started

        started = time.perf_counter()
        _ = [e async for e in t.stream(speed=10.0)]
        fast = time.perf_counter() - started

        assert slow > fast, f"加速后应当更快（{slow:.3f}s vs {fast:.3f}s）"

    async def test_long_gaps_are_capped(self, tmp_path: Path) -> None:
        """单次延迟要封顶。

        真实录制里偶尔有几秒的空白（网络抖动），原样回放会让演示出现
        难以解释的冷场。封顶保证演示节奏可预期 —— 而"可预期"
        正是回放存在的理由。
        """
        import time

        events = [_event(EventType.TOKEN, content="a"), _event(EventType.FINAL, content="b")]
        t = ReplayTranscript.load(_write(tmp_path / "gap.json", events, delays=[0.0, 30.0]))

        started = time.perf_counter()
        _ = [e async for e in t.stream(speed=1.0)]
        assert time.perf_counter() - started < 2.0, "30 秒的间隔没有被封顶"

    def test_describe_is_human_readable(self, tmp_path: Path) -> None:
        t = ReplayTranscript.load(_write(tmp_path / "desc.json", [_event(EventType.DONE)]))
        d = t.describe()
        assert "1 个事件" in d
        assert "测试问题" in d


class TestFactory:
    def test_empty_path_is_off(self) -> None:
        assert build_replayer("") is None
        assert build_replayer(None) is None
        assert build_replayer("   ") is None

    def test_broken_file_degrades_instead_of_crashing(self, tmp_path: Path) -> None:
        """回放文件坏了**不该让服务起不来**。

        演示现场如果因为一个损坏的录制文件导致服务启动失败，
        那连演示的其它部分都没了 —— 降级成"走真实链路试试"是更好的选择。
        但必须是 ERROR 级日志，否则就成了静默降级。
        """
        p = tmp_path / "broken.json"
        p.write_text("not json at all", encoding="utf-8")
        assert build_replayer(str(p)) is None

    def test_valid_file_builds(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "ok.json", [_event(EventType.DONE)])
        r = build_replayer(str(p))
        assert r is not None
        assert len(r.events) == 1
