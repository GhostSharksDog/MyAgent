"""Token 估算与上下文预算测试（技术债 T09）。

【这组测试要证明的三件事】

1. **估算不会低估**（在能用 tiktoken 对拍时）。低估是危险方向：以为还剩空间，
   结果超窗报错；高估只是提前少放一点上下文。所以断言是**单向**的 ——
   允许高估到 1.5×，不允许低于 1.0×。
   （tiktoken 是可选依赖：没装就跳过对拍，而不是让测试红。）

2. **裁剪不会拆散 tool 消息的配对**。OpenAI 兼容协议要求 `role=tool`
   紧跟请求它的那条 assistant；丢掉一半，服务端直接 400，
   而报错信息完全看不出是我们的裁剪干的。这条最容易写错，所以单独盯住。

3. **裁剪过的内容要说出来**。静默裁剪是这个功能最危险的形态 ——
   用户发现"它怎么忘了刚才说的"，却没有任何线索。
"""

from __future__ import annotations

import pytest
from app.agent.context import ContextBudget, summarize_tools
from app.llm import tokens as tokens_module
from app.llm.tokens import (
    count_message_tokens,
    count_messages_tokens,
    count_tokens,
    tokenizer_name,
)
from app.llm.types import ChatMessage, ToolCall

# 四类真实输入。第一次量的时候，"工具返回"那一类把启发式打了个 0.83×（低估），
# 于是常数从"非 CJK 一律 4 字符/token"改成"字母数字与符号分开算"。
SAMPLES = {
    "中文对话": (
        "你好，请帮我看看这份简历里有哪些可以改进的地方？"
        "我在一家互联网公司做了三年后端开发，主要用 Python 和 Go，"
        "负责过订单系统的重构，把平均响应时间从 200ms 降到了 80ms。"
    ),
    "英文对话": (
        "I have three years of backend experience with Python and Go, "
        "and I led a refactor that cut p99 latency from 200ms to 80ms."
    ),
    "工具返回": (
        '{"hits": [{"source": "resume.md", "score": 0.82, "text": "负责订单系统重构"}]}'
        "\ndef add(a: int, b: int) -> int:\n    return a + b\n"
    ),
    "中英混排": "用 FastAPI 写一个 /api/chat 接口，要求支持 SSE 流式返回，并在 timeout 时返回 504。",
}


class TestHeuristic:
    def test_empty_is_zero(self) -> None:
        assert count_tokens("") == 0
        assert count_tokens("   ") >= 0

    def test_cjk_costs_more_than_ascii(self) -> None:
        """同样长度，中文的 token 数应当明显更多 —— 这正是 T09 的起因。"""
        chinese = count_tokens("简历筛选用例" * 10)
        english = count_tokens("resume" * 10)
        assert chinese > english * 2, "中文与英文的 token 成本必须分开算"

    def test_single_char_is_at_least_one(self) -> None:
        # 不能用 floor：一个可见字符至少占一个 token，否则短消息会被算成 0
        assert count_tokens("a") >= 1

    def test_message_overhead_counts(self) -> None:
        """每条消息有固定开销（角色名与协议包装），不能只算正文。"""
        empty = count_messages_tokens([ChatMessage.user("")])
        assert empty >= tokens_module.TOKENS_PER_MESSAGE

    def test_tool_call_arguments_count(self) -> None:
        """工具调用的参数也是要发出去的正文，漏掉会显著低估。"""
        with_args = ChatMessage.assistant(
            tool_calls=[
                ToolCall(id="c1", name="search_knowledge", arguments={"query": "简历" * 50})
            ]
        )
        assert count_message_tokens(with_args) > 50

    def test_tokenizer_name_is_visible(self) -> None:
        """必须能看出现在是精确的还是启发式的（精度差一个量级）。"""
        assert tokenizer_name() in {"tiktoken:cl100k_base", "heuristic"}


