"""Filament related endpoints."""

import asyncio
import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pydantic.error_wrappers import ErrorWrapper
from sqlalchemy.ext.asyncio import AsyncSession

from spoolman.api.v1.models import Coil, CoilEvent, Message
from spoolman.database import coil
from spoolman.database.database import get_db_session
from spoolman.database.utils import SortOrder
from spoolman.exceptions import ItemDeleteError

from spoolman.ws import websocket_manager

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/coil",
    tags=["coil"],
)


class CoilUpdateParameters(BaseModel):
    name: Optional[str] = Field(
        max_length=64,
        description="Coil size, to distinguish this coil among others from same vendor.",
        example="Monofilament 3kg(big) coil",
    )
    vendor_id: int = Field(description="The ID of the vendor of this coil type.")
    weight: float = Field(
        gt=0,
        description="The weight of the empty coil in grams.",
        example=300,
    )
    comment: Optional[str] = Field(
        max_length=1024,
        description="Free text comment about this coil.",
        example="",
    )


@router.get(
    "",
    name="Find coils",
    description=(
        "Get a list of coils that matches the search query. "
        "A websocket is served on the same path to listen for updates to any coil, or added or deleted coils. "
        "See the HTTP Response code 299 for the content of the websocket messages."
    ),
    response_model_exclude_none=True,
    responses={
        200: {"model": list[Coil]},
        404: {"model": Message},
        299: {"model": CoilEvent, "description": "Websocket message"},
    },
)
async def find(
    *,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    vendor_name: Optional[str] = Query(
        alias="vendor.name",
        default=None,
        title="Vendor Name",
        description=(
            "Partial case-insensitive search term for the coil vendor name. "
            "Separate multiple terms with a comma. Specify an empty string to match coils with no vendor name."
        ),
    ),
    vendor_id: Optional[str] = Query(
        alias="vendor.id",
        default=None,
        title="Vendor ID",
        description=(
            "Match an exact vendor ID. Separate multiple IDs with a comma. "
            "Specify -1 to match coils with no vendor."
        ),
        examples=["1", "1,2"],
    ),
    name: Optional[str] = Query(
        default=None,
        title="Coil Name",
        description=(
            "Partial case-insensitive search term for the coil name. Separate multiple terms with a comma. "
            "Specify an empty string to match coils with no name."
        ),
    ),
    sort: Optional[str] = Query(
        default=None,
        title="Sort",
        description=(
            'Sort the results by the given field. Should be a comma-separate string with "field:direction" items.'
        ),
        example="vendor.name:asc,spool_weight:desc",
    ),
    limit: Optional[int] = Query(
        default=None,
        title="Limit",
        description="Maximum number of items in the response.",
    ),
    offset: int = Query(
        default=0,
        title="Offset",
        description="Offset in the full result set if a limit is set.",
    ),
) -> JSONResponse:
    sort_by: dict[str, SortOrder] = {}
    if sort is not None:
        for sort_item in sort.split(","):
            field, direction = sort_item.split(":")
            sort_by[field] = SortOrder[direction.upper()]

    vendor_id = vendor_id if vendor_id is not None else vendor_id_old
    if vendor_id is not None:
        try:
            vendor_ids = [int(vendor_id_item) for vendor_id_item in vendor_id.split(",")]
        except ValueError as e:
            raise RequestValidationError([ErrorWrapper(ValueError("Invalid vendor_id"), ("query", "vendor_id"))]) from e
    else:
        vendor_ids = None

    db_items, total_count = await coil.find(
        db=db,
        vendor_name=vendor_name if vendor_name is not None else vendor_name_old,
        vendor_id=vendor_ids,
        name=name,
        sort_by=sort_by,
        limit=limit,
        offset=offset,
    )

    # Set x-total-count header for pagination
    return JSONResponse(
        content=jsonable_encoder(
            (Coil.from_db(db_item) for db_item in db_items),
            exclude_none=True,
        ),
        headers={"x-total-count": str(total_count)},
    )


@router.websocket(
    "",
    name="Listen to coil changes",
)
async def notify_any(
    websocket: WebSocket,
) -> None:
    await websocket.accept()
    websocket_manager.connect(("coil",), websocket)
    try:
        while True:
            await asyncio.sleep(0.5)
            if await websocket.receive_text():
                await websocket.send_json({"status": "healthy"})
    except WebSocketDisconnect:
        websocket_manager.disconnect(("coil",), websocket)


@router.get(
    "/{coil_id}",
    name="Get coil",
    description=(
        "Get a specific coil. A websocket is served on the same path to listen for changes to the coil. "
        "See the HTTP Response code 299 for the content of the websocket messages."
    ),
    response_model_exclude_none=True,
    responses={404: {"model": Message}, 299: {"model": CoilEvent, "description": "Websocket message"}},
)
async def get(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    coil_id: int,
) -> Coil:
    db_item = await coil.get_by_id(db, coil_id)
    return Coil.from_db(db_item)


@router.websocket(
    "/{coil_id}",
    name="Listen to coil changes",
)
async def notify(
    websocket: WebSocket,
    coil_id: int,
) -> None:
    await websocket.accept()
    websocket_manager.connect(("coil", str(coil_id)), websocket)
    try:
        while True:
            await asyncio.sleep(0.5)
            if await websocket.receive_text():
                await websocket.send_json({"status": "healthy"})
    except WebSocketDisconnect:
        websocket_manager.disconnect(("coil", str(coil_id)), websocket)


@router.post(
    "",
    name="Add coil",
    description="Add a new coil to the database.",
    response_model_exclude_none=True,
    response_model=Coil,
    responses={400: {"model": Message}},
)
async def create(  # noqa: ANN201
    db: Annotated[AsyncSession, Depends(get_db_session)],
    body: CoilUpdateParameters,
):
    db_item = await coil.create(
        db=db,
        name=body.name,
        vendor_id=body.vendor_id,
        weight=body.weight,
        comment=body.comment,
    )

    return Coil.from_db(db_item)


@router.patch(
    "/{coil_id}",
    name="Update coil",
    description=(
        "Update any attribute of a coil. Only fields specified in the request will be affected. "
        "If extra is set, all existing extra fields will be removed and replaced with the new ones."
    ),
    response_model_exclude_none=True,
    response_model=Coil,
    responses={
        400: {"model": Message},
        404: {"model": Message},
    },
)
async def update(  # noqa: ANN201
    db: Annotated[AsyncSession, Depends(get_db_session)],
    coil_id: int,
    body: CoilUpdateParameters,
):
    patch_data = body.dict(exclude_unset=True)

    db_item = await coil.update(
        db=db,
        coil_id=coil_id,
        data=patch_data,
    )

    return Coil.from_db(db_item)


@router.delete(
    "/{coil_id}",
    name="Delete coil",
    description="Delete a coil.",
    response_model=Message,
    responses={
        403: {"model": Message},
        404: {"model": Message},
    },
)
async def delete(  # noqa: ANN201
    db: Annotated[AsyncSession, Depends(get_db_session)],
    coil_id: int,
):
    try:
        await coil.delete(db, coil_id)
    except ItemDeleteError:
        logger.exception("Failed to delete coil.")
        return JSONResponse(
            status_code=403,
            content={"message": "Failed to delete coil, see server logs for more information."},
        )
    return Message(message="Success!")
