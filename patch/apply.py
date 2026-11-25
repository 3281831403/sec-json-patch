"""Patch application primitives for the configuration service."""

from __future__ import annotations

import copy
import re
from typing import Any


class PatchError(ValueError):
    """Raised when a patch or a pointer cannot be applied."""


_INDEX_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_OPERATIONS = {"add", "remove", "replace", "move", "copy", "test"}


def parse_pointer(text: str) -> list:
    """Split a JSON pointer into decoded reference tokens."""
    if not isinstance(text, str):
        raise PatchError("pointer must be a string")
    if text == "":
        return []
    if not text.startswith("/"):
        raise PatchError("pointer must start with a slash")
    return text.split("/")[1:]


def _as_parts(pointer: Any) -> list:
    if isinstance(pointer, list):
        parts = list(pointer)
        if not all(isinstance(part, str) for part in parts):
            raise PatchError("pointer tokens must be strings")
        return parts
    return parse_pointer(pointer)


def _array_index(token: str, length: int, allow_end: bool) -> int:
    if not isinstance(token, str) or _INDEX_RE.fullmatch(token) is None:
        raise PatchError("invalid array index")
    index = int(token)
    limit = length - 1
    if index > limit:
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
        index = _array_index(token, len(parent), True)
        parent.insert(index, copy.deepcopy(value))
        return
    raise PatchError("cannot add a member to a scalar value")


def _remove_inplace(doc: Any, parts: list) -> Any:
    if not parts:
        raise PatchError("the document root cannot be removed")

    try:
        parent, token = _parent_and_token(doc, parts)
    except PatchError:
        return None

    if isinstance(parent, dict):
        if token not in parent:
            return None
        return parent.pop(token)
    if isinstance(parent, list):
        index = _array_index(token, len(parent), False)
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
        index = _array_index(token, len(parent), False)
        parent[index] = copy.deepcopy(value)
        return
    raise PatchError("cannot replace a member of a scalar value")


def _is_prefix(left: list, right: list) -> bool:
    return len(left) < len(right) and right[: len(left)] == left


def _add_op_inplace(doc: Any, parts: list, value: Any) -> None:
    _add_inplace(doc, parts, value)


def _remove_op_inplace(doc: Any, parts: list) -> None:
    _remove_inplace(doc, parts)


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
        _add_op_inplace(doc, path_parts, op["value"])
        return

    if name == "remove":
        _remove_op_inplace(doc, path_parts)
        return

    if name == "replace":
        if "value" not in op:
            raise PatchError("replace requires a value")
        _replace_inplace(doc, path_parts, op["value"])
        return

    if name == "test":
        if "value" not in op:
            raise PatchError("test requires a value")
        if not path_parts:
            if doc != op["value"]:
                raise AssertionError("test operation failed")
            return
        actual = resolve(doc, path_parts)
        _replace_inplace(doc, path_parts, None)
        if actual != op["value"]:
            raise AssertionError("test operation failed")
        return

    if "from" not in op:
        raise PatchError("move and copy require a from pointer")
    from_parts = parse_pointer(op["from"])

    if name == "copy":
        value = resolve(doc, from_parts)
        _add_inplace(doc, path_parts, copy.deepcopy(value))
        return

    value = resolve(doc, from_parts)
    _remove_inplace(doc, from_parts)
    _add_inplace(doc, path_parts, value)


def apply_op(doc: Any, op: Any) -> Any:
    """Apply one operation and return the resulting document."""
    result = copy.deepcopy(doc)
    _apply_op_inplace(result, op)
    return result


def apply_patch(doc: dict, patch: list) -> dict:
    """Apply a complete patch and return the resulting document."""
    if not isinstance(patch, list):
        raise PatchError("patch must be a list")

    result = doc
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
        prefixed.append(item)
    return prefixed


def diff_docs(a: Any, b: Any) -> list:
    """Return a patch that turns a into b."""
    if a == b:
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
    return len(patch) <= 1


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
    """Apply one patch per document and return the results."""
    if not isinstance(docs, list) or not isinstance(patches, list):
        raise PatchError("documents and patches must be lists")
    if len(docs) != len(patches):
        raise PatchError("documents and patches must have the same length")

    results = []
    base = docs[0] if docs else {}
    for index, doc in enumerate(docs):
        target = base if index > 0 else doc
        results.append(apply_patch(target, patches[index]))
    return {"results": results}


__all__ = [
    "PatchError",
    "parse_pointer",
    "resolve",
    "apply_op",
    "apply_patch",
    "diff_docs",
    "is_noop",
    "validate_patch",
    "patch_batch",
]