from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class AddStockRequest(BaseModel):
    product_id: int
    quantity: int = Field(gt=0, description="Quantity to add. Must be positive.")


class StockResponse(BaseModel):
    warehouse_id: int
    product_id: int
    physical_quantity: int
    available_quantity: int


class ReservationItemRequest(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)


class CreateReservationRequest(BaseModel):
    warehouse_id: int
    items: list[ReservationItemRequest]
    idempotency_key: str = Field(min_length=1, max_length=255)

    @field_validator("items")
    @classmethod
    def items_not_empty(cls, v):
        if not v:
            raise ValueError("items must contain at least one product")
        product_ids = [i.product_id for i in v]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("duplicate product_id in items; combine into one line")
        return v


class ReservationItemResponse(BaseModel):
    product_id: int
    quantity: int


class ReservationResponse(BaseModel):
    id: int
    warehouse_id: int
    status: str
    items: list[ReservationItemResponse]
    created_at: datetime
    expires_at: datetime
    confirmed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None
