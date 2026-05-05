"""Built-in verification templates and CDP scaffold registries."""

from . import assertion_handlers as assertion_handlers
from . import cdp_browser as cdp_browser
from . import cdp_engines as cdp_engines
from . import cdp_launchers as cdp_launchers
from . import cdp_wire as cdp_wire
from . import curl as curl
from . import form_strategies as form_strategies
from . import playwright as playwright

__all__ = [
    "assertion_handlers",
    "cdp_browser",
    "cdp_engines",
    "cdp_launchers",
    "cdp_wire",
    "curl",
    "form_strategies",
    "playwright",
]
