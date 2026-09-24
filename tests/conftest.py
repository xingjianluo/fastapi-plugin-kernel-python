"""测试公共夹具：自动用 asyncio 事件循环执行 async 测试，无需第三方插件。"""

from __future__ import annotations

import asyncio
import inspect

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    testfunction = pyfuncitem.obj
    if inspect.iscoroutinefunction(testfunction):
        sig_kwargs = {
            k: v for k, v in pyfuncitem.funcargs.items()
            if k in pyfuncitem._fixtureinfo.argnames
        }
        asyncio.run(testfunction(**sig_kwargs))
        return True
    return None
