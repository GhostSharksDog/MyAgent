"""工具层异常。

为什么单独定义一套异常而不是直接用 ValueError？
—— 因为工具执行失败有明确的语义分类，上层需要据此决定
   "回灌给模型让它重试" 还是 "直接告诉用户这条路不通"。
"""

from __future__ import annotations


class ToolError(Exception):
    """工具业务性失败（如"找不到该城市的岗位"）。会被转成观察结果回灌给模型。"""


class ToolValidationError(ToolError):
    """参数不合法。回灌给模型，通常模型能自行修正参数。"""


class ToolTimeout(ToolError):
    """执行超时。"""


class ToolPermissionError(ToolError):
    """越权访问（如路径穿越）。属于安全边界，绝不回灌原始路径细节。"""
