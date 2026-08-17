"""SAP-Anbindung (ausschliesslich ueber SAP GUI Scripting)."""

from .connection import (
    SapBusinessError,
    SapElementNotFoundError,
    SapError,
    SapGuiConnection,
    SapNotAvailableError,
    SapPopupError,
    SapScriptingDisabledError,
    SapWriteBlockedError,
    SessionInfo,
)
from .gateway import ConnectionStatus, SapGateway
from .interfaces import MaterialInfo, VendorMatch, WriteContext
from .message_guard import MessageSuppressionError
from .selectors import SelectorRegistry, SelectorNotVerifiedError

__all__ = [
    "ConnectionStatus", "MaterialInfo", "MessageSuppressionError", "SapBusinessError",
    "SapElementNotFoundError", "SapError", "SapGateway", "SapGuiConnection",
    "SapNotAvailableError", "SapPopupError", "SapScriptingDisabledError",
    "SapWriteBlockedError", "SelectorNotVerifiedError", "SelectorRegistry",
    "SessionInfo", "VendorMatch", "WriteContext",
]
