"""流式协议解析测试。

这部分逻辑最容易出错也最难手测：
tool_calls 分片到达时，id/name 只在首片、arguments 需要字符串拼接、
usage 在最后一个 chunk 才出现且此时 choices 可能为空。

把它和网络层隔离后就能纯函数式地测 —— 这正是 StreamAccumulator
单独成类的理由。
"""

from __future__ import annotations

from app.llm.client import LLMClient, StreamAccumulator
from app.llm.types import StreamDelta, ToolCall, Usage


def _tc_delta(index: int, **fn: object) -> StreamDelta:
    payload: dict[str, object] = {"index": index}
    if "id" in fn:
        payload["id"] = fn["id"]
    payload["function"] = {k: v for k, v in fn.items() if k != "id"}
    return StreamDelta(tool_call_delta=payload)


class TestStreamAccumulator:
    def test_plain_text_concatenation(self) -> None:
        acc = StreamAccumulator()
        for piece in ["你好", "，我是", " JobPilot"]:
            acc.feed(StreamDelta(content=piece))
        assert acc.content == "你好，我是 JobPilot"
        assert acc.tool_calls() is None
        assert acc.build_message().role == "assistant"
        assert acc.finish_reason == "stop"

    def test_accumulator_appends_not_overwrites(self) -> None:
        """增量是"追加"语义，不是"覆盖"——写错了会丢掉前面所有内容。"""
        acc = StreamAccumulator()
        for piece in ["A", "B", "C"]:
            acc.feed(StreamDelta(content=piece))
        assert acc.content == "ABC"

    def test_tool_call_fragmented_across_chunks(self) -> None:
        """真实场景：一个工具调用的参数被切成 4 个 SSE 事件推送。"""
        acc = StreamAccumulator()
        acc.feed(_tc_delta(0, id="call_abc", name="calculator", arguments=""))
        acc.feed(_tc_delta(0, arguments='{"expr'))
        acc.feed(_tc_delta(0, arguments='ession": "2'))
        acc.feed(_tc_delta(0, arguments='+2"}'))
        acc.feed(StreamDelta(finish_reason="tool_calls"))

        calls = acc.tool_calls()
        assert calls is not None
        assert len(calls) == 1
        assert calls[0].id == "call_abc"
        assert calls[0].name == "calculator"
        assert calls[0].arguments == {"expression": "2+2"}
        assert acc.finish_reason == "tool_calls"

    def test_multiple_parallel_tool_calls_kept_separate(self) -> None:
        """模型一次要调两个工具：必须按 index 分流，不能串在一起。"""
        acc = StreamAccumulator()
        acc.feed(_tc_delta(0, id="c0", name="read_resume", arguments='{"a'))
        acc.feed(_tc_delta(1, id="c1", name="search_jobs", arguments='{"b'))
        acc.feed(_tc_delta(0, arguments='": 1}'))
        acc.feed(_tc_delta(1, arguments='": 2}'))

        calls = acc.tool_calls()
        assert calls is not None
        assert [c.name for c in calls] == ["read_resume", "search_jobs"]
        assert calls[0].arguments == {"a": 1}
        assert calls[1].arguments == {"b": 2}
        assert [c.id for c in calls] == ["c0", "c1"]

    def test_empty_arguments_tolerated(self) -> None:
        """零参数工具的参数是空字符串，聚合后应得到空 dict 而不是报错。"""
        acc = StreamAccumulator()
        acc.feed(_tc_delta(0, id="c0", name="read_resume", arguments=""))
        calls = acc.tool_calls()
        assert calls is not None
        assert calls[0].arguments == {}

    def test_usage_captured_from_final_chunk(self) -> None:
        acc = StreamAccumulator()
        acc.feed(StreamDelta(content="hi"))
        acc.feed(
            StreamDelta(
                finish_reason="stop",
                usage=Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
            )
        )
        assert acc.usage.total_tokens == 120
        assert acc.usage.prompt_tokens == 100

    def test_mixed_text_and_tool_call(self) -> None:
        """模型经常会先说一句'我来查一下'，再发起工具调用。"""
        acc = StreamAccumulator()
        acc.feed(StreamDelta(content="我来查一下"))
        acc.feed(_tc_delta(0, id="c0", name="read_resume", arguments="{}"))
        msg = acc.build_message()
        assert msg.content == "我来查一下"
        assert msg.tool_calls is not None


class TestChunkParsing:
    """解析层：把 SSE 的 JSON 变成我们的类型。"""

    def test_content_chunk(self) -> None:
        delta = LLMClient._parse_chunk(
            {"choices": [{"delta": {"content": "你好"}, "finish_reason": None}]}
        )
        assert delta.content == "你好"
        assert delta.finish_reason is None

    def test_usage_only_chunk_with_empty_choices(self) -> None:
        """最后一个 chunk 常常只有 usage，没有 choices —— 不能因此索引越界。"""
        delta = LLMClient._parse_chunk(
            {
                "choices": [],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
            }
        )
        assert delta.usage is not None
        assert delta.usage.total_tokens == 12
        assert delta.content == ""

    def test_tool_call_first_fragment(self) -> None:
        delta = LLMClient._parse_chunk(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "c1", "function": {"name": "f", "arguments": ""}}
                            ]
                        }
                    }
                ]
            }
        )
        assert delta.tool_call_delta is not None
        assert delta.tool_call_delta["id"] == "c1"


class TestToolCallWire:
    """ToolCall 的序列化必须能被服务端接受。"""

    def test_round_trip(self) -> None:
        call = ToolCall.from_wire(
            {"id": "c1", "function": {"name": "calculator", "arguments": '{"expression":"1+1"}'}}
        )
        wire = call.to_wire()
        assert wire["type"] == "function"
        assert wire["id"] == "c1"
        assert wire["function"]["name"] == "calculator"
        # arguments 必须还原成**字符串**，这是协议要求
        assert isinstance(wire["function"]["arguments"], str)
        assert '"expression"' in wire["function"]["arguments"]

    def test_arguments_already_dict(self) -> None:
        """少数兼容实现直接返回对象而不是字符串，也要能处理。"""
        call = ToolCall.from_wire({"id": "c", "function": {"name": "f", "arguments": {"a": 1}}})
        assert call.arguments == {"a": 1}
