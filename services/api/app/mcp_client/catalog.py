"""显式服务清单；配置原子保存，接口永不返回鉴权值。"""

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

MASK = "********"


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: uuid4().hex[:12], pattern=r"^[a-zA-Z0-9_-]{1,32}$")
    name: str = Field(min_length=1, max_length=80)
    transport: Literal["stdio", "http"] = "http"
    enabled: bool = False
    url: str = Field(default="", max_length=2048)
    command: str = Field(default="", max_length=1024)
    args: list[str] = Field(default_factory=list, max_length=100)
    cwd: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    proxy: str = ""
    selected_tools: list[str] = Field(default_factory=list, max_length=32)
    trusted_tools: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_target(self):
        if self.transport == "http":
            parsed = urlsplit(self.url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
            ):
                raise ValueError("填写完整 HTTP(S) MCP 地址；认证请放在请求头中")
            if any(
                word in parsed.query.lower() for word in ("key=", "token=", "secret=", "password=")
            ):
                raise ValueError("密钥不能写入 URL，请使用鉴权请求头")
        elif not self.command.strip() or not self.cwd or not Path(self.cwd).is_absolute():
            raise ValueError("本地 MCP 需要启动程序和明确的绝对工作目录")
        if (
            self.proxy
            and self.proxy != MASK
            and urlsplit(self.proxy).scheme not in {"http", "https"}
        ):
            raise ValueError("代理必须是 HTTP(S) 地址")
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) for k in self.env):
            raise ValueError("环境变量名称无效")
        if len(json.dumps(self.model_dump())) > 65536:
            raise ValueError("服务配置超过 64 KiB，请缩减参数或环境变量")
        return self

    def public(self):
        view = self.model_dump()
        view["env"] = {k: MASK for k in self.env}
        view["headers"] = {k: MASK for k in self.headers}
        # 代理可能含认证信息，也作为密钥处理。
        view["proxy"] = MASK if self.proxy else ""
        return view

    def connection_fingerprint(self):
        value = self.model_dump(exclude={"name", "enabled", "selected_tools", "trusted_tools"})
        return digest(value)


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class Catalog:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[ServerConfig]:
        if not self.path.exists():
            return []
        if self.path.stat().st_size > 1024 * 1024:
            raise ValueError("MCP 清单超过 1 MiB，请检查配置文件")
        values = json.loads(self.path.read_text(encoding="utf-8"))
        result = [ServerConfig.model_validate(v) for v in values]
        if len(result) > 16 or len({s.id for s in result}) != len(result):
            raise ValueError("最多配置16个服务，服务 ID 不得重复")
        return result

    def save(self, values):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps([v.model_dump() for v in values], ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, self.path)


def merge_secrets(new: ServerConfig, old: ServerConfig | None):
    for field in ("headers", "env"):
        values = getattr(new, field)
        previous = getattr(old, field, {})
        for key, value in list(values.items()):
            if value == MASK:
                if key not in previous:
                    raise ValueError("不能用掩码创建密钥，请填写实际值")
                values[key] = previous[key]
    if new.proxy == MASK:
        new.proxy = old.proxy if old else ""
    return new
