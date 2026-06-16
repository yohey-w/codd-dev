"""Entry point for ``python -m codd.contracts_registry`` (delegates to certify).

``python -m codd.contracts_registry``         -> the certification report.
``python -m codd.contracts_registry.certify`` -> the same (certify has its own __main__).
"""

from __future__ import annotations

from codd.contracts_registry.certify import main

if __name__ == "__main__":
    raise SystemExit(main())
