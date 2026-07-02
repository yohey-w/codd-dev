"""Import-resolver adapters (Contract Kernel ``import_resolver`` kind).

Every language profile declares ``imports.resolver_adapter`` (e.g. java.yaml's
``java-package``, go.yaml's ``go-module``), but as of this module's introduction
NONE of them were registered on :data:`codd.languages.registry.
default_adapter_registry` — every profile's import-resolver contract resolved as
INCOMPLETE (:meth:`codd.languages.contract.ResolvedLanguageContract.is_complete`
is ``False``). This module registers the FIRST one: Java's, closing that gap for
the java profile specifically (the other languages' resolvers are a separate,
later increment — registering one does not require registering all).

The actual Java import-resolution LOGIC already lives where it is consumed — the
``_java_imported_lookup`` / ``_java_resolve_module`` / ``_java_find_method_def`` /
``_java_fallback_candidates`` plug-ins in :mod:`codd.vb_marker_authenticity`,
wired directly into that module's language-free helper-resolution engine. This
adapter class does not re-wrap them: the Contract Kernel's adapter registry (this
module) and the VB-authenticity gate's plug-in-function engine are deliberately
separate layers today (see :mod:`codd.languages.contract`'s own docstring — the
registry is "not yet wired into the live verify/greenfield gates"). This class
exists purely to make the java profile's DECLARED ``imports.resolver_adapter:
java-package`` contract resolvable rather than perpetually missing, so
``build_language_contract(java_profile).is_complete`` can become ``True`` once
every OTHER adapter kind the java profile declares is also registered.
"""

from __future__ import annotations


class JavaPackageImportResolverAdapter:
    """Fulfils java.yaml's ``imports.resolver_adapter: java-package`` declaration.

    ``id`` matches the profile-declared adapter id exactly, for anyone enumerating
    a registry's contents (:meth:`codd.languages.registry.AdapterRegistry.ids`).
    """

    id = "java-package"
