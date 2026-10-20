"""Compile a semantic-layer operation to parameterized SQL and run it on the read-only role.

Identifiers (tables, columns, aliases) come only from the frozen YAML and are composed with
`psycopg.sql.Identifier`; operators come from a fixed mapping; every value, including the
runtime-bound case id, is a named bound parameter. Nothing from the LLM is ever spliced into SQL.
"""

import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import tuple_row

from caselens_semantic_layer.layer import get_layer
from caselens_semantic_layer.models import BoundValue, QueryRecordsOutput, RecordSource
from caselens_semantic_layer.params import validate_params

SCHEMA = "public"
CASE_PARAM = "case_id"
# Aliases for the citation columns carried alongside every selected row.
CITE_RECORD = "_cite_record_id"
CITE_EVIDENCE = "_cite_evidence_id"
CITE_ROW = "_cite_row_id"
_CITE_ALIASES = (CITE_RECORD, CITE_EVIDENCE, CITE_ROW)

_COMPARISON_OPS: dict[str, sql.SQL] = {
    "eq": sql.SQL("="),
    "gte": sql.SQL(">="),
    "lte": sql.SQL("<="),
}
_AGGREGATES: dict[str, sql.SQL] = {
    "count": sql.SQL("count(*)"),
    "count_distinct": sql.SQL("count(DISTINCT {})"),
    "sum": sql.SQL("sum({})"),
    "min": sql.SQL("min({})"),
    "max": sql.SQL("max({})"),
}


class RoleError(RuntimeError):
    """The connection is not on the semantic layer's read-only role (a caller bug)."""


@dataclass
class CompiledQuery:
    """A composed statement plus everything needed to shape its result."""

    operation: str
    query: sql.Composed
    params: dict[str, Any]
    columns: list[str]
    citation_table: str
    metric: str | None
    max_rows: int
    statement_timeout_ms: int
    display_params: dict[str, BoundValue]


