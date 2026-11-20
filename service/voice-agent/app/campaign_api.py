"""Campaign and call-record API for the future GTMLeads admin UI."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.campaign_store import CallRecord, Campaign
from app.exotel_client import ExotelCallError
from app.outbound_calls import CallUnavailableError

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


class CreateCampaignRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    business_name: str = Field(min_length=1, max_length=200)


class CampaignResponse(BaseModel):
    id: str
    name: str
    business_name: str
    approved: bool

    @classmethod
    def from_model(cls, campaign: Campaign) -> "CampaignResponse":
        return cls(
            id=campaign.id,
            name=campaign.name,
            business_name=campaign.business_name,
            approved=campaign.approved,
        )


class CreateCallRequest(BaseModel):
    phone_number: str = Field(min_length=7, max_length=32)
    seller_name: str | None = Field(default=None, max_length=200)


class CallResponse(BaseModel):
    id: str
    campaign_id: str
    seller_name: str | None
    phone_number: str
    status: str
    exotel_call_sid: str | None
    stream_sid: str | None
    outcome: str | None

    @classmethod
    def from_model(cls, call: CallRecord) -> "CallResponse":
        return cls(
            id=call.id,
            campaign_id=call.campaign_id,
            seller_name=call.seller_name,
            phone_number=call.phone_number,
            status=call.status,
            exotel_call_sid=call.exotel_call_sid,
            stream_sid=call.stream_sid,
            outcome=call.outcome,
        )


def _store(request: Request):
    return request.app.state.campaign_store


@router.post("", status_code=status.HTTP_201_CREATED, response_model=CampaignResponse)
async def create_campaign(payload: CreateCampaignRequest, request: Request):
    campaign = await _store(request).create_campaign(
        name=payload.name,
        business_name=payload.business_name,
    )
    return CampaignResponse.from_model(campaign)


@router.post("/{campaign_id}/approve", response_model=CampaignResponse)
async def approve_campaign(campaign_id: str, request: Request):
    campaign = await _store(request).approve_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return CampaignResponse.from_model(campaign)


@router.post("/{campaign_id}/calls", status_code=status.HTTP_201_CREATED, response_model=CallResponse)
async def create_approved_call(
    campaign_id: str,
    payload: CreateCallRequest,
    request: Request,
):
    try:
        call = await _store(request).create_call(
            campaign_id=campaign_id,
            phone_number=payload.phone_number,
            seller_name=payload.seller_name,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Campaign not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail="Campaign has not been approved") from exc
    return CallResponse.from_model(call)


@router.get("/{campaign_id}/calls", response_model=list[CallResponse])
async def list_calls(campaign_id: str, request: Request):
    campaign = await _store(request).get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    calls = await _store(request).list_calls(campaign_id)
    return [CallResponse.from_model(call) for call in calls]


@router.post("/{campaign_id}/calls/{call_id}/start", response_model=CallResponse)
async def start_outbound_call(campaign_id: str, call_id: str, request: Request):
    """Start a queued call only when an authorized service is configured."""
    verifier = getattr(request.app.state, "outbound_request_verifier", None)
    service = getattr(request.app.state, "outbound_call_service", None)
    if verifier is None or not await verifier(request):
        raise HTTPException(status_code=403, detail="Call request not authorized")
    if service is None:
        raise HTTPException(status_code=503, detail="Outbound calling is not configured")
    call = await _store(request).get_call(call_id)
    if call is None or call.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="Call not found")
    try:
        result = await service.start_call(call_id)
    except CallUnavailableError as exc:
        raise HTTPException(status_code=409, detail="Call is not queued") from exc
    except ExotelCallError as exc:
        raise HTTPException(status_code=502, detail="Call outcome unknown; do not retry until reconciled") from exc
    return CallResponse.from_model(result)
