"""Types shared across contract validation and application operations."""

from decimal import Decimal
from typing import Any, Literal

# Documents and query values are checked against the pinned JSON Schema at entry.
type Document = dict[str, Any]
type Options = dict[str, Any]
type ResourceTable = Literal["schemas", "tracks"]
type Job = tuple[str, str, str, Document, int, str]
type EventCursor = tuple[Decimal, bool, Decimal, bool, str]
