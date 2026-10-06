"""检索质量回归门禁。

【为什么"有评测脚本"还不够，必须把它变成测试】

`scripts/eval_rag.py` 能算出指标，但**没人会每次改代码都去手动跑一遍**。
于是切分策略调了、embedding 换了、重排器改了 —— 指标可能悄悄掉了 10%，
而你要等到用户抱怨"答非所问"才发现。

把它变成断言，质量就有了**自动化的下限**。这也是"评测"与"回归"的区别：
评测告诉你现在多好，回归保证你不会变得更差。

【为什么断言下界而不是精确值】

当前实现是确定性的（TF-IDF 无随机性），所以精确值本来可以断言。
但那样任何**合理**的改动都会让测试失败：
调整语料、新增一条评测用例、微调切分参数 —— 这些都是正常迭代，
却会把测试变成阻力而不是护栏。

所以取一个**略低于当前基线、但不低到无意义**的下界：
它放过正常波动，拦住真实的劣化。阈值本身也需要随基线上升而调高，
否则"护栏"会越用越松。
"""

from __future__ import annotations

import pytest
from app.rag.chunker import ChunkStrategy
from app.rag.corpus import build_corpus
from app.rag.evaluate import EvalSet, evaluate
from app.rag.retriever import RetrievalMode, Retriever

# 公开评测集与示例简历：**不含任何个人隐私**，任何人 clone 后都能跑
EVAL_SET = "services/api/seed/eval_set.json"

# 下界阈值。
#
# 当前基线（公开集 / 示例简历 / k=5 / min_size=120 / hybrid，无重排）：
#   Recall@5 = 0.869   MRR = 0.657   NDCG@5 = 0.692（ndcg-corpus-v2）
#
# 阈值取基线的约 90%：放过正常波动，拦住真实劣化。
#
# 【阈值要随基线上升而调高】
# 护栏不会自己跟上 —— 基线涨了而阈值不动，等于护栏越来越松，
# 最后变成"只要不彻底崩掉就通过"。每次提升基线后应同步抬高这里的值。
#
# **改动检索链路后如果这里失败，先跑 `python scripts/eval_rag.py --compare --sample`
# 看清是哪一层退化，再决定是修实现还是调阈值 —— 不要直接调阈值。**
MIN_RECALL_AT_5 = 0.74
MIN_MRR = 0.60
MIN_NDCG_AT_5 = 0.64


@pytest.fixture(scope="module")
def public_eval_set() -> EvalSet:
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    return EvalSet.load(root / EVAL_SET)


@pytest.fixture(scope="module")
def public_retriever() -> Retriever:
    """基于**公开示例语料**构建检索器。

    【为什么必须把 include_resume / include_jobs 显式写成 True】
    这两个参数的默认值是 False（通用形态下知识库默认是空的）——
    "系统替用户决定读什么"正是这次改动要修掉的行为。
    但**评测与回归是那个约定的例外**：它需要一份确定的、不含隐私、
    clone 下来就存在的语料，否则指标无从谈起。

    所以这里把数据源声明出来，而不是走 `Retriever.from_default_corpus()` ——
    那条路现在会产出**空语料**：不报错、指标全 0，看起来像"检索算法坏了"，
    实际是"没有数据"。这类失败最贵的地方在于它会把人引向完全错误的排查方向。

    `use_sample_resume=True`：CI 与协作者都必须能跑，
    不能依赖一个被 gitignore 的真实简历文件。
    """
    docs = build_corpus(
        include_resume=True,
        include_jobs=True,
        use_sample_resume=True,
        include_notes=False,
        extra_paths=[],
    )
    return Retriever.from_documents(
        docs,
        strategy=ChunkStrategy.SECTION,
        min_size=120,  # 消融实验确定的最优值
        mode=RetrievalMode.HYBRID,
    )


class TestRetrievalRegression:
    async def test_public_baseline_not_regressed(
        self, public_retriever: Retriever, public_eval_set: EvalSet
    ) -> None:
        report = await evaluate(public_retriever, public_eval_set, k=5)
        m = report.metrics

        assert m["recall"] >= MIN_RECALL_AT_5, (
            f"Recall@5 退化到 {m['recall']:.3f}（下界 {MIN_RECALL_AT_5}）。"
            f"先跑 `python scripts/eval_rag.py --compare --sample` 定位是哪一层出了问题。"
        )
        assert m["mrr"] >= MIN_MRR, (
            f"MRR 退化到 {m['mrr']:.3f}（下界 {MIN_MRR}）—— 排序层出了问题，检查重排器是否生效。"
        )
        assert m["ndcg"] >= MIN_NDCG_AT_5, f"NDCG@5 退化到 {m['ndcg']:.3f}（下界 {MIN_NDCG_AT_5}）"

    async def test_recall_higher_than_mrr_implies_ranking_problem(
        self, public_retriever: Retriever, public_eval_set: EvalSet
    ) -> None:
        """召回率应高于 MRR。

        这是固定旧基准上的排序信号，不是所有语料的数学不变量。
        多相关块查询可能 Recall < MRR；合理改动失败时应检查逐条结果，
        不能仅凭倒挂就判断检索器损坏。
        """
        report = await evaluate(public_retriever, public_eval_set, k=5)
        assert report.metrics["recall"] >= report.metrics["mrr"]

    async def test_all_queries_produce_results(
        self, public_retriever: Retriever, public_eval_set: EvalSet
    ) -> None:
        """每条评测查询都必须能召回至少一条结果。

        某条查询返回空集合通常意味着切分把相关内容整块丢了 ——
        那是"内容在切分阶段被静默丢弃"这类 bug 的信号。
        """
        empty: list[str] = []
        for item in public_eval_set.queries:
            hits = await public_retriever.aretrieve(item.query, k=3)
            if not hits:
                empty.append(item.query)

        assert not empty, f"以下查询召回为空：{empty}"


class TestEvalSetIntegrity:
    """评测集自身的完整性。**评测工具算错了比被测系统错了更可怕** ——
    它会给你虚假的信心。"""

    def test_labels_match_corpus(
        self, public_retriever: Retriever, public_eval_set: EvalSet
    ) -> None:
        """每条标注都必须能匹配到至少一个块。

        标注写错（比如标了一个语料里不存在的章节）会让 recall 分母为 0、
        指标恒为 0，然后你会以为是检索系统坏了，去改本来没问题的代码。
        本项目开发过程中真的踩到过两次。
        """
        from app.rag.evaluate import _is_relevant

        corpus = public_retriever.chunks
        broken = [
            item.query
            for item in public_eval_set.queries
            if not any(_is_relevant(c, item.gold) for c in corpus)
        ]
        assert not broken, f"以下标注匹配不到任何块（recall 分母为 0）：{broken}"

    def test_difficulty_levels_present(self, public_eval_set: EvalSet) -> None:
        """难度分层必须都存在 —— 否则"hard 类全挂"这类结论无从得出。"""
        levels = {item.difficulty for item in public_eval_set.queries}
        assert {"easy", "normal", "hard"} <= levels

    def test_no_pii_in_eval_set(self) -> None:
        """评测集必须可提交：不含手机号、邮箱等个人隐私。

        这条不是形式主义 —— 首版评测集曾被真实简历当锚点，
        既泄漏隐私又让"别人 clone 后评测必然失效"。
        """
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        text = (root / EVAL_SET).read_text(encoding="utf-8")

        # 手机号（中国大陆）
        assert not re.search(r"1[3-9]\d{9}", text), "评测集里出现了手机号"
        # 邮箱
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "评测集里出现了邮箱"