class TestHeuristicAgainstTiktoken:
    """对拍：**只在装了 tiktoken 时跑**（它是可选依赖）。"""

    @pytest.fixture(autouse=True)
    def _require_tiktoken(self) -> None:
        pytest.importorskip("tiktoken")

    @pytest.mark.parametrize("label", list(SAMPLES))
    def test_never_underestimates(self, label: str) -> None:
        """**永不低于实际值** —— 这是这个估算器唯一的安全性质。

        低估会让"还有空间"的判断出错，结局是请求超窗失败；
        高估只是提前收一点上下文。两者代价不对称，所以断言也是单向的。
        """
        import tiktoken

        encoder = tiktoken.get_encoding("cl100k_base")
        actual = len(encoder.encode(SAMPLES[label]))
        estimated = count_tokens(SAMPLES[label])
        assert estimated >= actual, (
            f"{label}：估算了 {estimated}，实际 {actual} —— 低估了 {actual - estimated} token"
        )

    @pytest.mark.parametrize("label", list(SAMPLES))
    def test_overestimate_stays_within_bounds(self, label: str) -> None:
        """高估也要有上界：超过 1.5× 就说明常数该重新标定了。

        【为什么上界是 1.5 而不是更紧】
        这几个样本上实测是 1.08–1.27×。留到 1.5 是为了让"换了输入类型"不至于
        立刻报警；真正持续偏高的信号由运行期的两个累加计数器给出
        （见 tokens.record_prompt_estimate），这里只需拦住"常数被改坏了"。
        """
        import tiktoken

        encoder = tiktoken.get_encoding("cl100k_base")
        actual = len(encoder.encode(SAMPLES[label]))
        estimated = count_tokens(SAMPLES[label])
        assert estimated <= actual * 1.5, f"{label}：高估到 {estimated / actual:.2f}×，常数该重标了"


class TestBudgetTrim:
    def _messages(self, turns: int, *, body: str = "内容") -> list[ChatMessage]:
        out: list[ChatMessage] = [ChatMessage.system("系统提示")]
        for index in range(turns):
            out.append(ChatMessage.user(f"问题{index} {body}"))
            out.append(ChatMessage.assistant(f"回答{index} {body}"))
        out.append(ChatMessage.user("当前的问题"))
        return out

    def test_disabled_budget_changes_nothing(self) -> None:
        """默认不限制 —— 猜一个上限会把"本来就长但正常"的请求裁出内容。"""
        messages = self._messages(20)
        fitted, report = ContextBudget(0).fit(messages)
        assert fitted == messages
        assert not report.trimmed

    def test_within_budget_is_untouched(self) -> None:
        messages = self._messages(2)
        fitted, report = ContextBudget(10_000, protect_prefix=1).fit(messages)
        assert fitted == messages
        assert report.before_tokens == report.after_tokens

    def test_drops_oldest_and_keeps_the_current_question(self) -> None:
        messages = self._messages(30)
        fitted, report = ContextBudget(200, protect_prefix=1).fit(messages)
        assert report.trimmed
        assert fitted[0].role.value == "system", "系统提示永不被裁"
        assert fitted[-1].content == "当前的问题", "用户当前的问题必须留下"
        assert fitted[-1] is messages[-1]
        assert len(fitted) < len(messages)

    def test_trim_report_is_honest(self) -> None:
        """报告里的数字必须对得上 —— 它是唯一能解释"为什么它忘了"的线索。"""
        messages = self._messages(30)
        fitted, report = ContextBudget(200, protect_prefix=1).fit(messages)
        assert report.dropped_messages == len(messages) - len(fitted)
        assert report.before_tokens > report.after_tokens
        assert "丢弃" in report.describe()

    def test_still_over_is_reported_not_hidden(self) -> None:
        """连"系统提示 + 当前问题"都放不下时要明说，而不是继续裁。"""
        messages = [ChatMessage.system("很长" * 500), ChatMessage.user("问题")]
        fitted, report = ContextBudget(10, protect_prefix=1).fit(messages)
        assert report.still_over
        assert "AGENT_CONTEXT_TOKEN_BUDGET" in report.describe()
        assert len(fitted) == 2, "这时不该再裁 —— 再裁就要动用户当前的问题了"


