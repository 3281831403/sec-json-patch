"""Patch application primitives for the configuration service.

The module implements RFC 6901 pointer resolution and RFC 6902 style
operations.  Two invariants are guaranteed:

* :func:`apply_patch` is atomic: it never mutates its input, and when any
  operation fails the caller's document is left exactly as it was.
* Pointer tokens are decoded, so ``~1`` can only ever address a literal
  ``/`` inside a single object key and never a deeper path.
"""

from __future__ import annotations

import copy
import re
from typing import Any


class PatchError(ValueError):
    """Raised when a patch or a pointer cannot be applied."""


class PatchTestError(PatchError, AssertionError):
    """Raised when a ``test`` operation does not match the document.

    It derives from both :class:`PatchError` and :class:`AssertionError` so
    callers written against either convention keep working.
    """


_INDEX_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_INVALID_ESCAPE_RE = re.compile(r"~(?:[^01]|$)")
_OPERATIONS = {"add", "remove", "replace", "move", "copy", "test"}
_APPEND_TOKEN = "-"


def _decode_token(token: str) -> str:
    """Decode a single RFC 6901 reference token.

    ``~1`` is turned into ``/`` and ``~0`` into ``~`` (in that order so that
    ``~01`` decodes to ``~1``).  Any other ``~`` sequence is invalid and is
    rejected instead of being treated as a literal key, which closes the
    path-traversal hole.
    """
    if not isinstance(token, str):
        raise PatchError("pointer tokens must be strings")
    if _INVALID_ESCAPE_RE.search(token):
        raise PatchError("pointer contains an invalid escape sequence")
    return token.replace("~1", "/").replace("~0", "~")


def parse_pointer(text: str) -> list:
    """Split a JSON pointer into decoded reference tokens."""
    if not isinstance(text, str):
        raise PatchError("pointer must be a string")
    if text == "":
        return []
    if not text.startswith("/"):
        raise PatchError("pointer must start with a slash")
    return [_decode_token(token) for token in text.split("/")[1:]]


def _as_parts(pointer: Any) -> list:
    if isinstance(pointer, list):
        parts = list(pointer)
        if not all(isinstance(part, str) for part in parts):
            raise PatchError("pointer tokens must be strings")
        return parts
    return parse_pointer(pointer)


def _array_index(token: str, length: int, allow_end: bool = False) -> int:
    """Validate an array index token and return it as an int.

    ``allow_end`` permits ``index == length`` which is the insertion point
    just past the last element; the ``-`` append token is handled by the
    callers because it is only meaningful for ``add``.
    """
    if not isinstance(token, str) or _INDEX_RE.fullmatch(token) is None:
        raise PatchError("invalid array index")
    index = int(token)
    limit = length if allow_end else length - 1
    if index < 0 or index > limit:
        raise PatchError("array index out of range")
    return index


def resolve(doc: Any, pointer: Any) -> Any:
    """Return the value addressed by pointer inside doc."""
    current = doc
    for token in _as_parts(pointer):
        if isinstance(current, dict):
            if token not in current:
                raise PatchError("pointer member does not exist")
            current = current[token]
        elif isinstance(current, list):
            current = current[_array_index(token, len(current), False)]
        else:
            raise PatchError("pointer traverses a scalar value")
    return current


def _parent_and_token(doc: Any, parts: list) -> tuple:
    if not parts:
        raise PatchError("the document root has no parent")
    return resolve(doc, parts[:-1]), parts[-1]


def _replace_root(doc: Any, value: Any) -> None:
    if not isinstance(doc, dict) or not isinstance(value, dict):
        raise PatchError("the root document must remain an object")
    doc.clear()
    doc.update(copy.deepcopy(value))


def _add_inplace(doc: Any, parts: list, value: Any) -> None:
    if not parts:
        _replace_root(doc, value)
        return

    parent, token = _parent_and_token(doc, parts)
    if isinstance(parent, dict):
        parent[token] = copy.deepcopy(value)
        return
    if isinstance(parent, list):
        if token == _APPEND_TOKEN:
            # ``-`` appends after the last element and never shifts.
            parent.append(copy.deepcopy(value))
            return
        # A numeric index inserts *before* the addressed element; this is
        # deliberately distinct from the append semantics of ``-``.
        index = _array_index(token, len(parent), allow_end=True)
        parent.insert(index, copy.deepcopy(value))
        return
    raise PatchError("cannot add a member to a scalar value")


