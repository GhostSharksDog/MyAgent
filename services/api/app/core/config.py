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
