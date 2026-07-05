"""Schema diff and migration generation.

Diff compares two *physical* schemas (multilanguage already expanded) and can
render: a human-readable report, forward SQL (up) and reverse SQL (down).
PostgreSQL cannot drop enum values — such changes are reported as manual.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from psycopg import sql

from connector.schema import (
    ColumnDef,
    SchemaDef,
    TableDef,
    _check_default,
    _type_sql,
    column_ddl,
    comment_ddl,
    create_enum_ddl,
    create_table_ddl,
    fk_order,
    needs_vector_extension,
)


@dataclass
class ColumnChange:
    table: str
    name: str
    current: ColumnDef
    target: ColumnDef
    changes: list[str]  # subset of: type, not_null, default, unique, comment, manual:*


@dataclass
class SchemaDiff:
    added_tables: list[TableDef] = field(default_factory=list)
    removed_tables: list[TableDef] = field(default_factory=list)
    added_columns: list[tuple[str, ColumnDef]] = field(default_factory=list)
    removed_columns: list[tuple[str, ColumnDef]] = field(default_factory=list)
    changed_columns: list[ColumnChange] = field(default_factory=list)
    added_enums: dict[str, list[str]] = field(default_factory=dict)
    removed_enums: list[str] = field(default_factory=list)
    added_enum_values: list[tuple[str, str]] = field(default_factory=list)
    removed_enum_values: list[tuple[str, str]] = field(default_factory=list)
    target_enums: dict[str, list[str]] = field(default_factory=dict)

    def is_empty(self, additive_only: bool = False) -> bool:
        """additive_only=True ignores removals — useful when the target schema
        intentionally describes only a subset of the database."""
        additions = (
            self.added_tables or self.added_columns or self.changed_columns
            or self.added_enums or self.added_enum_values
        )
        if additive_only:
            return not additions
        return not (
            additions or self.removed_tables or self.removed_columns
            or self.removed_enums or self.removed_enum_values
        )

    # -- reporting -----------------------------------------------------------

    def __str__(self) -> str:
        if self.is_empty():
            return "Schemas are identical."
        lines = []
        for name, values in self.added_enums.items():
            lines.append(f"+ enum {name} = {', '.join(values)}")
        for enum, value in self.added_enum_values:
            lines.append(f"+ enum value {enum}: {value}")
        for enum, value in self.removed_enum_values:
            lines.append(f"! enum value {enum}: {value} — PostgreSQL cannot drop enum values (manual)")
        for name in self.removed_enums:
            lines.append(f"- enum {name}")
        for t in self.added_tables:
            lines.append(f"+ table {t.name} ({len(t.columns)} columns)")
        for t in self.removed_tables:
            lines.append(f"- table {t.name}")
        for table, col in self.added_columns:
            lines.append(f"+ column {table}.{col.name} {col.type}")
        for table, col in self.removed_columns:
            lines.append(f"- column {table}.{col.name}")
        for ch in self.changed_columns:
            lines.append(f"~ column {ch.table}.{ch.name}: {', '.join(ch.changes)}")
        return "\n".join(lines)

    # -- SQL generation --------------------------------------------------------

    def up_sql(self, use_id_as_uuid: bool = False) -> str:
        stmts: list[str] = []
        target = SchemaDef(enums=self.target_enums, tables=self.added_tables)
        if needs_vector_extension(target):
            stmts.append("CREATE EXTENSION IF NOT EXISTS vector")
        for name, values in self.added_enums.items():
            stmts.append(create_enum_ddl(name, values).as_string(None))
        for enum, value in self.added_enum_values:
            stmts.append(
                sql.SQL("ALTER TYPE {} ADD VALUE IF NOT EXISTS {}").format(
                    sql.Identifier(enum), sql.Literal(value)
                ).as_string(None)
            )
        for enum, value in self.removed_enum_values:
            stmts.append(
                f"-- MANUAL: enum {enum} value {value!r} removed in target; "
                "PostgreSQL cannot drop enum values (recreate the type by hand)"
            )
        for t in fk_order(self.added_tables):
            stmts.append(create_table_ddl(t, self.target_enums, use_id_as_uuid).as_string(None))
            for c in t.columns:
                if c.comment:
                    stmts.append(comment_ddl(t.name, c.name, c.comment).as_string(None))
        for table, col in self.added_columns:
            stmts.append(
                sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS ").format(
                    sql.Identifier(table)
                ).as_string(None)
                + column_ddl(col, self.target_enums).as_string(None)
            )
            if col.comment:
                stmts.append(comment_ddl(table, col.name, col.comment).as_string(None))
        for ch in self.changed_columns:
            stmts.extend(_alter_column(ch.table, ch.name, ch.current, ch.target,
                                       ch.changes, self.target_enums))
        for table, col in self.removed_columns:
            stmts.append(
                sql.SQL("ALTER TABLE {} DROP COLUMN IF EXISTS {}").format(
                    sql.Identifier(table), sql.Identifier(col.name)
                ).as_string(None)
            )
        for t in self.removed_tables:
            stmts.append(
                sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(
                    sql.Identifier(t.name)
                ).as_string(None)
            )
        for name in self.removed_enums:
            stmts.append(
                sql.SQL("DROP TYPE IF EXISTS {}").format(sql.Identifier(name)).as_string(None)
            )
        return _script(stmts)

    def down_sql(self, use_id_as_uuid: bool = False) -> str:
        stmts: list[str] = []
        current_enums = dict(self.target_enums)
        current_enums.update({name: [] for name in self.removed_enums})
        for t in self.added_tables:
            stmts.append(
                sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(
                    sql.Identifier(t.name)
                ).as_string(None)
            )
        for table, col in self.added_columns:
            stmts.append(
                sql.SQL("ALTER TABLE {} DROP COLUMN IF EXISTS {}").format(
                    sql.Identifier(table), sql.Identifier(col.name)
                ).as_string(None)
            )
        for ch in self.changed_columns:
            stmts.extend(_alter_column(ch.table, ch.name, ch.target, ch.current,
                                       ch.changes, current_enums))
        for table, col in self.removed_columns:
            restored = col
            if col.not_null and col.default is None:
                # re-adding NOT NULL without a default would fail on any
                # non-empty table — restore the column nullable instead
                restored = replace(col, not_null=False)
                stmts.append(
                    f"-- NOTE: {table}.{col.name} was NOT NULL without a default; "
                    "restored as nullable (backfill, then SET NOT NULL manually)"
                )
            stmts.append(
                sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS ").format(
                    sql.Identifier(table)
                ).as_string(None)
                + column_ddl(restored, current_enums).as_string(None)
            )
            stmts.append(f"-- NOTE: data of dropped column {table}.{col.name} is not restored")
        for t in self.removed_tables:
            stmts.append(create_table_ddl(t, current_enums).as_string(None))
            stmts.append(f"-- NOTE: data of dropped table {t.name} is not restored")
        for enum, value in self.added_enum_values:
            stmts.append(
                f"-- MANUAL: enum value {enum}.{value} was added; "
                "PostgreSQL cannot drop enum values"
            )
        for name in self.added_enums:
            stmts.append(
                sql.SQL("DROP TYPE IF EXISTS {}").format(sql.Identifier(name)).as_string(None)
            )
        return _script(stmts)


def _alter_column(
    table: str, name: str, old: ColumnDef, new: ColumnDef,
    changes: list[str], enums: dict[str, list[str]],
) -> list[str]:
    stmts: list[str] = []
    t, c = sql.Identifier(table), sql.Identifier(name)
    if "type" in changes:
        stmts.append(
            sql.SQL("ALTER TABLE {} ALTER COLUMN {} TYPE ").format(t, c).as_string(None)
            + _type_sql(new.type, enums).as_string(None)
            + sql.SQL(" USING {}::").format(c).as_string(None)
            + _type_sql(new.type, enums).as_string(None)
        )
    if "default" in changes:
        if new.default is None:
            stmts.append(
                sql.SQL("ALTER TABLE {} ALTER COLUMN {} DROP DEFAULT").format(t, c).as_string(None)
            )
        else:
            stmts.append(
                sql.SQL("ALTER TABLE {} ALTER COLUMN {} SET DEFAULT ").format(t, c).as_string(None)
                + _check_default(new.default)
            )
    if "not_null" in changes:
        action = sql.SQL("SET NOT NULL") if new.not_null else sql.SQL("DROP NOT NULL")
        stmts.append(
            sql.SQL("ALTER TABLE {} ALTER COLUMN {} {}").format(t, c, action).as_string(None)
        )
    if "unique" in changes:
        constraint = sql.Identifier(f"{table}_{name}_key")
        if new.unique:
            stmts.append(
                sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} UNIQUE ({})").format(
                    t, constraint, c
                ).as_string(None)
            )
        else:
            stmts.append(
                sql.SQL("ALTER TABLE {} DROP CONSTRAINT IF EXISTS {}").format(
                    t, constraint
                ).as_string(None)
            )
    if "comment" in changes:
        stmts.append(comment_ddl(table, name, new.comment or "").as_string(None))
    for change in changes:
        if change.startswith("manual:"):
            stmts.append(f"-- MANUAL: column {table}.{name}: {change[7:]} (not automated)")
    return stmts


def _script(stmts: list[str]) -> str:
    out = []
    for s in stmts:
        out.append(s if s.startswith("--") else s + ";")
    return "\n".join(out) + ("\n" if out else "")


def _normalize_default(expr: str | None) -> str | None:
    if expr is None:
        return None
    return expr.strip().lower()


def diff_schemas(current: SchemaDef, target: SchemaDef) -> SchemaDiff:
    """What has to happen to `current` to become `target` (physical schemas)."""
    d = SchemaDiff(target_enums=dict(target.enums))

    current_tables = {t.name: t for t in current.tables}
    target_tables = {t.name: t for t in target.tables}

    for name, values in target.enums.items():
        if name not in current.enums:
            d.added_enums[name] = list(values)
        else:
            for v in values:
                if v not in current.enums[name]:
                    d.added_enum_values.append((name, v))
            for v in current.enums[name]:
                if v not in values:
                    d.removed_enum_values.append((name, v))
    for name in current.enums:
        if name not in target.enums:
            d.removed_enums.append(name)

    for name, table in target_tables.items():
        if name not in current_tables:
            d.added_tables.append(table)
    for name, table in current_tables.items():
        if name not in target_tables:
            d.removed_tables.append(table)

    for name in set(current_tables) & set(target_tables):
        cur_cols = {c.name: c for c in current_tables[name].columns}
        tgt_cols = {c.name: c for c in target_tables[name].columns}
        for cname, col in tgt_cols.items():
            if cname not in cur_cols:
                d.added_columns.append((name, col))
        for cname, col in cur_cols.items():
            if cname not in tgt_cols:
                d.removed_columns.append((name, col))
        for cname in set(cur_cols) & set(tgt_cols):
            old, new = cur_cols[cname], tgt_cols[cname]
            changes = []
            if old.type != new.type:
                # serial vs integer is the same physical column
                pair = {old.type, new.type}
                if pair not in ({"serial", "integer"}, {"bigserial", "bigint"}):
                    changes.append("type")
            if _normalize_default(old.default) != _normalize_default(new.default):
                changes.append("default")
            if old.not_null != new.not_null:
                changes.append("not_null")
            if old.unique != new.unique:
                changes.append("unique")
            if (old.comment or None) != (new.comment or None):
                changes.append("comment")
            if old.primary != new.primary:
                changes.append("manual:primary key change")
            if (old.references or None) != (new.references or None):
                changes.append("manual:foreign key change")
            if changes:
                d.changed_columns.append(ColumnChange(name, cname, old, new, changes))
    return d


def write_migration(diff: SchemaDiff, out_dir: str | Path = "migrations",
                    use_id_as_uuid: bool = False) -> Path:
    """Write <timestamp>.sql and <timestamp>.down.sql; returns the up-file path."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    up_path = out / f"{stamp}.sql"
    up_path.write_text(diff.up_sql(use_id_as_uuid), encoding="utf-8")
    (out / f"{stamp}.down.sql").write_text(diff.down_sql(use_id_as_uuid), encoding="utf-8")
    return up_path
