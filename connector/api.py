"""serve_as_api: a REST CRUD API over the database (FastAPI, optional extra).

    dbc.serve_as_api(host="0.0.0.0", port=8000, key="secret")

Routes (auth: X-API-Key header when key is set):
    GET    /                     -> {"tables": [...], "views": [...]}
    GET    /{table}              -> rows; query params are filters
    GET    /{table}/{item_id}    -> one row by primary key
    POST   /{table}              -> insert (JSON body), 201
    PATCH  /{table}/{item_id}    -> update by primary key
    DELETE /{table}/{item_id}    -> delete by primary key

List filtering: ?col=value (equal), ?col__more=5, ?col__less=, ?col__like=,
?col__startswith=, ?col__endswith=, ?col__unequal=; special params:
_limit, _page, _order, _desc, _lang.

NOTE: no `from __future__ import annotations` here — FastAPI must see real
(evaluated) annotation objects for Request, which is imported lazily inside
build_app.
"""

from connector.errors import ConnectorError, QueryError

_OPS = {"equal", "unequal", "more", "less", "like", "startswith", "endswith", "contains", "any"}
_RESERVED = {"_limit", "_page", "_order", "_desc", "_lang"}

_INT_TYPES = {"int2", "int4", "int8"}
_FLOAT_TYPES = {"float4", "float8", "numeric"}


def _coerce(meta, column: str, value: str):
    """Query-string values are strings; cast by the column's udt type."""
    udt = meta.types.get(column, "")
    try:
        if udt in _INT_TYPES:
            return int(value)
        if udt in _FLOAT_TYPES:
            return float(value)
        if udt == "bool":
            return value.lower() in ("true", "1", "t", "yes")
    except ValueError:
        pass
    return value


def build_app(connector, key=None):
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.encoders import jsonable_encoder
    except ImportError as e:
        raise ConnectorError(
            "serve_as_api requires FastAPI: pip install pg-connector[api]"
        ) from e

    app = FastAPI(title=f"connector API: {connector.config.database}")

    def check_auth(request: Request) -> None:
        if key is not None and request.headers.get("X-API-Key") != key:
            raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

    def get_query(table: str):
        try:
            return connector.table(table)
        except QueryError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    def pk_of(table: str) -> str:
        meta = connector._meta[table]
        if not meta.pk:
            raise HTTPException(status_code=400, detail=f"Table {table!r} has no primary key")
        return meta.pk[0]

    def coerce_pk(table: str, item_id: str):
        return _coerce(connector._meta[table], pk_of(table), item_id)

    @app.get("/")
    def index(request: Request):
        check_auth(request)
        return {"tables": connector.tables(), "views": connector.views()}

    @app.get("/{table}")
    def list_rows(table: str, request: Request):
        check_auth(request)
        q = get_query(table)
        params = dict(request.query_params)
        lang = params.pop("_lang", None)
        if lang:
            q = q.lang(lang)
        order = params.pop("_order", None)
        desc = params.pop("_desc", "").lower() in ("true", "1")
        limit = params.pop("_limit", None)
        page = params.pop("_page", "1")
        meta = connector._meta[table]
        try:
            for raw_key, value in params.items():
                column, _, op = raw_key.partition("__")
                op = op or "equal"
                if op not in _OPS:
                    raise QueryError(f"Unknown filter operator {op!r}")
                q = getattr(q, op)(**{column: _coerce(meta, column, value)})
            if order:
                q = q.order_by(order, desc=desc)
            if limit:
                try:
                    q = q.per_page(int(limit)).page(int(page))
                except ValueError:
                    raise HTTPException(
                        status_code=400, detail="_limit and _page must be integers"
                    ) from None
            return jsonable_encoder([r.to_dict() for r in q.items])
        except QueryError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/{table}/{item_id}")
    def get_row(table: str, item_id: str, request: Request):
        check_auth(request)
        q = get_query(table)
        try:
            row = q.equal(**{pk_of(table): coerce_pk(table, item_id)}).item
        except QueryError as e:  # malformed PK value (e.g. "abc" for an int key)
            raise HTTPException(status_code=400, detail=str(e)) from e
        if row is None:
            raise HTTPException(status_code=404, detail="Not found")
        return jsonable_encoder(row.to_dict())

    @app.post("/{table}", status_code=201)
    async def create_row(table: str, request: Request):
        check_auth(request)
        q = get_query(table)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object")
        try:
            rows = q.add(**body).exec()
        except QueryError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return jsonable_encoder(rows[0].to_dict())

    @app.patch("/{table}/{item_id}")
    async def update_row(table: str, item_id: str, request: Request):
        check_auth(request)
        q = get_query(table)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object")
        try:
            rows = q.equal(**{pk_of(table): coerce_pk(table, item_id)}).update(**body).exec()
        except QueryError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if not rows:
            raise HTTPException(status_code=404, detail="Not found")
        return jsonable_encoder(rows[0].to_dict())

    @app.delete("/{table}/{item_id}")
    def delete_row(table: str, item_id: str, request: Request):
        check_auth(request)
        q = get_query(table)
        try:
            deleted = q.delete(**{pk_of(table): coerce_pk(table, item_id)}).exec()
        except QueryError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if not deleted:
            raise HTTPException(status_code=404, detail="Not found")
        return {"deleted": deleted}

    return app


def serve(connector, host="127.0.0.1", port=8000, key=None):
    try:
        import uvicorn
    except ImportError as e:
        raise ConnectorError(
            "serve_as_api requires uvicorn: pip install pg-connector[api]"
        ) from e
    uvicorn.run(build_app(connector, key=key), host=host, port=port)
