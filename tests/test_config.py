"""配置合并与校验测试：冲突可解释、拒绝原因可区分。"""
from __future__ import annotations

import logging

import pytest

from app.config.errors import InvalidTypeError, MissingRequiredError
from app.config.loader import ConfigLoader, FieldSpec
from app.config.sources import ConfigSource

log = logging.getLogger(__name__)


def test_priority_merge_with_provenance():
    """高优先级来源胜出，被覆盖的值全部可追溯。"""
    specs = [FieldSpec("port", int, default=8000)]
    sources = [
        ConfigSource("file", 10, {"port": 9000}),
        ConfigSource("env", 20, {"port": 9100}),
        ConfigSource("override", 30, {"port": 9200}),
    ]
    log.info("输入 specs=%s sources=%s", specs, [(s.name, dict(s.values)) for s in sources])
    out = ConfigLoader(specs).load(sources)
    rv = out["port"]
    log.info("判定依据: 优先级 override(30) > env(20) > file(10) > default(0)，胜出值=%s 来源=%s", rv.value, rv.source)
    assert rv.value == 9200
    assert rv.source == "override"
    assert rv.overridden == (("default", 8000), ("file", 9000), ("env", 9100))


def test_missing_required_rejected():
    """必填项缺失必须拒绝，不得静默回退。"""
    log.info("输入: 必填字段 token 无任何来源提供")
    with pytest.raises(MissingRequiredError) as exc:
        ConfigLoader([FieldSpec("token", str, required=True)]).load([])
    log.info("判定依据: 候选为空且 required=True -> MissingRequiredError: %s", exc.value)
    assert exc.value.key == "token"


def test_invalid_type_rejected_not_silent_default():
    """类型不合法必须拒绝，不得静默回退到默认值。"""
    specs = [FieldSpec("port", int, default=8000)]
    sources = [ConfigSource("env", 20, {"port": "not-a-number"})]
    log.info("输入: default=8000, env 提供 port='not-a-number'")
    with pytest.raises(InvalidTypeError) as exc:
        ConfigLoader(specs).load(sources)
    log.info("判定依据: 生效来源 env 的值类型 str != int -> InvalidTypeError: %s", exc.value)
    assert exc.value.source == "env"


def test_bool_not_accepted_as_int():
    """bool 是 int 子类，必须显式区分。"""
    specs = [FieldSpec("port", int, default=8000)]
    sources = [ConfigSource("env", 20, {"port": True})]
    log.info("输入: env 提供 port=True (bool)")
    with pytest.raises(InvalidTypeError):
        ConfigLoader(specs).load(sources)
    log.info("判定依据: bool 不应被当作 int 接受 -> InvalidTypeError")


def test_error_types_are_distinguishable():
    """缺失与类型错误是不同异常类型，调用方可区分处理。"""
    assert issubclass(MissingRequiredError, Exception)
    assert issubclass(InvalidTypeError, Exception)
    assert not issubclass(MissingRequiredError, InvalidTypeError)
    assert not issubclass(InvalidTypeError, MissingRequiredError)
