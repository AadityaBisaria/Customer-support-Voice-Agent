"""Canonical Pydantic models for the bot's data contract."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, NewType

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ------------------------------------------------------------ Strong IDs
CustomerId = NewType("CustomerId", int)
OrderId = NewType("OrderId", str)
OrderItemId = NewType("OrderItemId", int)
VariantId = NewType("VariantId", int)
RefundId = NewType("RefundId", str)
ReturnId = NewType("ReturnId", str)
DeliveryId = NewType("DeliveryId", int)
DisputeId = NewType("DisputeId", str)

class SchemaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ------------------------------------------------------------ Value Objects
class Money(SchemaModel):
    paise: int

    @field_validator("paise")
    @classmethod
    def _validate_paise(cls, value: int) -> int:
        if not isinstance(value, int):
            raise TypeError("Money is integer paise")
        return value

    @classmethod
    def rupees(cls, rupees: int) -> "Money":
        return cls(paise=rupees * 100)

    def __add__(self, other: "Money") -> "Money":
        return Money(paise=self.paise + other.paise)

    def __sub__(self, other: "Money") -> "Money":
        return Money(paise=self.paise - other.paise)

    def __mul__(self, count: int) -> "Money":
        if not isinstance(count, int):
            raise TypeError("Money can only be multiplied by an integer count")
        return Money(paise=self.paise * count)

    def speak(self) -> str:
        rupees, paise = divmod(self.paise, 100)
        if paise:
            return f"{rupees} rupees {paise} paise"
        return f"{rupees} rupees"


class PhoneNumber(SchemaModel):
    digits: str

    @field_validator("digits")
    @classmethod
    def _validate_digits(cls, value: str) -> str:
        if len(value) != 10 or not value.isdigit():
            raise ValueError(f"phone number must be exactly 10 digits, got {value!r}")
        return value

    @classmethod
    def parse(cls, raw: str) -> "PhoneNumber":
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        return cls(digits=digits)


# ------------------------------------------------------------ Enums
class OrderStatus(StrEnum):
    PLACED = "placed"
    CONFIRMED = "confirmed"
    SHIPPED = "shipped"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    RETURNED = "returned"


class OrderItemStatus(StrEnum):
    ORDERED = "ordered"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    RETURN_REQUESTED = "return_requested"
    REPLACEMENT_REQUESTED = "replacement_requested"
    EXCHANGE_REQUESTED = "exchange_requested"
    CANCELLED = "cancelled"

class PaymentMethod(StrEnum):
    CARD = "card"
    NETBANKING = "netbanking"
    UPI = "upi"
    CASH_ON_DELIVERY = "cash_on_delivery"


class PaymentStatus(StrEnum):
    PAID = "paid"
    PENDING = "pending"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


class PaymentCollectionMode(StrEnum):
    CASH = "cash"
    STATIC_QR = "static_qr"
    PRE_DELIVERY_PAYMENT_LINK = "pre_delivery_payment_link"


class ReturnPolicyType(StrEnum):
    RETURNABLE_10D = "returnable_10d"
    RETURNABLE_30D = "returnable_30d"
    REPLACEMENT_ONLY_7D = "replacement_only_7d"
    REPLACEMENT_ONLY_10D = "replacement_only_10d"
    STANDARD = "standard"
    REPLACEMENT_ONLY = "replacement_only"
    NON_RETURNABLE = "non_returnable"




class Resolution(StrEnum):
    REFUND = "refund"
    REPLACEMENT = "replacement"
    EXCHANGE = "exchange"


class ReturnReason(StrEnum):
    DAMAGED = "damaged"
    DEFECTIVE = "defective"
    WRONG_ITEM = "wrong_item"
    MISSING_PARTS = "missing_parts"
    NOT_NEEDED = "not_needed"
    SIZE_ISSUE = "size_issue"


class ReturnRequestStatus(StrEnum):
    REQUESTED = "requested"
    INITIATED = "initiated"
    PICKUP_SCHEDULED = "pickup_scheduled"
    PICKED_UP = "picked_up"
    INSPECTED = "inspected"
    COMPLETED = "completed"
    REJECTED = "rejected"




class RefundMethod(StrEnum):
    ORIGINAL_SOURCE = "original_source"
    CARD = "card"
    UPI = "upi"
    NETBANKING = "netbanking"
    NEFT = "neft"
    CHEQUE = "cheque"
    STORE_CREDIT = "store_credit"


class RefundStatus(StrEnum):
    INITIATED = "initiated"
    PROCESSING = "processing"
    PROCESSED = "processed"
    COMPLETED = "completed"
    FAILED = "failed"


class CustomerAccountStatus(StrEnum):
    ACTIVE = "active"
    FLAGGED = "flagged"
    SUSPENDED = "suspended"


class VerifiedChannel(StrEnum):
    CALLER_ID = "caller_id"
    MANUAL_OTP = "manual_otp"
    UNVERIFIED = "unverified"


class ProductCategory(StrEnum):
    HYGIENE = "hygiene"
    ELECTRONICS = "electronics"
    APPAREL = "apparel"
    OTHER = "other"


class DeliveryStatus(StrEnum):
    MANIFESTED = "manifested"
    IN_TRANSIT = "in_transit"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    FAILED_ATTEMPT = "failed_attempt"
    RETURNED_TO_ORIGIN = "returned_to_origin"


class DeliverySlotPreference(StrEnum):
    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"


class DisputeType(StrEnum):
    ITEM_NOT_RECEIVED = "item_not_received"
    EMPTY_BOX = "empty_box"
    WRONG_LOCATION = "wrong_location"


class DisputeStatus(StrEnum):
    OPEN = "open"
    CARRIER_INVESTIGATION = "carrier_investigation"
    GEO_LOCATION_VERIFIED = "geo_location_verified"
    RESOLVED_REFUND = "resolved_refund"
    REJECTED_OTP_MATCH = "rejected_otp_match"


class FeedbackTargetType(StrEnum):
    OVERALL_ORDER = "overall_order"
    DELIVERY_EXPERIENCE = "delivery_experience"
    SPECIFIC_ITEM = "specific_item"


class FeedbackTag(StrEnum):
    LATE_DELIVERY = "late_delivery"
    BAD_PACKAGING = "bad_packaging"
    GOOD_SUPPORT = "good_support"
    QUALITY_ISSUE = "quality_issue"


# ------------------------------------------------------------ Payout Destinations

class UpiRefundDestination(SchemaModel):
    kind: Literal["upi"] = "upi"
    upi_id: str

class NeftRefundDestination(SchemaModel):
    kind: Literal["neft"] = "neft"
    account_holder_name: str
    account_number: str
    ifsc: str

RefundDestination = Annotated[
    UpiRefundDestination | NeftRefundDestination ,
    Field(discriminator="kind"),
]
# ------------------------------------------------------------ Core Entities
class Address(SchemaModel):
    address_id: int | None = None
    label: str
    line1: str
    city: str
    pincode: str


class Customer(SchemaModel):
    customer_id: CustomerId
    first_name: str
    last_name: str
    primary_phone: str
    email: str | None = None
    preferred_language: str | None = None
    saved_addresses: tuple[Address, ...] = Field(default_factory=tuple)

    @field_validator("primary_phone")
    @classmethod
    def _validate_phone(cls, value: str) -> str:
        return PhoneNumber.parse(value).digits


class CustomerAccount(SchemaModel):

    account_id: int
    customer_id: CustomerId
    status: CustomerAccountStatus = CustomerAccountStatus.ACTIVE
    verified_channels: tuple[VerifiedChannel, ...] = Field(default_factory=tuple)
    auth_failure_count: int = 0
    default_refund_destination: RefundDestination | None = None


class Product(SchemaModel):
    product_id: int
    title: str
    category: ProductCategory = ProductCategory.OTHER
    return_policy_type: ReturnPolicyType


class ProductVariant(SchemaModel):
    variant_id: VariantId
    product_id: int
    sku: str
    attributes: Mapping[str, str] = Field(default_factory=dict)
    stock_count: int
    price: Money


class OrderItemPolicySnapshot(SchemaModel):
    return_window_days: int | None = None
    allowed_resolutions: tuple[Resolution, ...] = Field(default_factory=tuple)
    is_returnable: bool = True


class OrderItem(SchemaModel):
    order_item_id: OrderItemId
    order_id: OrderId
    product_id: int
    variant_id: VariantId
    title: str
    quantity: int
    unit_price: Money
    total_price: Money
    status: OrderItemStatus = OrderItemStatus.ORDERED
    exchange_variant_id: VariantId | None = None
    policy_snapshot: OrderItemPolicySnapshot = Field(default_factory=OrderItemPolicySnapshot)


class Order(SchemaModel):
    order_id: OrderId
    customer_id: CustomerId
    placed_at: datetime
    delivered_at: datetime | None = None
    status: OrderStatus
    items: tuple[OrderItem, ...] = Field(default_factory=tuple)
    subtotal: Money
    tax_amount: Money
    shipping_fee: Money
    total_amount: Money
    payment_method: PaymentMethod
    payment_status: PaymentStatus = PaymentStatus.PENDING
    payment_collection_mode: PaymentCollectionMode | None = None
    pre_delivery_payment_link: str | None = None
    cancellable_until: datetime | None = None


class Delivery(SchemaModel):
    delivery_id: DeliveryId
    order_id: OrderId
    courier_partner: str | None = None
    tracking_number: str | None = None
    status: DeliveryStatus
    estimated_delivery_date: datetime | None = None
    actual_delivery_date: datetime | None = None
    rescheduled_delivery_date: datetime | None = None
    reschedule_count: int = 0
    delivery_slot_preference: DeliverySlotPreference | None = None
    delivery_instructions: str | None = None
    latest_checkpoint_text: str | None = None
    failed_attempt_reason: str | None = None


class ReturnRequest(SchemaModel):
    return_id: ReturnId
    order_id: OrderId
    order_item_id: OrderItemId
    reason: ReturnReason
    resolution_type: Resolution
    status: ReturnRequestStatus = ReturnRequestStatus.INITIATED
    pickup_date: datetime | None = None
    pickup_slot: DeliverySlotPreference | None = None
    pickup_address_id: int | None = None


class Refund(SchemaModel):
    refund_id: RefundId
    order_id: OrderId
    return_id: ReturnId | None = None
    amount: Money
    status: RefundStatus
    method: RefundMethod
    initiated_at: datetime
    expected_by_date: datetime
    arn: str | None = None


class DisputeTicket(SchemaModel):
    dispute_id: DisputeId
    order_id: OrderId
    delivery_id: DeliveryId | None = None
    customer_id: CustomerId
    dispute_type: DisputeType
    status: DisputeStatus = DisputeStatus.OPEN
    reported_at: datetime
    sla_resolution_deadline: datetime
    investigation_notes: tuple[str, ...] = Field(default_factory=tuple)


class OrderFeedback(SchemaModel):
    feedback_id: str
    order_id: OrderId
    customer_id: CustomerId
    target_type: FeedbackTargetType
    target_item_id: OrderItemId | None = None
    rating: Annotated[int, Field(ge=1, le=5)]
    category_tags: tuple[FeedbackTag, ...] = Field(default_factory=tuple)
    spoken_comment: str | None = None
    created_at: datetime


class DomainEvent(SchemaModel):
    seq: int
    occurred_at: datetime
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: Mapping[str, Any]
    idempotency_key: str | None = None


# ------------------------------------------------------------ View / Read Models
class OrderSummary(SchemaModel):
    order_id: OrderId
    status: OrderStatus
    payment_method: PaymentMethod
    placed_at: datetime
    delivered_at: datetime | None = None
    item_titles: tuple[str, ...] = Field(default_factory=tuple)
    total_amount: Money


class ItemDetail(SchemaModel):
    item: OrderItem
    variant: ProductVariant
    product: Product


class RefundView(SchemaModel):
    refund: Refund
    order_item_titles: tuple[str, ...] = Field(default_factory=tuple)


class RefundInfo(SchemaModel):
    refund_id: RefundId
    amount: Money
    method: RefundMethod
    expected_by: datetime


class CancelResult(SchemaModel):
    order_id: OrderId
    refund: RefundInfo | None = None


class ReturnResult(SchemaModel):
    return_id: ReturnId
    order_item_id: OrderItemId
    resolution: Resolution
    refund: RefundInfo | None = None
    pickup_by: datetime