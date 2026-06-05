"""
Shipment API — resolve a scanned barcode to its shipment/order info.

Backed by the pluggable lookup (Airtable now, FedEx later). Fail-safe: an
unconfigured or failing lookup returns {"found": false} rather than erroring,
so the capture flow is never blocked.
"""
from fastapi import APIRouter

from backend.services.shipment_lookup import get_shipment_lookup, is_configured

router = APIRouter(prefix="/api", tags=["Shipment"])


@router.get("/shipment/{barcode}", summary="Resolve a barcode to shipment info")
def get_shipment(barcode: str):
    info = get_shipment_lookup().resolve(barcode)
    if not info:
        return {"found": False, "barcode": barcode, "configured": is_configured()}
    info.setdefault("configured", is_configured())
    return info
