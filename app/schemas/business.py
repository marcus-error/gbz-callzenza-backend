from datetime import datetime
from pydantic import BaseModel


class BusinessCreate(BaseModel):
    name: str
    industry: str = ""


class BusinessUpdate(BaseModel):
    name: str | None = None
    industry: str | None = None


class BusinessOut(BaseModel):
    id: str
    name: str
    industry: str | None = ""
    owner_user_id: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class BillingPreferenceUpdate(BaseModel):
    billing_company_name: str = ""
    billing_email: str = ""
    billing_address: str = ""
    billing_tax_id: str = ""


class BillingPreferenceOut(BaseModel):
    billing_company_name: str | None = ""
    billing_email: str | None = ""
    billing_address: str | None = ""
    billing_tax_id: str | None = ""
    wallet_balance: float
    current_plan: str

    model_config = {"from_attributes": True}


class InvoiceOut(BaseModel):
    id: str
    invoice_number: str
    date: str
    description: str
    amount: str
    status: str

    model_config = {"from_attributes": True}


class PaymentPayload(BaseModel):
    amount_added: float
    plan_name: str | None = None
    invoice_description: str