def _remove_inplace(doc: Any, parts: list) -> Any:
    if not parts:
        raise PatchError("the document root cannot be removed")

    parent, token = _parent_and_token(doc, parts)
    if isinstance(parent, dict):
        if token not in parent:
            raise PatchError("removed member does not exist")
        return parent.pop(token)
    if isinstance(parent, list):
        index = _array_index(token, len(parent), allow_end=False)
        return parent.pop(index)
    raise PatchError("cannot remove a member from a scalar value")


def _replace_inplace(doc: Any, parts: list, value: Any) -> None:
    if not parts:
        _replace_root(doc, value)
        return

    parent, token = _parent_and_token(doc, parts)
    if isinstance(parent, dict):
        if token not in parent:
            raise PatchError("replaced member does not exist")
        parent[token] = copy.deepcopy(value)
        return
    if isinstance(parent, list):
        if token == _APPEND_TOKEN:
            raise PatchError("invalid array index")
        index = _array_index(token, len(parent), allow_end=False)
        parent[index] = copy.deepcopy(value)
        return
    raise PatchError("cannot replace a member of a scalar value")


def _equal(left: Any, right: Any) -> bool:
    """Depth-aware JSON equality, treating booleans strictly.

    Plain ``==`` would consider ``True`` equal to ``1``; JSON does not.
    Numbers compare by value so ``1`` and ``1.0`` remain equal.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return (
            isinstance(left, bool)
            and isinstance(right, bool)
            and left is right
        )
    if isinstance(left, dict) and isinstance(right, dict):
        if left.keys() != right.keys():
            return False
        return all(_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return False
        return all(_equal(a, b) for a, b in zip(left, right))
    return left == right


def _is_prefix(left: list, right: list) -> bool:
    return len(left) < len(right) and right[: len(left)] == left


def _apply_op_inplace(doc: Any, op: Any) -> None:
    if not isinstance(op, dict):
        raise PatchError("operation must be an object")

    name = op.get("op")
    if name not in _OPERATIONS:
        raise PatchError("unknown operation")
    if "path" not in op:
        raise PatchError("operation path is required")

    path_parts = parse_pointer(op["path"])

    if name == "add":
        if "value" not in op:
            raise PatchError("add requires a value")
        _add_inplace(doc, path_parts, op["value"])
        return

    if name == "remove":
        _remove_inplace(doc, path_parts)
        return

    if name == "replace":
        if "value" not in op:
            raise PatchError("replace requires a value")
        _replace_inplace(doc, path_parts, op["value"])
        return

    if name == "test":
        if "value" not in op:
            raise PatchError("test requires a value")
        # A test operation is strictly read-only: it resolves and compares,
        # it never writes anything into the working document.
        actual = doc if not path_parts else resolve(doc, path_parts)
        if not _equal(actual, op["value"]):
            raise PatchTestError("test operation failed")
        return

    if "from" not in op:
        raise PatchError("move and copy require a from pointer")
    from_parts = parse_pointer(op["from"])

    if name == "copy":
        value = resolve(doc, from_parts)
        _add_inplace(doc, path_parts, value)
        return

    if _is_prefix(from_parts, path_parts):
        raise PatchError("cannot move a value into its own child")
    value = resolve(doc, from_parts)
    _remove_inplace(doc, from_parts)
    _add_inplace(doc, path_parts, value)


def apply_op(doc: Any, op: Any) -> Any:
    """Apply one operation and return the resulting document.

    The input document is never mutated.
    """
    result = copy.deepcopy(doc)
    _apply_op_inplace(result, op)
    return result


def apply_patch(doc: dict, patch: list) -> dict:
    """Apply a complete patch and return the resulting document.

    Application is atomic: all operations run against a private deep copy of
    ``doc``.  If any operation raises, the exception propagates and ``doc``
    is left untouched, so a failed patch is a no-op.
    """
    if not isinstance(patch, list):
        raise PatchError("patch must be a list")

    result = copy.deepcopy(doc)
    for op in patch:
        _apply_op_inplace(result, op)
    return result


def _escape_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _path_from_parts(parts: list) -> str:
    return "".join("/" + _escape_token(part) for part in parts)


def _prefix_ops(ops: list, prefix: str) -> list:
    prefixed = []
    for op in ops:
        item = copy.deepcopy(op)
        item["path"] = prefix + item["path"]
        if "from" in item:
            item["from"] = prefix + item["from"]
        prefixed.append(item)
    return prefixed


def diff_docs(a: Any, b: Any) -> list:
    """Return a patch that turns a into b."""
    if _equal(a, b):
        return []

    if isinstance(a, dict) and isinstance(b, dict):
        ops = []
        for key in a:
            if key not in b:
                ops.append({"op": "remove", "path": _path_from_parts([key])})
        for key in b:
            if key not in a:
                ops.append(
                    {
                        "op": "add",
                        "path": _path_from_parts([key]),
                        "value": copy.deepcopy(b[key]),
                    }
                )
        for key in a:
            if key in b:
                child_ops = diff_docs(a[key], b[key])
                ops.extend(_prefix_ops(child_ops, _path_from_parts([key])))
        return ops

    if isinstance(a, list) and isinstance(b, list):
        ops = []
        common = min(len(a), len(b))
        for index in range(common):
            child_ops = diff_docs(a[index], b[index])
            ops.extend(_prefix_ops(child_ops, _path_from_parts([str(index)])))
        if len(a) > len(b):
            for index in range(len(a) - 1, len(b) - 1, -1):
                ops.append(
                    {"op": "remove", "path": _path_from_parts([str(index)])}
                )
        elif len(b) > len(a):
            for index in range(len(a), len(b)):
                ops.append(
                    {
                        "op": "add",
                        "path": _path_from_parts([str(index)]),
                        "value": copy.deepcopy(b[index]),
                    }
                )
        return ops

    return [{"op": "replace", "path": "", "value": copy.deepcopy(b)}]


def is_noop(patch: list, doc: dict) -> bool:
    """Return whether applying patch leaves doc unchanged."""
    if not isinstance(patch, list):
        return False
    try:
        return _equal(apply_patch(doc, patch), doc)
    except PatchError:
        return False


def validate_patch(patch: Any) -> list[str]:
    """Return the problems found in a patch without applying it."""
    problems = []
    if not isinstance(patch, list):
        return ["patch must be a list"]

    for index, op in enumerate(patch):
        label = "operation " + str(index)
        if not isinstance(op, dict):
            problems.append(label + " must be an object")
            continue

        name = op.get("op")
        if name not in _OPERATIONS:
            problems.append(label + " has an unknown operation")
            continue

        if "path" not in op:
            problems.append(label + " is missing path")
        elif not isinstance(op["path"], str):
            problems.append(label + " path must be a string")
        else:
            try:
                parse_pointer(op["path"])
            except PatchError:
                problems.append(label + " has an invalid path")

        if name in {"move", "copy"}:
            if "from" not in op:
                problems.append(label + " is missing from")
            elif not isinstance(op["from"], str):
                problems.append(label + " from must be a string")
            else:
                try:
                    parse_pointer(op["from"])
                except PatchError:
                    problems.append(label + " has an invalid from")

        if name in {"add", "replace", "test"} and "value" not in op:
            problems.append(label + " is missing value")

    return problems


def patch_batch(docs: list, patches: list) -> dict:
    """Apply one patch per document and return the results.

    Each document is patched independently from its own starting state, and
    because :func:`apply_patch` is atomic a failure on one entry cannot leak
    partial edits into any document.
    """
    if not isinstance(docs, list) or not isinstance(patches, list):
        raise PatchError("documents and patches must be lists")
    if len(docs) != len(patches):
        raise PatchError("documents and patches must have the same length")

    results = [
        apply_patch(doc, patches[index])
        for index, doc in enumerate(docs)
    ]
    return {"results": results}


__all__ = [
    "PatchError",
    "PatchTestError",
    "parse_pointer",
    "resolve",
    "apply_op",
    "apply_patch",
    "diff_docs",
    "is_noop",
    "validate_patch",
    "patch_batch",
]
