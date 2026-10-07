from __future__ import annotations

from .act import _ActNode
from .base import _NodeBase
from .execute import _ExecuteNode
from .lifecycle import _LifecycleNode
from .verify import _VerifyNode


class AgentNodes(_LifecycleNode, _ActNode, _ExecuteNode, _VerifyNode, _NodeBase):
    pass


__all__ = ["AgentNodes"]
