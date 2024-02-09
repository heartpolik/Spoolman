"""Helper functions to iteract with coil base objects."""

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Optional, Union

import sqlalchemy
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager, joinedload

from spoolman.api.v1.models import Coil, CoilEvent, EventType
from spoolman.database import models, vendor
from spoolman.database.utils import (
    SortOrder,
    add_where_clause_int_in,
    add_where_clause_int_opt,
    add_where_clause_str,
    add_where_clause_str_opt,
    parse_nested_field,
)
from spoolman.exceptions import ItemDeleteError, ItemNotFoundError
from spoolman.ws import websocket_manager


async def create(
    *,
    db: AsyncSession,
    name: Optional[str] = None,
    vendor_id: Optional[int] = None,
    weight: Optional[float] = None,
    comment: Optional[str] = None,
    extra: Optional[dict[str, str]] = None,
) -> models.Coil:
    """Add a new coil to the database."""
    vendor_item: models.Vendor = None
    if vendor_id is not None:
        vendor_item = await vendor.get_by_id(db, vendor_id)

    coil = models.Coil(
        name=name,
        registered=datetime.utcnow().replace(microsecond=0),
        vendor=vendor_item,
        weight=weight,
        comment=comment,
        extra=[models.FilamentField(key=k, value=v) for k, v in (extra or {}).items()],
    )
    db.add(coil)
    await db.commit()
    await coil_changed(coil, EventType.ADDED)
    return coil

async def get_by_id(db: AsyncSession, coil_id: int) -> models.Coil:
    """Get a coil object from the database by the unique ID."""
    coil = await db.get(
        models.Coil,
        coil_id,
        options=[joinedload("*")],  # Load all nested objects as well
    )
    if coil is None:
        raise ItemNotFoundError(f"No coil with ID {coil_id} found.")
    return coil

async def find(
    *,
    db: AsyncSession,
    ids: Optional[list[int]] = None,
    vendor_name: Optional[str] = None,
    vendor_id: Optional[Union[int, Sequence[int]]] = None,
    name: Optional[str] = None,
    sort_by: Optional[dict[str, SortOrder]] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> tuple[list[models.Coil], int]:
    """Find a list of coil objects by search criteria.

    Sort by a field by passing a dict with the field name as key and the sort order as value.
    The field name can contain nested fields, e.g. vendor.name.

    Returns a tuple containing the list of items and the total count of matching items.
    """
    stmt = (
        select(models.Coil)
        .options(contains_eager(models.Coil.vendor))
        .join(models.Coil.vendor, isouter=True)
    )

    stmt = add_where_clause_int_in(stmt, models.Coil.id, ids)
    stmt = add_where_clause_int_opt(stmt, models.Coil.vendor_id, vendor_id)
    stmt = add_where_clause_str(stmt, models.Vendor.name, vendor_name)
    stmt = add_where_clause_str_opt(stmt, models.Coil.name, name)

    total_count = None

    if limit is not None:
        total_count_stmt = stmt.with_only_columns(func.count(), maintain_column_froms=True)
        total_count = (await db.execute(total_count_stmt)).scalar()

        stmt = stmt.offset(offset).limit(limit)

    if sort_by is not None:
        for fieldstr, order in sort_by.items():
            field = parse_nested_field(models.Coil, fieldstr)
            if order == SortOrder.ASC:
                stmt = stmt.order_by(field.asc())
            elif order == SortOrder.DESC:
                stmt = stmt.order_by(field.desc())

    rows = await db.execute(stmt)
    result = list(rows.unique().scalars().all())
    if total_count is None:
        total_count = len(result)

    return result, total_count

async def update(
    *,
    db: AsyncSession,
    coil_id: int,
    data: dict,
) -> models.Coil:
    """Update the fields of a coil object."""
    coil = await get_by_id(db, coil_id)
    for k, v in data.items():
        if k == "vendor_id":
            if v is None:
                coil.vendor = None
            else:
                coil.vendor = await vendor.get_by_id(db, v)
        else:
            setattr(coil, k, v)
    await db.commit()
    await coil_changed(coil, EventType.UPDATED)
    return coil


async def delete(db: AsyncSession, coil_id: int) -> None:
    """Delete a coil object."""
    coil = await get_by_id(db, coil_id)
    await db.delete(coil)
    try:
        await db.commit()  # Flush immediately so any errors are propagated in this request.
        await coil_changed(coil, EventType.DELETED)
    except IntegrityError as exc:
        await db.rollback()
        raise ItemDeleteError("Failed to delete coil.") from exc


async def coil_changed(coil: models.Coil, typ: EventType) -> None:
    """Notify websocket clients that a coil has changed."""
    await websocket_manager.send(
        ("coil", str(coil.id)),
        CoilEvent(
            type=typ,
            resource="coil",
            date=datetime.utcnow(),
            payload=Coil.from_db(coil),
        ),
    )
