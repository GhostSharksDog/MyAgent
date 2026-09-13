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

    # 嵌套配置：pydantic-settings 会分别按各自 prefix 从环境变量读取
    llm: LLMSettings = Field(default_factory=LLMSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    rag: RagSettings = Field(default_factory=RagSettings)

    database_url: str = "sqlite+aiosqlite:///./data/jobpilot.db"
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_fake: bool = True

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
