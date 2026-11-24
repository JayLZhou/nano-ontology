"""Read-through to existing tables: govern the data where it already lives.

An object type with a ``source`` keeps its rows in an existing table instead of
nano-onto's own ``onto_objects``; a link type with a ``source`` is a foreign-key
column or a join table. Nothing is copied -- actions read and write those tables
directly, inside the same transaction as the audit entry::

    object_types:
      Supplier:
        source: {table: suppliers, columns: {supplier_id: id, name: supplier_name}}
    link_types:
      supplier:
        from: PurchaseOrder
        to: Supplier
        source: {column: supplier_id}          # FK column on PurchaseOrder's table
      tags:
        from: PurchaseOrder
        to: Tag
        cardinality: many_to_many
        source: {table: po_tags, from_column: po_id, to_column: tag_id}

Table and column names are validated as plain identifiers when the ontology
loads; values are always bound as parameters.
"""

from __future__ import annotations

from typing import Any

from .schema import LinkType, ObjectType, Ontology, coerce
from .store import Store, StoreError


class MappedStore(Store):
    def __init__(self, path: str, onto: Ontology):
        super().__init__(path)
        self.onto = onto
        self._types = {ot.name: ot for ot in onto.object_types.values() if ot.table}
        self._links = {lt.name: lt for lt in onto.link_types.values() if lt.column or lt.table}
        for ot in self._types.values():  # fail fast if a table or column is missing
            self._run(f"SELECT {self._columns(ot)} FROM {ot.table} WHERE 1 = 0")
        for lt in self._links.values():
            if lt.column:
                self._run(f"SELECT {lt.column} FROM {self._types[lt.from_type].table} WHERE 1 = 0")
            else:
                self._run(f"SELECT {lt.from_column}, {lt.to_column} FROM {lt.table} WHERE 1 = 0")

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _columns(ot: ObjectType) -> str:
        return ", ".join(ot.column(p) for p in ot.properties)

    def _select(self, ot: ObjectType, tail: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        """Rows of a mapped table as ontology properties, coerced to their declared types."""
        with self.lock:
            rows = self._run(f"SELECT {self._columns(ot)} FROM {ot.table} {tail}", params).fetchall()
        out = []
        for row in rows:
            props = {}
            for prop, value in zip(ot.properties.values(), row):
                try:
                    props[prop.name] = None if value is None else coerce(prop.type, value)
                except (TypeError, ValueError):
                    raise StoreError(f"{ot.table}.{ot.column(prop.name)} holds {value!r}, which is not a "
                                     f"{prop.type} as {ot.name}.{prop.name} declares") from None
            out.append(props)
        return out

    def _key(self, type_name: str, pk: Any) -> Any:
        """A primary key in its column's type (keys travel as strings inside the engine)."""
        ot = self.onto.object_types[type_name]
        return coerce(ot.properties[ot.primary_key].type, pk)

    def _pk_filter(self, ot: ObjectType) -> str:
        return f"{ot.column(ot.primary_key)} = ?"

    # ---------------------------------------------------------------- objects
    def get(self, type_name: str, pk: str) -> dict[str, Any] | None:
        ot = self._types.get(type_name)
        if ot is None:
            return super().get(type_name, pk)
        try:
            key = self._key(type_name, pk)
        except (TypeError, ValueError):
            return None
        rows = self._select(ot, f"WHERE {self._pk_filter(ot)}", (key,))
        return rows[0] if rows else None

    def put(self, type_name: str, pk: str, props: dict[str, Any]) -> None:
        ot = self._types.get(type_name)
        if ot is None:
            return super().put(type_name, pk, props)
        if self.exists(type_name, pk):
            self._write(ot, pk, props)
        else:
            names = [p for p in ot.properties if p in props]
            self._run(
                f"INSERT INTO {ot.table} ({', '.join(ot.column(p) for p in names)}) "
                f"VALUES ({', '.join('?' for _ in names)})",
                [props[p] for p in names],
            )

    def update(self, type_name: str, pk: str, changes: dict[str, Any]) -> dict[str, Any]:
        ot = self._types.get(type_name)
        if ot is None:
            return super().update(type_name, pk, changes)
        if not self.exists(type_name, pk):
            raise KeyError(f"{type_name} {pk!r} not found")
        self._write(ot, pk, changes)  # only the changed columns, so unmapped precision is never rewritten
        return self.get(type_name, pk)  # type: ignore[return-value]

    def _write(self, ot: ObjectType, pk: str, changes: dict[str, Any]) -> None:
        names = [p for p in changes if p in ot.properties and p != ot.primary_key]
        if names:
            self._run(
                f"UPDATE {ot.table} SET {', '.join(ot.column(p) + ' = ?' for p in names)} WHERE {self._pk_filter(ot)}",
                [changes[p] for p in names] + [self._key(ot.name, pk)],
            )

    def delete(self, type_name: str, pk: str) -> None:
        ot = self._types.get(type_name)
        if ot is None:
            return super().delete(type_name, pk)
        self._run(f"DELETE FROM {ot.table} WHERE {self._pk_filter(ot)}", (self._key(type_name, pk),))

    def query(self, type_name: str, where: dict[str, Any] | None = None, limit: int | None = 100) -> list[dict[str, Any]]:
        ot = self._types.get(type_name)
        if ot is None:
            return super().query(type_name, where, limit)
        sql, params, conditions = "", [], []
        for key, value in (where or {}).items():
            if key not in ot.properties:
                return []
            conditions.append(f"{ot.column(key)} IS NULL" if value is None else f"{ot.column(key)} = ?")
            params += [] if value is None else [value]
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += f" ORDER BY {ot.column(ot.primary_key)}"
        if limit is not None:
            sql, params = sql + " LIMIT ?", params + [limit]
        return self._select(ot, sql, params)

    def count(self, type_name: str) -> int:
        ot = self._types.get(type_name)
        if ot is None:
            return super().count(type_name)
        return self._rows(f"SELECT COUNT(*) AS n FROM {ot.table}")[0]["n"]

    # ------------------------------------------------------------------ links
    def _fk(self, lt: LinkType) -> tuple[str, str, str]:
        """``(table, from_column, to_column)`` for a mapped link, whichever way it is stored."""
        if lt.column:
            ot = self._types[lt.from_type]
            return ot.table, ot.column(ot.primary_key), lt.column  # type: ignore[return-value]
        return lt.table, lt.from_column, lt.to_column  # type: ignore[return-value]

    def links_from(self, link: str, from_pk: str) -> list[str]:
        lt = self._links.get(link)
        if lt is None:
            return super().links_from(link, from_pk)
        table, src, dst = self._fk(lt)
        rows = self._rows(f"SELECT {dst} AS v FROM {table} WHERE {src} = ? AND {dst} IS NOT NULL ORDER BY {dst}",
                          (self._key(lt.from_type, from_pk),))
        return [str(r["v"]) for r in rows]

    def links_to(self, link: str, to_pk: str) -> list[str]:
        lt = self._links.get(link)
        if lt is None:
            return super().links_to(link, to_pk)
        table, src, dst = self._fk(lt)
        rows = self._rows(f"SELECT {src} AS v FROM {table} WHERE {dst} = ? ORDER BY {src}",
                          (self._key(lt.to_type, to_pk),))
        return [str(r["v"]) for r in rows]

    def add_link(self, link: str, from_pk: str, to_pk: str) -> None:
        lt = self._links.get(link)
        if lt is None:
            return super().add_link(link, from_pk, to_pk)
        table, src, dst = self._fk(lt)
        a, b = self._key(lt.from_type, from_pk), self._key(lt.to_type, to_pk)
        if lt.column:
            self._run(f"UPDATE {table} SET {dst} = ? WHERE {src} = ?", (b, a))
        elif not self._rows(f"SELECT 1 AS x FROM {table} WHERE {src} = ? AND {dst} = ?", (a, b)):
            self._run(f"INSERT INTO {table} ({src}, {dst}) VALUES (?, ?)", (a, b))

    def remove_link(self, link: str, from_pk: str | None = None, to_pk: str | None = None) -> None:
        lt = self._links.get(link)
        if lt is None:
            return super().remove_link(link, from_pk, to_pk)
        table, src, dst = self._fk(lt)
        conditions, params = [], []
        if from_pk is not None:
            conditions.append(f"{src} = ?")
            params.append(self._key(lt.from_type, from_pk))
        if to_pk is not None:
            conditions.append(f"{dst} = ?")
            params.append(self._key(lt.to_type, to_pk))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        if lt.column:  # a foreign key is cleared, the row stays
            self._run(f"UPDATE {table} SET {dst} = NULL{where}", params)
        else:
            self._run(f"DELETE FROM {table}{where}", params)