def escape_like(value: str) -> str:
    """Escape LIKE/ILIKE wildcards so the value matches literally (escape char is backslash)."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _validated_int(value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RuntimeError("semantic layer defaults are out of range")
    return value


class _Binder:
    """Collects bound values under names derived only from YAML param identifiers."""

    def __init__(self, normalized: Mapping[str, Any]) -> None:
        self.normalized = normalized
        self.bound: dict[str, Any] = {}

    def bind(self, name: str, value: Any) -> sql.Placeholder:
        self.bound[name] = value
        return sql.Placeholder(name)

    def param(self, name: str) -> sql.Placeholder:
        return self.bind(name, self.normalized[name])


def _predicate(pred: Mapping[str, Any], binder: _Binder) -> sql.Composable:
    column = sql.Identifier(pred["column"])
    op = pred["op"]
    if op in _COMPARISON_OPS:
        return sql.SQL("{} {} {}").format(column, _COMPARISON_OPS[op], binder.param(pred["param"]))
    if op == "between":
        start, end = pred["params"]
        return sql.SQL("{} BETWEEN {} AND {}").format(
            column, binder.param(start), binder.param(end)
        )
    if op == "ilike":
        name = pred["param"]
        value = binder.normalized[name]
        placeholder = binder.bind(name, f"%{escape_like(str(value))}%")
        return sql.SQL("{} ILIKE {} ESCAPE '\\'").format(column, placeholder)
    if op == "in":
        name = pred["param"]
        values = binder.normalized[name]
        if not isinstance(values, list) or not values:
            raise RuntimeError("an 'in' predicate needs a non-empty list value")
        placeholders = [binder.bind(f"{name}_{i}", v) for i, v in enumerate(values)]
        return sql.SQL("{} IN ({})").format(column, sql.SQL(", ").join(placeholders))
    raise RuntimeError("unsupported predicate op in semantic layer")


def _filter(item: Mapping[str, Any], binder: _Binder) -> sql.Composable:
    if "any_of" in item:
        parts = [_filter(branch, binder) for branch in item["any_of"]]
        return sql.SQL("({})").format(sql.SQL(" OR ").join(parts))
    if "all_of" in item:
        parts = [_filter(pred, binder) for pred in item["all_of"]]
        return sql.SQL("({})").format(sql.SQL(" AND ").join(parts))
    return _predicate(item, binder)


def _where(source: Mapping[str, Any], filters: list[Any], binder: _Binder) -> sql.Composable:
    clauses: list[sql.Composable] = [
        sql.SQL("{} = {}").format(
            sql.Identifier(source["case_column"]), sql.Placeholder(CASE_PARAM)
        )
    ]
    clauses.extend(_filter(item, binder) for item in filters)
    return sql.SQL(" AND ").join(clauses)


def _citation_select(source: Mapping[str, Any]) -> list[sql.Composable]:
    cite = source["citation"]
    keys = ("record_id_column", "evidence_id_column", "row_id_column")
    return [
        sql.SQL("{} AS {}").format(sql.Identifier(cite[key]), sql.Identifier(alias))
        for key, alias in zip(keys, _CITE_ALIASES, strict=True)
    ]


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    return value


def compile_operation(
    operation: str, params: Mapping[str, Any], case_id: uuid.UUID
) -> CompiledQuery:
    """Validate params and compile `operation` into one parameterized statement."""
    layer = get_layer()
    normalized = validate_params(operation, params)
    op = layer["operations"][operation]
    source = layer["sources"][op["source"]]
    defaults = layer["defaults"]
    max_rows = _validated_int(defaults["max_rows"], 1, 500)
    timeout_ms = _validated_int(defaults["statement_timeout_ms"], 100, 5000)
    binder = _Binder(normalized)
    table = sql.Identifier(SCHEMA, source["table"])
    where = _where(source, op["filters"], binder)
    limit = sql.Literal(max_rows + 1)

    if "metric" in op:
        metric_name = op["metric"]
        metric = layer["metrics"][metric_name]
        agg_column = metric.get("column")
        matched_cols = _citation_select(source)
        if agg_column is not None:
            matched_cols.append(sql.Identifier(agg_column))
        aggregate = _AGGREGATES[metric["aggregate"]]
        agg_expr = aggregate.format(sql.Identifier(agg_column)) if agg_column else aggregate
        cites = sql.SQL(", ").join(sql.Identifier("m", a) for a in _CITE_ALIASES)
        query = sql.SQL(
            "WITH matched AS (SELECT {matched_cols} FROM {table} WHERE {where}) "
            "SELECT agg.{metric}, {cites} "
            "FROM (SELECT {agg_expr} AS {metric} FROM matched) AS agg "
            "LEFT JOIN LATERAL (SELECT {plain_cites} FROM matched "
            "ORDER BY {evidence}, {row} LIMIT {limit}) AS m ON true"
        ).format(
            matched_cols=sql.SQL(", ").join(matched_cols),
            table=table,
            where=where,
            metric=sql.Identifier(metric_name),
            cites=cites,
            agg_expr=agg_expr,
            plain_cites=sql.SQL(", ").join(sql.Identifier(a) for a in _CITE_ALIASES),
            evidence=sql.Identifier(CITE_EVIDENCE),
            row=sql.Identifier(CITE_ROW),
            limit=limit,
        )
        columns = [metric_name]
    else:
        columns = list(op["select"])
        select = [sql.Identifier(c) for c in columns] + _citation_select(source)
        order = [
            sql.SQL("{} {}").format(
                sql.Identifier(o["column"]), sql.SQL("DESC" if o["direction"] == "desc" else "ASC")
            )
            for o in op.get("order_by", [])
        ]
        # Primary key last so the order (and therefore truncation) is deterministic.
        order.append(sql.SQL("{} ASC").format(sql.Identifier(source["primary_key"])))
        query = sql.SQL(
            "SELECT {select} FROM {table} WHERE {where} ORDER BY {order} LIMIT {limit}"
        ).format(
            select=sql.SQL(", ").join(select),
            table=table,
            where=where,
            order=sql.SQL(", ").join(order),
            limit=limit,
        )
        metric_name = None

    bound = {CASE_PARAM: case_id, **binder.bound}
    return CompiledQuery(
        operation=operation,
        query=query,
        params=bound,
        columns=columns,
        citation_table=source["citation"]["table"],
        metric=metric_name,
        max_rows=max_rows,
        statement_timeout_ms=timeout_ms,
        display_params={k: _json_value(v) for k, v in bound.items()},
    )


def _source(table: str, record: Any, evidence: Any, row_id: Any) -> RecordSource:
    return RecordSource.model_validate(
        {"table": table, "record_id": record, "evidence_id": evidence, "row_id": row_id}
    )


@contextmanager
def readonly_transaction(conn: psycopg.Connection[Any], timeout_ms: int) -> Iterator[None]:
    """A (sub)transaction that is read-only, time-limited, on the read-only role, and always
    rolled back, so its `SET LOCAL` settings never leak into the caller's transaction."""
    role = get_layer()["defaults"]["role"]
    timeout = _validated_int(timeout_ms, 1, 5000)
    with conn.transaction(force_rollback=True), conn.cursor(row_factory=tuple_row) as cur:
        cur.execute("SET LOCAL transaction_read_only = on")
        cur.execute(sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(timeout)))
        cur.execute("SELECT current_user")
        current = cur.fetchone()
        if current is None or current[0] != role:
            raise RoleError("semantic layer SQL must run on the read-only role")
        yield


def run_operation(
    conn: psycopg.Connection[Any],
    case_id: uuid.UUID,
    operation: str,
    params: Mapping[str, Any],
) -> QueryRecordsOutput:
    """Run one semantic-layer operation for `case_id` on a `caselens_readonly` connection.

    The connection must already be on the read-only role with `app.user_id` set, so RLS applies.
    The statement runs in its own (sub)transaction with `transaction_read_only` and the layer's
    `statement_timeout`; that transaction is always rolled back, so the settings never leak.
    Raises `OperationError` for an unknown operation or invalid params.
    """
    compiled = compile_operation(operation, params, case_id)
    with (
        readonly_transaction(conn, compiled.statement_timeout_ms),
        conn.cursor(row_factory=tuple_row) as cur,
    ):
        cur.execute(compiled.query, compiled.params)
        names = [d.name for d in cur.description or []]
        fetched = [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
    sql_text = compiled.query.as_string(conn)
    table = compiled.citation_table

    if compiled.metric is not None:
        cited = [r for r in fetched if r[CITE_RECORD] is not None]
        truncated = len(cited) > compiled.max_rows
        cited = cited[: compiled.max_rows]
        rows = [{compiled.metric: _json_value(fetched[0][compiled.metric])}] if fetched else []
    else:
        truncated = len(fetched) > compiled.max_rows
        cited = fetched[: compiled.max_rows]
        rows = [{c: _json_value(r[c]) for c in compiled.columns} for r in cited]
    sources = [_source(table, r[CITE_RECORD], r[CITE_EVIDENCE], r[CITE_ROW]) for r in cited]
    return QueryRecordsOutput.model_validate(
        {
            "operation": compiled.operation,
            "sql": sql_text,
            "params": compiled.display_params,
            "columns": compiled.columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "sources": sources,
        }
    )