class TestTrimKeepsToolPairing:
    """裁剪**绝不能**拆散 assistant(tool_calls) 与它的 tool 结果。"""

    def _conversation_with_tools(self, rounds: int) -> list[ChatMessage]:
        out: list[ChatMessage] = [ChatMessage.system("系统提示")]
        for index in range(rounds):
            out.append(ChatMessage.user(f"问题{index}"))
            out.append(
                ChatMessage.assistant(
                    tool_calls=[
                        ToolCall(
                            id=f"call_{index}", name="search_knowledge", arguments={"q": str(index)}
                        )
                    ]
                )
            )
            out.append(
                ChatMessage.tool_result(
                    tool_call_id=f"call_{index}", content="检索结果" * 200, name="search_knowledge"
                )
            )
            out.append(ChatMessage.assistant(f"回答{index}"))
        out.append(ChatMessage.user("当前的问题"))
        return out

    def test_trim_never_orphans_tool_messages(self) -> None:
        """**这是本组最重要的一条。**

        丢一半的工具消息组 → 服务端 400，而报错只说
        "tool_call_id 找不到对应的调用"，完全看不出是裁剪干的。
        """
        messages = self._conversation_with_tools(12)
        fitted, report = ContextBudget(500, protect_prefix=1).fit(messages)
        assert report.trimmed, "这个预算下应当确实裁过，否则测不到东西"

        pending: set[str] = set()
        for message in fitted:
            if message.role.value == "assistant" and message.tool_calls:
                pending = {call.id for call in message.tool_calls}
            elif message.role.value == "tool":
                assert message.tool_call_id in pending, (
                    f"孤儿 tool 消息：{message.tool_call_id} 的调用方被裁掉了"
                )
                pending.discard(message.tool_call_id)
            elif message.role.value == "assistant":
                # 纯文本回答出现时，说明这一组已经收尾
                pending = set()
        assert not pending, f"有 tool_calls 没等到结果：{pending}"

    def test_group_is_dropped_whole(self) -> None:
        """整组一起走：留下的组里 assistant 与 tool 的数量必须配对。"""
        messages = self._conversation_with_tools(12)
        fitted, _ = ContextBudget(500, protect_prefix=1).fit(messages)
        calls = sum(len(m.tool_calls or []) for m in fitted if m.role.value == "assistant")
        results = sum(1 for m in fitted if m.role.value == "tool")
        assert calls == results, f"调用 {calls} 次但只有 {results} 个结果 —— 配对被拆了"


class TestToolSummary:
    """T07：把"查过什么"压成一行，让下一轮不再重复调用。"""

    def test_empty_trace_is_empty_string(self) -> None:
        assert summarize_tools([]) == ""

    def test_counts_repeats_and_failures(self) -> None:
        text = summarize_tools(
            [
                {"name": "search_knowledge", "ok": True, "chars": 1200},
                {"name": "search_knowledge", "ok": True, "chars": 800},
                {"name": "read_file", "ok": False, "chars": 60},
            ]
        )
        assert "search_knowledge×2" in text
        assert "read_file" in text
        assert "1 次失败" in text
        assert "2.0k 字" in text

    def test_preserves_first_seen_order(self) -> None:
        """ "先检索再读文件"与"先读文件再检索"对模型意义不同：那是推理路径。"""
        text = summarize_tools(
            [
                {"name": "search_knowledge", "ok": True},
                {"name": "read_file", "ok": True},
                {"name": "search_knowledge", "ok": True},
            ]
        )
        assert text.index("search_knowledge") < text.index("read_file")

    def test_summary_is_short(self) -> None:
        """它的价值就在于**短**：原始工具输出动辄几千字，这行是几十 token。"""
        trace = [{"name": f"tool_{i}", "ok": True, "chars": 4000} for i in range(8)]
        assert count_tokens(summarize_tools(trace)) < 200
