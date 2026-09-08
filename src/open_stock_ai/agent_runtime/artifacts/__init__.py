"""Logical artifact selection and immutable versioning contracts."""

from .selection_context import ArtifactSelection, SelectionContextManager
from .versioning import (
    ArtifactVersion,
    ArtifactVersionManager,
    InMemoryArtifactVersionStore,
    OptimisticVersionConflict,
    SQLiteArtifactVersionStore,
)

__all__ = [
    "ArtifactSelection",
    "ArtifactVersion",
    "ArtifactVersionManager",
    "InMemoryArtifactVersionStore",
    "OptimisticVersionConflict",
    "SQLiteArtifactVersionStore",
    "SelectionContextManager",
]
