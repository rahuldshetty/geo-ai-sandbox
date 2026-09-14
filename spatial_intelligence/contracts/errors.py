"""Error taxonomy shared by every layer of the package."""

from __future__ import annotations


class SpatialIntelligenceError(Exception):
    """Base class for this package's own failures."""


class WorkspaceError(SpatialIntelligenceError, ValueError):
    """A path violated workspace confinement, or a file contract was broken.

    Subclasses ``ValueError`` because every existing caller already translates
    ``ValueError`` into a tool failure or a 400 response.
    """


class ToolInputError(SpatialIntelligenceError, ValueError):
    """A tool rejected its arguments (bad URL scheme, unknown option, ...).

    Distinct from ``WorkspaceError`` only for readability at the call site; both
    are model-visible, recoverable failures.
    """


class RuntimeNotBoundError(SpatialIntelligenceError, RuntimeError):
    """A tool ran without a bound :class:`~spatial_intelligence.tools.runtime.ToolRuntime`."""
