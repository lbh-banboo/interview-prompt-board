"""传给各路由模块 register(app, ctx) 的上下文；路由模块只使用这里的字段。"""
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AppContext:
    store: object  # app.core.Store
    root: Path  # 数据根目录
    token: str
    bus: object  # EventBus
    config: dict = field(default_factory=dict)
