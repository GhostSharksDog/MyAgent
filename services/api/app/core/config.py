"""配置层：全项目唯一的配置入口。

设计要点（面试可讲）：
1. **单一事实来源**：所有可调参数集中在 Settings，禁止在业务代码里散落 os.getenv。
2. **12-Factor**：配置来自环境变量，.env 只服务于本地开发，生产用真实环境变量。
3. **启动即校验**：类型/取值范围在进程启动时校验失败，而不是跑到一半才炸。
4. **可测试**：get_settings() 带缓存，测试里用 get_settings.cache_clear() 重置。
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# services/api/app/core/config.py -> parents[4] == 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[4]


class AppEnv(StrEnum):
    """运行环境。不同环境的安全策略、日志级别、是否回显密钥都不同。"""

    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class LLMSettings(BaseSettings):
    """大模型接入配置。

    刻意只依赖 OpenAI 兼容协议，因此同一份代码可以指向
    DeepSeek / 通义 / 智谱 / 本地 vLLM / Ollama，切换只改环境变量。
    """

    # 【踩坑记录】嵌套的 BaseSettings **不会**继承父级的 env_file。
    # 如果这里不重复声明 env_file，.env 里的 LLM_API_KEY 就读不到，
    # 表现为"明明写了 .env 却报未配置密钥"。这是 pydantic-settings 的经典陷阱。
    model_config = SettingsConfigDict(
        env_prefix="LLM_", env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    api_key: SecretStr = SecretStr("")
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"

    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, gt=0)
    timeout: float = Field(default=120.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        # 拼接路径时 /v1 + /chat/completions 不能出现双斜杠
        return v.rstrip("/")

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key.get_secret_value())


class AgentSettings(BaseSettings):
    """Agent 循环的行为参数——这些直接决定 token 成本与稳定性。"""

    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 单轮用户输入内最多允许几次"模型→工具→观察"循环。
    # 太小会导致复杂任务做不完，太大会让"模型卡死"时烧掉大量 token。
    max_steps: int = Field(default=12, ge=1, le=50)

    # 连续 N 次调用完全相同的工具+参数 => 判定为死循环，主动中止。
    # 这是生产环境必备的护栏：模型偶尔会陷入重复调用同一个工具。
    loop_guard: int = Field(default=3, ge=2, le=10)

    # 同一个模型回合里，最多**同时**执行几个工具调用。
    #
    # 【为什么需要上限，而不是"有几个就并发几个"】
    # 模型一次可能吐出十几个调用，而工具背后可能是网络、文件系统或外部 API。
    # 不限量的话，"并行优化"会变成"对一个外部服务的突发压测" ——
    # 那是把本地的延迟收益，换成别人的限流与自己的超时。
    #
    # 【为什么默认是 4 而不是 1】
    # 1 就是原来的串行行为。默认值取 4 是因为：本项目的工具都是只读的，
    # 并发是纯收益；而 4 这个数量级既能覆盖"模型同时问几件事"的常见形态，
    # 又远低于任何外部服务的限流阈值。
    # 设成 1 可以完全恢复串行（排查顺序敏感问题时有用）。
    #
    # 有副作用的工具由 `Tool.serial` 单独声明，见 tools/base.py：
    # 只要一个回合里出现 serial 工具，整段就退回串行 ——
    # **简单的正确策略胜过需要证明的聪明策略。**
    tool_concurrency: int = Field(default=4, ge=1, le=16)

    # 单轮对话的**总时长预算**（秒）。0 = 不限制（默认）。
    #
    # 【为什么需要它（技术债 T15）】
    # `max_steps` 管住了成本，但**没有管住时间**：最坏情况下
    # `12 步 × (模型 120s + 工具 30s) ≈ 30 分钟`，而这期间连接一直占着、
    # 额度一直挂着。步数上限和时长上限是两个独立的维度，
    # 只设前者时，"一个卡住的请求"仍然是可能的。
    #
    # 【为什么默认是 0（不限制）】
    # 与限流、重排、改写同一条纪律：**没有数据支持的默认值不开**。
    # 合理的总预算取决于部署形态（本地单人用 vs 对外服务），
    # 而猜一个值会把"本来就慢但正常"的请求掐断 —— 那比慢更难排查。
    # 部署方按自己的 P95 设一个明显高于它的值即可。
    #
    # 到达预算时不会硬杀：当前这一步会以 **timeout** 终止，
    # 并如实告诉用户"时间预算用完了、已经做到第几步"，
    # 而不是抛一个看不出原因的异常。
    run_timeout: float = Field(default=0.0, ge=0.0, le=3600.0)

    # ---------- 身份 / 能力集（P6：从专用 Agent 改成通用 Agent） ----------
    # general  —— 通用助手：核心工具（时间、计算、文件、知识库检索）
    # jobhunt  —— 求职专用：额外加载简历 / 岗位 / 匹配相关的提示词与工具
    #
    # 【为什么做成 profile 而不是直接删掉求职功能】
    # 求职那套代码是完整实现并测过的（简历诊断、岗位匹配、多智能体专家），
    # 删掉等于把已经完成的工作扔掉；而且它对特定场景仍然有效。
    # 做成"默认不加载的可选能力集"，既满足"日常是个通用 Agent"，
    # 又保留了随时切回去的能力 —— 代价只是多一个配置项。
    #
    # **默认必须是 general**：一个工具会自动把用户私人文件（简历）
    # 读进知识库、并且开口就自称"求职顾问"的助手，不该是默认形态。
    # 默认值决定了"没读文档的人会得到什么"，那必须是最无害的那个。
    profile: Literal["general", "jobhunt"] = "general"

    # ---------- 文件能力 ----------
    # Agent 可读写的工作区根目录。**所有文件工具都被限制在这个目录内。**
    #
    # 【为什么是"一个根目录"而不是"允许访问整个磁盘"】
    # 给 Agent 文件权限，等于把一个"能读能写"的程序交给它驱动。
    # 一旦它读到 ~/.ssh/id_rsa 或 .env 里的密钥，那些内容就会：
    #   1. 进入提示词（发给模型厂商）
    #   2. 进入会话历史（落盘）
    #   3. 出现在前端（可能被截图/分享）
    # 而且模型可能被文档里的内容诱导去读别的地方（提示词注入）。
    #
    # 限制在一个根目录内、并且**解析符号链接后再校验**，
    # 能把上述风险收敛到"用户主动放进来的东西"。
    # 空字符串 = 文件工具不启用。
    workspace_root: str = ""
    # 单个文件读取上限（字符）。防止一条 read_file 把上下文撑爆 ——
    # 这个上限是**成本性质**的，不是优化。
    file_max_chars: int = Field(default=20000, gt=0)
    # 单次目录列举上限，防止在一个几十万文件的目录上卡死
    file_max_entries: int = Field(default=500, gt=0)

    # ---------- 目录选择器 ----------
    # 界面上"打开文件夹"用哪种交互：
    #   auto   —— 启动时按宿主事实自动判定（默认）
    #   native —— 强制用系统对话框
    #   browse —— 强制用应用内浏览
    #
    # 【为什么默认是 auto，而不是直接把 native 打开】
    # native 只在"操作者就坐在这台机器的屏幕前"时才成立。服务一旦绑到
    # 0.0.0.0、或跑在 SSH/容器里，系统对话框就会开在一台没人看的屏幕上 ——
    # 用户点了按钮什么都不会发生，而且完全没有线索。
    # auto 正是为了不让用户在这种情形下踩坑：判定含糊时一律退回 browse，
    # 因为它到处都能用。两个值只用于"你确定自己在宿主屏幕前"的显式覆盖。
    # 判定规则见 app/core/directory_picker.py（与 DSH 的 directory-picker-auto 一致）。
    directory_picker: Literal["auto", "native", "browse"] = "auto"

    # ---------- 知识库数据源 ----------
    # 逗号分隔的额外文档路径（文件或目录）。**相对仓库根或绝对路径**。
    #
    # 【为什么默认是空的 —— 这是本次改动的起点】
    # 原来知识库是写死的「data/resume.md + seed/jobs.json」，
    # 于是用户一启动就发现"我的简历已经在知识库里了"。
    # 一个通用 Agent 不该默认把用户的私人文件读进索引 ——
    # **数据源应该是用户显式声明的，不是我们猜的。**
    corpus_paths: str = ""
    # 是否把内置的示例语料（seed/）加进知识库。
    # 默认关：示例数据只该用来跑测试与演示，不该混进用户的知识库。
    corpus_include_seed: bool = False

    @property
    def corpus_path_list(self) -> list[str]:
        """把逗号分隔的配置解析成路径列表。"""
        return [p.strip() for p in self.corpus_paths.split(",") if p.strip()]


class TaskSettings(BaseSettings):
    """异步任务队列配置。

    | backend | 用途 | 跨进程 | 投递语义 |
    |---|---|---|---|
    | `memory` | 单进程、本地开发、测试 | ❌ | 恰好一次（同进程内） |
    | `redis`  | 多副本部署 | ✅ | **至多一次**（worker 崩溃会丢任务） |
    | `auto`   | 默认：能连上 Redis 就用，否则降级 | 视环境 | — |

    **投递语义的差别是这里最重要的一点**：内存实现下任务与进程同生共死，
    不存在"投递丢失"；Redis 实现是至多一次，worker 崩溃会让任务永久消失。
    要升级成至少一次需要 Redis Streams + 消费者组，属于 P4 的范围
    （详见 app/tasks/redis_queue.py 的模块文档）。

    `worker_count` 默认 1：处理器会把 CPU 密集的活丢线程池，
    多开 worker 只会让线程数相乘，收益有限而上下文切换成本上升。
    """

    model_config = SettingsConfigDict(
        env_prefix="TASK_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    backend: str = "auto"  # auto | memory | redis
    worker_count: int = Field(default=1, ge=1, le=16)
    max_tasks: int = Field(default=200, ge=1)
    ttl_seconds: int = Field(default=24 * 3600, gt=0)
    # 是否在 API 进程内启动 worker。
    # 拆出独立 worker 进程后设为 false，API 只负责投递任务。
    #
    # 【为什么默认是 true】
    # 单体模式下必须为 true，否则任务永远没人执行（静默失效）。
    # 默认值的选取原则是"让最常见的部署形态零配置正确" ——
    # 这里最常见的是单体，所以 worker 跟着 API 起。
    run_workers_in_api: bool = True


class SessionSettings(BaseSettings):
    """会话存储配置。

    【四个后端的适用场景 —— 选错会造成很难查的问题】

    | backend | 用途 | 跨进程共享 |
    |---|---|---|
    | `memory` | 单元测试、单进程 demo | ❌ |
    | `fake`   | 本地开发：走**真实 Redis 代码路径**但不需要 Docker | ❌ |
    | `redis`  | 生产、多副本部署 | ✅ |
    | `auto`   | 默认：能连上真 Redis 就用，否则降级到内存 | 视环境 |

    `auto` 的降级必须**打醒目日志**：静默降级会让人以为"多进程共享生效了"，
    实际上请求落到别的实例就读不到会话 —— 表现为"用户偶尔丢历史"，
    这种间歇性故障极难定位。
    """

    model_config = SettingsConfigDict(
        env_prefix="SESSION_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    backend: str = "auto"  # auto | memory | fake | redis
    ttl_seconds: int = Field(default=7 * 24 * 3600, gt=0)
    max_sessions: int = Field(default=500, ge=1)


class MemorySettings(BaseSettings):
    """记忆模块配置。

    【默认值的取舍】
    - `max_turns=8` / `keep_recent=6`：超出 8 轮就压缩，但保留最近 6 轮原文。
      摘要是有损的，越近的上下文越需要保真，所以 `keep_recent` 不能太小。
    - `enable_summary=True`：开启后**每超出窗口一次**多一次 LLM 调用
      （不是每轮），换来的是"旧信息不丢失"。关掉则退化为截断 ——
      便宜，但会突然失忆。
    - `max_facts=200`：长期记忆超过后淘汰最早的。生产环境应改为按访问时间
      淘汰，或让模型判断重要性；前者需要记录访问，后者需要额外调用。
    - `enabled=False`：默认关闭。记忆会让每轮多出记忆装配与召回的开销，
      而且**它的价值应该被度量而不是被假设** —— 与检索消融实验同样的方法论。
    """

    model_config = SettingsConfigDict(
        env_prefix="MEMORY_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = False
    max_turns: int = Field(default=8, ge=2, le=100)
    keep_recent: int = Field(default=6, ge=1, le=50)
    enable_summary: bool = True
    max_summary_chars: int = Field(default=1200, gt=0)
    max_facts: int = Field(default=200, ge=1)
    # 长期记忆的落盘位置。相对路径按项目根目录解析。
    facts_path: str = "data/memory/facts.json"

    @property
    def facts_file(self) -> Path:
        p = Path(self.facts_path)
        return p if p.is_absolute() else PROJECT_ROOT / p


class RagSettings(BaseSettings):
    """RAG 检索管线配置。

    默认值是**消融实验测出来的**，不是拍脑袋定的（见
    docs/03-journal/2026-09-13-P2-检索基线评测.md）：

      - `mode=hybrid`：召回受限时混合检索优于纯向量；生产中 recall_k 远小于
        语料规模，所以生产恰好落在混合检索有效的那个区间
      - `reranker=lexical`：唯一稳定带来收益且**零成本**的选项（MRR +0.053）。
        `llm` 重排效果更好（MRR +0.414）但每次查询多一次 LLM 调用 ——
        在 Agent 循环里每次工具调用都会触发一次重排，成本会成倍放大，
        因此设为可选而非默认。这是"效果"与"成本"的显式取舍。
    """

    model_config = SettingsConfigDict(
        env_prefix="RAG_", env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    mode: str = "hybrid"  # dense | sparse | hybrid
    reranker: str = "lexical"  # none | lexical | llm

    # 切分参数（min_chunk_size=120 由消融实验确定）
    strategy: str = "section"
    chunk_size: int = Field(default=500, gt=0)
    chunk_overlap: int = Field(default=80, ge=0)
    min_chunk_size: int = Field(default=120, ge=0)

    # 检索参数
    top_k: int = Field(default=4, ge=1, le=20)
    rrf_k: int = Field(default=60, ge=1)
    max_context_chars: int = Field(default=3000, gt=0)

    # 相关性闸门（余弦相似度下限，对全部检索模式生效）。
    #
    # 【为什么需要它，以及为什么默认关闭】
    # RRF 与 BM25 是基于**排名**的：它们只会说"谁比谁更相关"，从不说
    # "都不相关"。所以混合检索永远会凑满 k 条，哪怕语料里毫无相关内容，
    # 而这些噪声片段会让模型硬编出看似合理的答案。
    # 余弦相似度是绝对量，可以当闸门。
    #
    # 但**合理的阈值高度依赖语料**（词表大小、文档长度都会改变分数尺度）。
    # 设错了会静默丢掉正确结果 —— 比召回噪声更糟。所以默认关闭，
    # 要开启就必须先在本项目的评测集上标定。
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)

    # ---------- Query 改写（P5 补的最后一个 RAG 空白） ----------
    # none | multi_query | hyde
    #
    # 【为什么需要它 —— 这是本项目评测里唯一一条始终失败的查询】
    # 「我适合投递哪些岗位」期望覆盖简历的多个侧面，实际只召回了岗位块。
    # 原因不是算法不够好，而是**一个查询只有一个向量**：
    # 这句话与"Kafka 使用经验""ClickHouse 位图索引"之间既无词汇重叠、
    # 也无足够的语义桥梁，它在向量空间里落在一个很泛的位置，谁也召不回来。
    # 这是「单一查询」的固有局限，**加多少召回路数都救不了**，
    # 必须从查询侧解决。
    #
    # 默认关闭：它与重排一样每次检索多一次 LLM 调用，
    # 收益必须先在评测集上量化 —— 与 RAG_RERANKER 保持同一条纪律：
    # **没有数字支持的默认值不开。**
    query_rewrite: str = "none"
    # multi_query 生成多少条改写（不含原查询）
    rewrite_count: int = Field(default=3, ge=1, le=8)
    # 改写真相对原查询的 RRF 权重。**必须 < 1**：
    # 改写只是"我们猜你可能想问什么"，原查询才是用户真的问了什么。
    rewrite_weight: float = Field(default=0.6, gt=0.0, le=1.0)
    # 改写结果缓存条数。评测会用同一批查询反复跑，
    # 不缓存的话消融阶梯会付出成倍的 LLM 调用。
    rewrite_cache_size: int = Field(default=512, ge=1)


class ResilienceSettings(BaseSettings):
    """韧性配置：熔断与限流。

    【为什么这两个开关默认是"开"和"关"】
    · 熔断默认**开**：它不是优化，是防级联故障的必需件。关掉它不会让
      系统更快，只会让"下游挂了"升级成"整个服务挂了"。
    · 限流默认**关**：它的阈值强依赖业务（单用户该给多少 QPS、
      每天多少 token），设错会直接拒掉正常用户。**没有标定过的阈值
      比没有阈值更危险** —— 所以默认关闭，由部署方按实际容量标定。
    """

    model_config = SettingsConfigDict(
        env_prefix="RESILIENCE_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- 熔断 ----------
    circuit_enabled: bool = True
    # 连续失败多少次打开熔断。
    # 5 次是经验值：既不会被单次抖动误触发，也能在"服务真挂了"时
    # 只牺牲 5 个请求就开始快速失败。
    circuit_failure_threshold: int = Field(default=5, ge=1, le=100)
    # 打开后多久开始探测。要**大于**下游的典型恢复时间（重启、预热），
    # 否则会陷入"探测→失败→再打开"的空转，白白反复打一个还没好的服务。
    circuit_recovery_timeout: float = Field(default=30.0, gt=0)
    # 半开时同时放行的探测请求数。宁可取小（1~2）：
    # 它的作用是"试探"而不是"恢复流量"。
    circuit_half_open_calls: int = Field(default=1, ge=1, le=10)

    # ---------- 限流 ----------
    rate_limit_enabled: bool = False
    # 每个会话每秒允许的请求数（长期平均）
    rate_limit_rps: float = Field(default=0.5, gt=0)
    # 允许的瞬时突发，即桶容量。
    # 必须 >= 1，否则冷启动时第一个请求就会被拒。
    rate_limit_burst: int = Field(default=3, ge=1, le=100)
    # 全局兜底：所有会话合计的上限。防止"开一堆会话"绕过单会话限流。
    # 0 表示不限制。
    rate_limit_global_rps: float = Field(default=0.0, ge=0.0)


class Settings(BaseSettings):
    """全局配置聚合根。"""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_host: str = "127.0.0.1"
    app_port: int = 8000
    app_env: AppEnv = AppEnv.DEV
    log_level: str = "INFO"
    # 日志形态：text（给人看，带颜色）| json（给机器看，每行一个 JSON 对象）。
    #
    # 【为什么默认是 text】
    # 默认值决定"没读文档的人会得到什么"：本地开发时一屏彩色日志最好用，
    # 而 JSON 是**部署到有采集器的地方**才需要的东西。
    # 反过来（默认 JSON）会让每个人第一次跑起来时对着满屏 JSON 皱眉。
    log_format: Literal["text", "json"] = "text"

    # 嵌套配置：pydantic-settings 会分别按各自 prefix 从环境变量读取
    llm: LLMSettings = Field(default_factory=LLMSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    rag: RagSettings = Field(default_factory=RagSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    session: SessionSettings = Field(default_factory=SessionSettings)
    tasks: TaskSettings = Field(default_factory=TaskSettings)
    resilience: ResilienceSettings = Field(default_factory=ResilienceSettings)

    database_url: str = "sqlite+aiosqlite:///./data/legacy.db"
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_fake: bool = True

    # ---------- 微服务拆分 ----------
    # 空字符串 = 在本进程内直接检索（单体模式，默认）。
    # 设成 http://rag:8001 之类 = 改走独立的 RAG 服务。
    #
    # 【为什么用一个"空值即单体"的开关，而不是加一个 MODE 枚举】
    # 开关越少，组合出的状态越少。用 `RAG_SERVICE_URL` 一个变量同时表达
    # "在哪"和"是否远程"，就不可能出现"模式=远程但地址为空"这种
    # 需要额外校验的非法组合。**能用一个变量表达的配置，
    # 就不要用两个变量加一条校验规则。**
    rag_service_url: str = ""
    rag_service_timeout: float = Field(default=15.0, gt=0)

    # ---------- 离线回放（演示兜底） ----------
    # 非空时，/api/chat/stream 不再调用模型，而是重放录制好的事件流。
    # 空 = 正常走真实链路（默认）。
    #
    # 【为什么这值得成为一个配置项，而不是一个临时脚本】
    # 现场演示最怕的不是讲错，是网络不通或额度用完 —— 那会否定整个项目。
    # 而且即使网络正常，真实模型也可能"这次不用工具"或答出不同内容，
    # 而演示要传达的是"**这套系统能做什么**"，这件事应该是确定的。
    #
    # 回放走的是完全相同的 SSE 端点与序列化，只有数据源不同 ——
    # 所以它演示的是真东西，不是前端 mock。
    demo_replay_file: str = ""
    # 回放速度倍率。>1 加快（去掉录制时的网络冷场），
    # 但保持事件之间的**相对**节奏，token 依然是逐个出现的。
    demo_replay_speed: float = Field(default=3.0, gt=0)

    embedding_backend: str = "tfidf"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = 512

    @model_validator(mode="after")
    def _check_prod_safety(self) -> Settings:
        """生产环境的启动自检：宁可启动失败，也不要带着错误配置上线。"""
        if self.app_env is AppEnv.PROD and not self.llm.is_configured:
            raise ValueError("生产环境必须配置 LLM_API_KEY")
        return self

    @property
    def is_dev(self) -> bool:
        return self.app_env is AppEnv.DEV

    @property
    def data_dir(self) -> Path:
        d = PROJECT_ROOT / "data"
        d.mkdir(parents=True, exist_ok=True)
        return d


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试中调用 get_settings.cache_clear() 可强制重载。"""
    return Settings()
