"""Public API for the connector framework.

Concrete connector modules live next to this file (one per source) and
register themselves via the ``@register`` decorator. The
``_REGISTERED_MODULES`` tuple at the bottom of this file imports each
concrete connector so the registry is fully populated on package
import; that is what makes ``get_connector("goszakup")`` work without
the caller having to remember to import the module first.
"""

from __future__ import annotations

from tender_monitor.connectors import ets_tender as _ets_tender
from tender_monitor.connectors import goszakup as _goszakup
from tender_monitor.connectors import mitwork as _mitwork
from tender_monitor.connectors import national_bank as _national_bank
from tender_monitor.connectors import samruk_kazyna as _samruk_kazyna
from tender_monitor.connectors import tendersinfo as _tendersinfo
from tender_monitor.connectors import uzex_etender as _uzex_etender
from tender_monitor.connectors import xt_xarid as _xt_xarid
from tender_monitor.connectors import zakup_unified as _zakup_unified
from tender_monitor.connectors.base import Connector, FetchResult
from tender_monitor.connectors.errors import (
    AuthError,
    ConnectorError,
    FetchError,
    ParseError,
    RateLimitError,
)
from tender_monitor.connectors.registry import (
    all_connectors,
    clear_registry,
    get_connector,
    register,
)

# Tuple references the imported modules so static analyzers know they're
# used and we don't get a "unused import" warning. Importing each
# module triggers the @register decorator at its module top.
_REGISTERED_MODULES = (
    _ets_tender,
    _goszakup,
    _mitwork,
    _national_bank,
    _samruk_kazyna,
    _tendersinfo,
    _uzex_etender,
    _xt_xarid,
    _zakup_unified,
)


__all__ = [
    "AuthError",
    "Connector",
    "ConnectorError",
    "FetchError",
    "FetchResult",
    "ParseError",
    "RateLimitError",
    "all_connectors",
    "clear_registry",
    "get_connector",
    "register",
]
