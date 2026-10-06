"""Token 估算。

《为什么需要它 —— 技术债 T09 的原文》

> `MAX_OBSERVATION_CHARS = 8000` 是**字符**数：中文场景约 5k–8k token，偏大；
> 也因此做不了真正的 token 预算与超窗保护。

把"字数"当"token 数"用，在中文与英文之间的误差是**四倍量级**：
同样 8000 个字符，中文约 8000–12000 token，英文只有约 2000 token。
于是同一个上限对中文用户是"随时可能撞窗口"，对英文用户是"限制得莫名其妙"。
预算要按 token 算，第一步就得先能量出 token。

《为什么不用 len(text)/4 这种一行写法》

那是英文的近似。中文里一个字就接近一个 token，用 /4 会**低估四倍**，
而低估的后果是"以为还剩很多空间，结果超窗报错"。
估算必须区分字符集，并且**宁可高估**：

    低估 → 超窗 → 请求直接失败（用户看到的是报错）
    高估 → 提前裁剪 → 少了些上下文（用户看到的还是回答）

所以本模块的非对称原则是：**宁可多算**。

《tiktoken 是可选的，而不是依赖》

它是 OpenAI 系的精确分词器：装了就用（对 GPT/DeepSeek 这类 BPE 词表准确），
没装就用启发式。理由与 `fakeredis` / `pypdf` 一致 ——
一个"只影响估算精度、不影响功能"的依赖，不该成为别人 clone 后的安装门槛。
（T17 那条测试盯着这件事：代码里 import 了就必须在 pyproject 里声明，
所以它被声明成了可选 extra `[tokens]`。）

《精度不靠声称，靠测量》

估算器准不准，是在**真实流量**上量出来的：模型响应里的 `usage.prompt_tokens`
是权威值，而我们知道自己发了多少估算 token。于是每次调用都能得到一对
（估算, 实际），两个累加计数器一除就是偏差倍数 —— 见 `record_prompt_estimate`。
不写死"误差 < 10%"这种没有依据的话，而是把它变成一个可观测的指标。
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from app.llm.types import ChatMessage

# ============================================================
# 启发式常数 —— 每一个都是**量出来**的，不是拍出来的
# ============================================================
#
# 方法：拿四类真实输入（中文对话 / 英文对话 / 工具返回的 JSON 与代码 / 中英混排）
# 用 tiktoken 的 cl100k_base 算出权威值，然后在常数网格上找"所有样本都不低估
# 且高估最少"的组合（脚本见 git 历史里的探针）。
#
# 关键的一次修正：第一版把非 CJK 一律按 4 字符/token 算，结果**工具返回那一类
# 低估到 0.83×** —— 标点密集的文本（`{"hits": [{"source": ...`）token 化效率
# 低得多，几乎一个符号一个 token。低估正是最危险的方向（见模块开头），
# 所以现在把"字母数字"与"符号"分开算。
#
# 选定值（CJK 1.4 / 字母数字 4.0 / 符号 1.5）在这四类样本上的实测比值：
#
#     中文对话  1.27×      英文对话  1.08×
#     工具返回  1.10×      中英混排  1.25×
#
# 方向恒为高估（1.08–1.27×），而且**刻意留了余量**：网格里"最紧"的一组是
# 1.3/4.2/1.6（最大 1.18×），但它在 JSON 样本上只有 1.00× —— 紧贴边界意味着
# 换一批输入就可能翻到低估那一侧。多算 10% 换一个不会翻车的下限，划算。
#
# 真实流量上的偏差不靠这段注释保证，而是由两个累加计数器持续测量（见文件末尾）。

#: CJK 字符的平均 token 数（含中文标点、全角字符、日文假名、韩文音节）。
CJK_TOKENS_PER_CHAR = 1.4

#: 字母、数字、空白：每个 token 覆盖的字符数。英文约 4，代码里的标识符接近 3。
ALNUM_CHARS_PER_TOKEN = 4.0

#: 标点与其它符号：每个 token 覆盖的字符数。
#: 这一条是上表里最关键的一个数 —— 它决定 JSON/代码那类的估算方向。
SYMBOL_CHARS_PER_TOKEN = 1.5

#: 每条消息的固定开销：角色名、分隔符、以及 OpenAI 协议里的包装。
#: 官方文档给出的经验值是"每条消息约 3–4 个 token 外加名字"，
#: 这里取 4 —— 一轮对话几十条消息时，这部分是实打实的一两百 token。
TOKENS_PER_MESSAGE = 4


def _is_cjk(char: str) -> bool:
    """是否属于"CJK 表意文字及中日韩标点"。

    用码点范围而不是正则 `[\\u4e00-\\u9fff]`：范围与量都摆在这里，
    而且**把标点也算进来** —— 中文标点同样占一个 token，漏掉会低估。
    """
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF  # 基本汉字
        or 0x3400 <= code <= 0x4DBF  # 扩展 A
        or 0x3000 <= code <= 0x303F  # 中文标点
        or 0xFF00 <= code <= 0xFFEF  # 全角字符
        or 0x3040 <= code <= 0x30FF  # 日文假名（词表行为同类）
        or 0xAC00 <= code <= 0xD7AF  # 韩文音节
    )


def _heuristic_tokens(text: str) -> int:
    """按三类字符分别估算，再向上取整。"""
    if not text:
        return 0
    cjk = 0
    alnum = 0
    symbol = 0
    for char in text:
        if _is_cjk(char):
            cjk += 1
        elif char.isalnum() or char.isspace():
            alnum += 1
        else:
            symbol += 1
    return math.ceil(
        cjk * CJK_TOKENS_PER_CHAR + alnum / ALNUM_CHARS_PER_TOKEN + symbol / SYMBOL_CHARS_PER_TOKEN
    )


# ============================================================
# 精确路径（可选依赖）
# ============================================================
@lru_cache(maxsize=1)
def _encoder() -> Any | None:
    """拿到 tiktoken 编码器，拿不到就返回 None。

    【为什么缓存、以及为什么缓存 None】
    加载编码器要读词表文件（首次还要下载），每次估算都做一遍是不可接受的；
    而"没有 tiktoken"这个结果同样要缓存 —— 否则每次估算都要走一遍
    import 失败的异常路径，那比估算本身还贵。
    """
    try:
        import tiktoken  # 可选依赖：装了就用，没装退回启发式
    except ImportError:
        return None
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        # 词表下载失败 / 离线：退回启发式即可。
        # 这里刻意接住所有异常 —— 一个"只影响估算精度"的可选依赖，
        # 不该因为任何原因让请求失败。
        return None


def tokenizer_name() -> str:
    """当前实际使用的估算方式（显示在 /healthz 与启动日志里）。

    必须能看出来"现在是精确的还是启发式的"：两者的精度差一个量级，
    而如果只显示一个 token 数，没人知道该不该信它。
    """
    return "tiktoken:cl100k_base" if _encoder() is not None else "heuristic"


def count_tokens(text: str) -> int:
    """估算一段文本的 token 数。"""
    encoder = _encoder()
    if encoder is not None:
        # 精确路径失败（比如离线导致词表不可用）也不该让请求挂掉：
        # 估算精度不是关键路径，功能可用性才是。
        try:
            return len(encoder.encode(text))
        except Exception:
            # 与上面同样的理由：估算精度不是关键路径，功能可用性才是
            pass
    return _heuristic_tokens(text)


def count_message_tokens(message: ChatMessage) -> int:
    """单条消息的 token 数（含固定开销）。"""
    parts = [message.content or ""]
    # 工具调用的参数也是要发出去的正文，漏掉会显著低估。
    #
    # 【这里踩过一次】`ToolCall.arguments` 是**已经解析好的 dict**，
    # 不是字符串 —— 直接塞进 `"".join()` 会抛
    # `TypeError: expected str instance, dict found`。
    # 优先用 `raw_arguments`（模型实际发出的那段 JSON，长度最接近真实开销），
    # 没有才退回收序列化。
    for call in message.tool_calls or []:
        parts.append(call.name)
        parts.append(call.raw_arguments or json.dumps(call.arguments, ensure_ascii=False))
    return count_tokens("".join(parts)) + TOKENS_PER_MESSAGE


def count_messages_tokens(messages: Sequence[ChatMessage]) -> int:
    """一批消息的 token 数。"""
    return sum(count_message_tokens(m) for m in messages)


# ============================================================
# 估算精度：测量而不是声称
# ============================================================
def record_prompt_estimate(estimated: int, actual: int) -> float | None:
    """记录一次"估算 vs 实际"，返回偏差倍数（实际/估算）。

    【为什么用两个累加计数器，而不是一个直方图】
    偏差是一个**比值**，而直方图的桶是按毫秒设计的。两个计数器相加之后
    一除就得到整体偏差倍数，没有桶不匹配的问题，也不受标签基数影响
    （标签只有固定一种）。`/api/metrics` 里能直接看到这两个数。

    返回 None 表示这次没法比较（actual 为 0 —— 有些兼容端点不回 usage）。
    """
    from app.core.telemetry import METRICS  # 延迟导入：避免 core → llm 的循环

    METRICS.inc("legacy_prompt_tokens_estimated_total", float(estimated))
    if actual <= 0:
        return None
    METRICS.inc("legacy_prompt_tokens_actual_total", float(actual))
    return actual / estimated if estimated > 0 else None
