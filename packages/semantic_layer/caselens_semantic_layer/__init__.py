"""CaseLens semantic layer: YAML definitions and SQL compiler."""

from caselens_semantic_layer.compiler import (
    CompiledQuery,
    RoleError,
    compile_operation,
    readonly_transaction,
    run_operation,
)
from caselens_semantic_layer.layer import LayerError, get_layer
from caselens_semantic_layer.models import OperationError, QueryRecordsOutput, RecordSource
from caselens_semantic_layer.params import validate_params

__version__ = "0.0.1"

__all__ = [
    "CompiledQuery",
    "LayerError",
    "OperationError",
    "QueryRecordsOutput",
    "RecordSource",
    "RoleError",
    "compile_operation",
    "get_layer",
    "readonly_transaction",
    "run_operation",
    "validate_params",
]
