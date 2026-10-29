"""CRM Connector (TS-025) — Salesforce / Zoho / HubSpot behind one interface.

Select the provider via CRM_PROVIDER in .env. Each provider needs its own
OAuth/API key setup; only HubSpot's simple API-key flow is sketched below as
an example. Add Salesforce (JWT bearer flow) / Zoho (OAuth2) similarly.
"""

import httpx

from app.core.config import get_settings

settings = get_settings()


async def upsert_contact(email: str, first_name: str = "", last_name: str = "") -> dict:
    if not settings.crm_api_key:
        return {"status": "stub", "detail": f"Set CRM_API_KEY for provider={settings.crm_provider}"}

    if settings.crm_provider == "hubspot":
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.hubapi.com/crm/v3/objects/contacts",
                headers={"Authorization": f"Bearer {settings.crm_api_key}"},
                json={"properties": {"email": email, "firstname": first_name, "lastname": last_name}},
            )
            resp.raise_for_status()
            return resp.json()

    return {"status": "stub", "detail": f"Provider '{settings.crm_provider}' connector not yet implemented"}
