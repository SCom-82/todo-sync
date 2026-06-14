"""Graph id validation helpers.

Two validators with different semantics:

- is_task_graph_id: strict validator for Task ids. Graph Task ids are long opaque
  base64 strings (100+ chars). A UUID-shaped id (8-4-4-4-12 hex) is NOT a real
  Graph Task id — it means the push silently failed. Used by task_service only.

- is_present_id: lenient validator for child-resource ids (linkedResources,
  attachments). According to MS Graph documentation the linkedResource id is a
  GUID (8-4-4-4-12 format) by design. Rejecting UUID-shaped ids here would
  incorrectly mark correctly-synced resources as failed. All we require is a
  non-empty string.
"""
import re as _re

_UUID_RE = _re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    _re.IGNORECASE,
)


def is_task_graph_id(resp: dict) -> "str | None":
    """Return Graph id from response if it looks like a valid Graph Task id.

    Graph Task ids are base64-encoded opaque strings, typically 100+ chars.
    UUID-shaped ids (36 chars, 8-4-4-4-12) indicate push failure — return None.

    This is a port of the legacy _validate_graph_id from task_service.py.
    Import from here; the copy in task_service delegates here.
    """
    id_val = resp.get("id")
    if not id_val or not isinstance(id_val, str):
        return None
    if _UUID_RE.fullmatch(id_val):
        return None  # local UUID, not a real Graph Task id
    return id_val


def is_present_id(resp: dict) -> "str | None":
    """Return id from response if it is a non-empty string, else None.

    Used for linkedResources and attachments. Does NOT reject UUID-shaped ids —
    Graph returns GUID-format ids for linkedResources by design (ADR 0001 §2).
    """
    id_val = resp.get("id")
    if id_val and isinstance(id_val, str):
        return id_val
    return None
