from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from tadoss.adapters import postgres as store
from tadoss.api.dependencies import Body, Database, Query
from tadoss.api.http import (
    collection_page,
    property_body,
    property_value,
    read_response,
)
from tadoss.application import records

router = APIRouter()


@router.head("/schemas")
@router.get("/schemas")
def list_schemas(request: Request, db: Database, options: Query) -> Response:
    return collection_page(request, db, "schemas", options)


@router.head("/schemas/{schema_id}")
@router.get("/schemas/{schema_id}")
def get_schema(
    request: Request, schema_id: str, db: Database, options: Query
) -> Response:
    document = store.get(db, "schemas", schema_id)
    return read_response(request, document)


@router.put("/schemas/{schema_id}")
def put_schema(request: Request, schema_id: str, db: Database, body: Body) -> Response:
    created = records.replace(
        db, "schemas", schema_id, body, request.state.identity.subject
    )
    if created:
        return JSONResponse(
            body, status_code=201, headers={"Location": str(request.url.path)}
        )
    return Response(status_code=204)


@router.delete("/schemas/{schema_id}")
def delete_schema(request: Request, schema_id: str, db: Database) -> Response:
    records.delete_schema(db, schema_id)
    return Response(status_code=204)


@router.head("/schemas/{schema_id}/schema")
@router.get("/schemas/{schema_id}/schema")
@router.head("/schemas/{schema_id}/tags")
@router.get("/schemas/{schema_id}/tags")
@router.head("/schemas/{schema_id}/tags/{name}")
@router.get("/schemas/{schema_id}/tags/{name}")
@router.head("/schemas/{schema_id}/description")
@router.get("/schemas/{schema_id}/description")
@router.head("/schemas/{schema_id}/label")
@router.get("/schemas/{schema_id}/label")
@router.head("/schemas/{schema_id}/external_id")
@router.get("/schemas/{schema_id}/external_id")
def get_property(request: Request, schema_id: str, db: Database) -> Response:
    return read_response(
        request, property_value(request, store.get(db, "schemas", schema_id))
    )


@router.put("/schemas/{schema_id}/tags/{name}")
@router.put("/schemas/{schema_id}/description")
@router.put("/schemas/{schema_id}/label")
@router.put("/schemas/{schema_id}/external_id")
def put_property(
    request: Request, schema_id: str, db: Database, body: Body
) -> Response:
    field = request.scope["route"].path.split("/")[3]
    property_body(request, body)
    records.change_property(
        db,
        "schemas",
        schema_id,
        field,
        body,
        request.state.identity.subject,
        tag=request.path_params.get("name"),
        remove=False,
    )
    return Response(status_code=204)


@router.delete("/schemas/{schema_id}/tags/{name}")
@router.delete("/schemas/{schema_id}/description")
@router.delete("/schemas/{schema_id}/label")
@router.delete("/schemas/{schema_id}/external_id")
def delete_property(request: Request, schema_id: str, db: Database) -> Response:
    field = request.scope["route"].path.split("/")[3]
    records.change_property(
        db,
        "schemas",
        schema_id,
        field,
        None,
        request.state.identity.subject,
        tag=request.path_params.get("name"),
        remove=True,
    )
    return Response(status_code=204)
