"""配置来源抽象：默认值、文件、环境变量、外部覆盖。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

# 固定优先级约定：值越大优先级越高
PRIORITY_DEFAULT = 0    # 代码内缺省值
PRIORITY_FILE = 10      # 配置文件
PRIORITY_ENV = 20       # 环境变量
PRIORITY_OVERRIDE = 30  # 外部显式覆盖（如启动参数、测试注入）


@dataclass(frozen=True)
class ConfigSource:
    """一个具名配置来源，priority 越大优先级越高。"""

    name: str
    priority: int
    values: Mapping[str, Any]


def env_source(prefix: str = "APP_", priority: int = PRIORITY_ENV) -> ConfigSource:
    """从环境变量构造配置来源。

    取所有以 prefix 开头的环境变量，键名去掉前缀并转小写，
    值尝试按 JSON 解析（还原数字/布尔），失败则保留原始字符串。
    """
    values: dict[str, Any] = {}
    for key, raw in os.environ.items():
        if not key.startswith(prefix):
            continue
        name = key[len(prefix):].lower()
        try:
            values[name] = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            values[name] = raw
    return ConfigSource(name=f"env({prefix}*)", priority=priority, values=values)


def file_source(path: str | Path, priority: int = PRIORITY_FILE) -> ConfigSource:
    """从 JSON 配置文件构造配置来源；文件不存在时视为空来源。"""
    p = Path(path)
    values: dict[str, Any] = {}
    if p.exists():
        values = json.loads(p.read_text(encoding="utf-8"))
    return ConfigSource(name=f"file({p})", priority=priority, values=values)
