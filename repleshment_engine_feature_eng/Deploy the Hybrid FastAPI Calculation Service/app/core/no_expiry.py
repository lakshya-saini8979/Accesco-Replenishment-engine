"""
Job A: No-Expiry & Fresh Routing Engine
Evaluates all batches on the shelf in PostgreSQL to determine if they are sellable,
routed to the Swadishtt kitchen (to absorb aging stock), or composted.
Enforces the FSSAI floor (>= 30% life and >= 45 days remaining for packaged goods)
and 48h/72h harvest rules for fresh produce.
"""

from datetime import datetime, timezone
from typing import Dict, List
from app.database import db
from app.models.schemas import BatchStatus, FreshRoutingReport, FreshRoutingItem
import logging

logger = logging.getLogger(__name__)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def execute_routing() -> FreshRoutingReport:
    """
    Executes daytime / continuous shelf life routing calculation against PostgreSQL:
    Packaged goods:
      L(batch, t) = (expiry_date - current_date) / (expiry_date - mfg_date)
      FSSAI floor: must have >= 30% life remaining and >= 45 days remaining to leave for customers.
      L >= 0.50 and remaining_days >= 45 -> SELLABLE
      0.30 <= L < 0.50 or remaining_days < 45 (if L >= 0.30) -> ROUTE_TO_KITCHEN
      L < 0.30 -> COMPOST / RETURN TO SUPPLIER

    Fresh produce:
      hours <= 48      -> SELLABLE
      48 < hours <= 72 -> ROUTE_TO_KITCHEN
      hours > 72       -> COMPOST
    """
    now = datetime.now(timezone.utc)
    batches = await db.get_all_batches()
    skus_list = await db.get_all_skus()
    sku_map = {s.sku_id: s for s in skus_list}
    batch_ref_dates: Dict[str, datetime] = {}

    sellable_items: List[FreshRoutingItem] = []
    kitchen_items: List[FreshRoutingItem] = []
    compost_items: List[FreshRoutingItem] = []

    for b in batches:
        sku = sku_map.get(b.sku_id)
        if not sku:
            continue

        ref_date = _to_utc(b.manufacture_or_harvest_date)
        batch_ref_dates[b.batch_id] = ref_date

        if sku.is_fresh:
            hours_elapsed = (now - ref_date).total_seconds() / 3600.0

            if hours_elapsed <= 48.0:
                new_status = BatchStatus.SELLABLE
                action = f"Sound shelf stock ({hours_elapsed:.1f}h <= 48h); available for customer orders."
            elif 48.0 < hours_elapsed <= 72.0:
                new_status = BatchStatus.ROUTE_TO_KITCHEN
                action = f"Approaching freshness limit ({hours_elapsed:.1f}h > 48h); routed to Swadishtt kitchen."
            else:
                new_status = BatchStatus.COMPOST
                action = f"Exceeded 72h harvest limit ({hours_elapsed:.1f}h); routed to composting."

            await db.update_batch_status(b.batch_id, new_status, metric=round(hours_elapsed, 2))

            item = FreshRoutingItem(
                batch_id=b.batch_id,
                sku_id=b.sku_id,
                name=sku.name,
                category=sku.category.value,
                quantity=b.quantity,
                age_metric=f"{hours_elapsed:.1f} hours elapsed",
                status=new_status,
                action_notes=action,
            )
        else:
            expiry_date = _to_utc(b.expiry_date) if b.expiry_date else now

            total_lifespan_sec = (expiry_date - ref_date).total_seconds()
            remaining_sec = (expiry_date - now).total_seconds()
            remaining_days = remaining_sec / 86400.0

            if total_lifespan_sec > 0:
                L = max(0.0, remaining_sec / total_lifespan_sec)
            else:
                L = 0.0

            fssai_compliant = (L >= 0.30) and (remaining_days >= 45.0)

            if L >= 0.50 and fssai_compliant:
                new_status = BatchStatus.SELLABLE
                action = f"Sound packaged stock (L={L:.2f} >= 0.50, {remaining_days:.0f}d remaining)."
            elif (0.30 <= L < 0.50) or (not fssai_compliant and L >= 0.30):
                new_status = BatchStatus.ROUTE_TO_KITCHEN
                action = (
                    f"FSSAI threshold / shelf transition (L={L:.2f}, {remaining_days:.0f}d remaining); "
                    f"routed to Swadishtt kitchen for immediate cooking."
                )
            else:
                new_status = BatchStatus.COMPOST
                action = f"Sub-critical shelf life (L={L:.2f} < 0.30); removed from active inventory."

            await db.update_batch_status(b.batch_id, new_status, metric=round(L, 4))

            item = FreshRoutingItem(
                batch_id=b.batch_id,
                sku_id=b.sku_id,
                name=sku.name,
                category=sku.category.value,
                quantity=b.quantity,
                age_metric=f"L={L:.2f} ({remaining_days:.0f} days remaining)",
                status=new_status,
                action_notes=action,
            )

        if new_status == BatchStatus.SELLABLE:
            sellable_items.append(item)
        elif new_status == BatchStatus.ROUTE_TO_KITCHEN:
            kitchen_items.append(item)
        else:
            compost_items.append(item)

    # FEFO Sorting: Order sellable batches by oldest harvest/mfg date first
    sellable_items.sort(key=lambda x: batch_ref_dates[x.batch_id])

    report = FreshRoutingReport(
        timestamp=now,
        total_batches_checked=len(batches),
        sellable_count=len(sellable_items),
        routed_to_kitchen_count=len(kitchen_items),
        compost_count=len(compost_items),
        routed_to_kitchen_items=kitchen_items,
        compost_items=compost_items,
        sellable_items=sellable_items,
    )

    logger.info(
        f"Fresh Routing completed: {len(sellable_items)} Sellable, "
        f"{len(kitchen_items)} to Kitchen, {len(compost_items)} to Compost."
    )
    return report
