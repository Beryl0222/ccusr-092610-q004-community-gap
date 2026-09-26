"""领域错误：所有业务规则违例都以稳定代码抛出。"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """携带稳定代码与中文说明的业务错误。"""

    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details if details is not None else {}
