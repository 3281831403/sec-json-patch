"""Public package interface for the configuration patch library."""

from .apply import (
    PatchError,
    apply_op,
    apply_patch,
    diff_docs,
    is_noop,
    parse_pointer,
    patch_batch,
    resolve,
    validate_patch,
)

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