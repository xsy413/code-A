from __future__ import annotations

from .act import _ActNode
from .base import _NodeBase
from .diagnose import _DiagnoseNode
from .lifecycle import _LifecycleNode
from .reflect import _ReflectNode
from .verify import _VerifyNode


class AgentNodes(_LifecycleNode, _ActNode, _VerifyNode, _DiagnoseNode, _ReflectNode, _NodeBase):
    pass


__all__ = ["AgentNodes"]
