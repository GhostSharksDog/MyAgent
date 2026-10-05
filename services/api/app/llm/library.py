"""模型库：用户保存的多个模型配置（`data/models.json`）。

《为什么"模型清单"不能塞进 .env》

`.env` 是**运行时配置**，只表达"现在用哪一个" —— 那个唯一性正是它的价值。
而模型库是**候选清单**：用户可能同时存着 DeepSeek、公司网关、本地 Ollama，
随时切。

把清单塞进 `.env` 会需要 `LLM_MODEL_1` / `LLM_MODEL_2` 这类编号键，而编号配置
有两个解决不了的毛病：
  · 表达不了"哪个是当前"（得再加一个 `LLM_ACTIVE=2`，于是三处要同步）；
  · 删除中间某个会让编号出现空洞，"3 号还在吗"成了每次读配置都要想一遍的问题。

所以：**清单放文件，选中的那份写进 `.env`** —— 与设置界面写 `.env` 是同一套
机制（`app/api/settings.py` 的 `_write_env`），保证"界面改的"和"手改 .env 的"
仍然是同一个事实来源。

《密钥放在哪》

存在这个文件里（`data/` 已被 gitignore，与简历同级）。对外接口**只返回掩码**，
与 `/api/settings` 的处理保持一致 —— 界面需要知道"配没配"，不需要知道原文。

《写文件为什么要"临时文件 + 替换"》

直接 `write_text` 覆盖时，如果进程在写到一半时被杀（或磁盘满），
留下的是一个**被截断的 JSON** —— 下次读取直接解析失败，而用户会失去整份清单。
先写临时文件再 `os.replace` 是原子的（同分区内的 rename）：
要么是旧的完整内容，要么是新的完整内容，不存在中间态。
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.core.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

# 与 data/ 下其它运行时文件同级；整个 data/ 已被 gitignore
DEFAULT_PATH = PROJECT_ROOT / "data" / "models.json"


@dataclass
class SavedModel:
    """一条保存下来的模型配置。"""

    label: str
    base_url: str
    model: str
    api_key: str = ""
    # 预设来源（deepseek / openai / ollama / custom …）。只用于界面归类与显示，
    # 不参与任何逻辑判断 —— 判断"是不是同一个模型"靠的是地址+模型名+密钥。
    provider: str = "custom"
    temperature: float | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_json(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: dict[str, object]) -> SavedModel:
        """从磁盘/请求体构造。

        【为什么要对每个字段做类型收敛】
        这个文件是**用户可以手改的**（就在 data/ 下）。如果直接
        `cls(**raw)`，一个手滑写成数字的 label 会让整份清单读不出来 ——
        而用户完全不知道自己的编辑把整个模型管理界面弄崩了。
        这里逐字段收敛，坏字段退回默认值，剩下能读的照常显示。
        """
        return cls(
            label=str(raw.get("label") or "未命名模型"),
            base_url=str(raw.get("base_url") or ""),
            model=str(raw.get("model") or ""),
            api_key=str(raw.get("api_key") or ""),
            provider=str(raw.get("provider") or "custom"),
            temperature=(
                float(raw["temperature"])  # type: ignore[arg-type]
                if isinstance(raw.get("temperature"), (int, float))
                else None
            ),
            id=str(raw.get("id") or uuid.uuid4().hex[:12]),
        )

    def same_target_as(self, other: SavedModel) -> bool:
        """是否指向同一个"目标"（地址 + 模型名 + 密钥）。

        用来判断"当前正在用的是不是这一条"。**不能只比 label** ——
        label 是用户起的名字，改个名字不该让它变成另一个模型；
        也不能只比 base_url —— 同一个网关下不同模型名是不同的东西。
        """
        return (
            self.base_url.rstrip("/") == other.base_url.rstrip("/")
            and self.model == other.model
            and self.api_key == other.api_key
        )


class ModelLibrary:
    """`data/models.json` 的读写。

    刻意做成"读-改-写"的即时落盘，而不是进程内缓存：
    这个文件很小、改动很少（用户手动操作级别），而**缓存会带来
    "多个 worker 进程各自持有不同清单"** 的问题 —— 那正是本项目
    一直避免的那类静默不一致。
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_PATH

    def load(self) -> list[SavedModel]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # 文件坏了要**说出来**并当作空清单继续，而不是抛给调用方：
            # 一个读不出来的清单不该让整个设置界面打不开。
            logger.warning("模型清单无法解析（%s）：%s —— 当作空清单继续", self.path, exc)
            return []

        items = raw if isinstance(raw, list) else raw.get("models", [])
        if not isinstance(items, list):
            return []

        out: list[SavedModel] = []
        for item in items:
            if isinstance(item, dict):
                out.append(SavedModel.from_json(item))
        return out

    def save(self, models: list[SavedModel]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([m.to_json() for m in models], ensure_ascii=False, indent=2)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(tmp, self.path)  # 原子替换：不会留下被截断的 JSON

    def upsert(self, model: SavedModel) -> list[SavedModel]:
        """新增或按 id 覆盖一条，返回新清单。"""
        models = self.load()
        for index, existing in enumerate(models):
            if existing.id == model.id:
                models[index] = model
                break
        else:
            models.append(model)
        self.save(models)
        return models

    def delete(self, model_id: str) -> list[SavedModel]:
        models = [m for m in self.load() if m.id != model_id]
        self.save(models)
        return models
