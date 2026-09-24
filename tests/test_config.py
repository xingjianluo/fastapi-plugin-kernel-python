"""分层配置加载测试：冲突合并、缺省、必填、类型拒绝均可解释。"""

from __future__ import annotations

import logging

import pytest

from app.config import (
    ConfigField,
    ConfigSchema,
    InvalidTypeError,
    LayeredConfigLoader,
    MissingRequiredError,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("test.config")

SCHEMA = ConfigSchema(
    fields=(
        ConfigField("app.name", "str", required=True),
        ConfigField("http.port", "int", default=8080),
        ConfigField("http.debug", "bool", default=False),
        ConfigField("http.timeout", "float", default=1.5),
    )
)


def test_default_when_all_sources_absent() -> None:
    sources = [("env", {"app.name": "kernel"})]
    log.info("输入 sources=%s，判定依据：仅必填项给出，其余取声明缺省值", sources)
    cfg = LayeredConfigLoader(SCHEMA).load(sources)
    assert cfg.get("app.name") == "kernel"
    assert cfg.get("http.port") == 8080
    assert cfg.get("http.debug") is False
    assert cfg.get("http.timeout") == 1.5
    assert "声明缺省值" in cfg.explain("http.port")


def test_fixed_priority_override_and_explain() -> None:
    # 优先级：cli > env > file（file 最低）
    sources = [
        ("cli", {"app.name": "from-cli", "http.port": 9090}),
        ("env", {"app.name": "from-env", "http.port": 8000}),
        ("file", {"app.name": "from-file", "http.port": 7000}),
    ]
    log.info("输入 sources=%s，判定依据：固定优先级高 -> 低，首位胜出", sources)
    cfg = LayeredConfigLoader(SCHEMA).load(sources)
    assert cfg.get("app.name") == "from-cli"
    assert cfg.get("http.port") == 9090
    assert cfg.sources["http.port"] == "cli"
    assert cfg.shadowed["http.port"] == ("env", "file")
    text = cfg.explain("http.port")
    log.info("装配/合并顺序=%s，explain=%s", cfg.priority, text)
    assert "生效来源='cli'" in text
    assert "优先级 1/3" in text
    assert "'env'" in text and "'file'" in text


def test_lower_priority_used_when_higher_missing() -> None:
    sources = [
        ("cli", {"app.name": "x"}),
        ("env", {"http.port": 6000}),
        ("file", {"http.port": 7000}),
    ]
    cfg = LayeredConfigLoader(SCHEMA).load(sources)
    log.info("输入=%s，判定依据：高来源缺键，落到下一来源 env", sources)
    assert cfg.get("http.port") == 6000
    assert cfg.sources["http.port"] == "env"
    assert cfg.shadowed["http.port"] == ("file",)


def test_missing_required_is_rejected_not_default() -> None:
    sources = [("env", {"http.port": 80})]
    log.info("输入=%s，判定依据：必填 app.name 全来源缺失 -> MissingRequiredError", sources)
    with pytest.raises(MissingRequiredError) as ei:
        LayeredConfigLoader(SCHEMA).load(sources)
    assert ei.value.key == "app.name"
    assert ei.value.sources == ["env"]


def test_invalid_type_is_rejected_not_silent_fallback() -> None:
    # 高优先级 cli 给出非法字符串；不得回退到 env 的合法值或缺省值。
    sources = [
        ("cli", {"app.name": "k", "http.port": "9090"}),
        ("env", {"http.port": 7000}),
    ]
    log.info("输入=%s，判定依据：生效来源值类型非法 -> InvalidTypeError，不回退", sources)
    with pytest.raises(InvalidTypeError) as ei:
        LayeredConfigLoader(SCHEMA).load(sources)
    assert ei.value.key == "http.port"
    assert ei.value.source == "cli"
    assert ei.value.expected == "int"


def test_bool_not_accepted_as_int_and_vice_versa() -> None:
    with pytest.raises(InvalidTypeError):
        LayeredConfigLoader(SCHEMA).load([("env", {"app.name": "k", "http.port": True})])
    with pytest.raises(InvalidTypeError):
        LayeredConfigLoader(SCHEMA).load([("env", {"app.name": "k", "http.debug": 1})])


def test_duplicate_source_names_rejected() -> None:
    with pytest.raises(ValueError):
        LayeredConfigLoader(SCHEMA).load(
            [("env", {"app.name": "a"}), ("env", {"app.name": "b"})]
        )


def test_error_types_are_distinguishable() -> None:
    from app.config import ConfigError

    for exc_type in (MissingRequiredError, InvalidTypeError):
        assert issubclass(exc_type, ConfigError)
