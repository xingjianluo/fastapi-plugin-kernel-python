# fastapi-plugin-kernel-python

在 FastAPI 后台服务模板上实现的**分层配置加载**与**可插拔插件装配**内核，
目标是让配置冲突、插件依赖异常与并发访问下的行为都可预期、可解释。

## 目录结构

```
app/
├── main 入口        : main.py / app/api.py
├── 配置子系统       : app/config/   (schema / loader / errors)
├── 插件子系统       : app/plugins/  (contract / registry / manager / errors)
└── 示例插件         : app/example_plugins.py
tests/               : test_config.py / test_plugins.py / test_api.py
docs/                : configuration.md / plugins.md
```

## 能力概览

### 1. 分层配置（`app.config`）

- 多来源按**固定优先级（高 → 低）**合并：`external > env > builtin 缺省`；
- 每个键的胜负关系完整保留，`explain(key)` 可解释冲突裁决；
- 严格类型校验（str/int/float/bool，不做隐式转换，bool 非 int）：
  - 必填缺失 → `MissingRequiredError`；
  - 类型非法（即使是高优先级来源）→ `InvalidTypeError`，**绝不静默回退**；
  - 拒绝原因可按异常类型区分，且带键名、来源、期望类型等上下文。

### 2. 可插拔插件（`app.plugins`）

- 声明式依赖 + Kahn 拓扑排序确定装配顺序（依赖先初始化，逆序释放）；
- 重复注册 / 缺失依赖 / 循环依赖三类拒绝原因**可区分**（见下表）；
- 单插件失败隔离：失败插件 `FAILED`、下游 `SKIPPED`、其余正常，服务可用；
  失败记录含错误类型、信息与**原始异常**，可经管理器或 HTTP 端点查询；
- 并发 `assemble()` 共享同一任务、只生效一次；结果快照不可变；
- `teardown()` 逆序、幂等、并发安全；释放失败记录后继续，不泄漏资源；
- 失败后重试整体重建，无部分生效；同名不允许重复/静默替换注册。

| 拒绝场景 | 异常 |
| --- | --- |
| 注册时发现重名 | `DuplicatePluginError` |
| 依赖未注册 | `MissingDependencyError`（含 `plugin`、`missing`） |
| 依赖图成环 | `CircularDependencyError`（含具体环路径） |
| 单插件初始化/工厂失败 | 隔离为 `FailureRecord`（非致命，可查） |

### 3. HTTP 端点

| 端点 | 用途 |
| --- | --- |
| `GET /healthz` | 存活检查 |
| `GET /readyz` | 装配状态与失败列表（含原始错误类型与信息） |
| `GET /plugins/{name}` | 单插件健康状态与失败详情 |
| `GET /config/{key}` | 配置值、生效来源与冲突解释 |

更多见 [`docs/configuration.md`](docs/configuration.md)、[`docs/plugins.md`](docs/plugins.md)。

## 本地验证

```bash
# 环境（Python 3.14，依赖由 uv 管理）
uv sync

# 全量单测（测试内会打印输入、装配顺序、判定依据日志）
uv run python -m pytest -s -o log_cli=true

# 仅关注某类场景
uv run python -m pytest tests/test_config.py -v     # 配置冲突 / 必填 / 类型
uv run python -m pytest tests/test_plugins.py -v    # 循环依赖 / 重复注册 / 失败隔离 / 并发
uv run python -m pytest tests/test_api.py -v        # HTTP 状态查询 / lifespan 装配

# 启动服务（默认 app.name=kernel，无外部插件）
uv run uvicorn main:app --reload

# 冒烟查询
curl -s localhost:8000/readyz | python -m json.tool
curl -s localhost:8000/config/app.name | python -m json.tool
```

## 在自己的服务中接入

```python
from app.api import create_app
from app.example_plugins import SPECS

app = create_app(
    specs=SPECS,
    overrides={"app.name": "billing", "http.port": 9000},
)
```
