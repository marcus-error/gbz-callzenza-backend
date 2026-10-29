"""Meta WhatsApp Cloud API service (TS-029)."""

import httpx

from loguru import logger
from app.core.config import get_settings

settings = get_settings()


async def send_whatsapp_message(to: str, text: str, token: str = None, phone_number_id: str = None) -> dict:
    active_token = (token or settings.whatsapp_token or "").strip()
    if active_token.startswith("WHATSAPP_TOKEN="):
        active_token = active_token[len("WHATSAPP_TOKEN="):].strip()
    active_token = active_token.strip("\"'")

    # If active_token contains bullet characters (e.g. ••••••••), fall back to settings.whatsapp_token
    if any(ord(c) > 127 for c in active_token):
        active_token = (settings.whatsapp_token or "").replace("WHATSAPP_TOKEN=", "").strip("\"'").strip()

    active_phone_id = (phone_number_id or settings.whatsapp_phone_number_id or "").strip()

    if not active_token or not active_phone_id:
        logger.warning("[WHATSAPP] Token or phone number ID missing in config")
        return {"status": "stub", "detail": "Set WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID to send real messages"}

    # Sanitize destination phone number to digits only (Meta Cloud API requirement)
    clean_to = "".join(filter(str.isdigit, to))
    if not clean_to:
        logger.warning(f"[WHATSAPP] Invalid recipient phone number format: {to}")
        return {"status": "error", "detail": f"Invalid recipient number: {to}"}

    url = f"https://graph.facebook.com/v23.0/{active_phone_id}/messages"
    logger.info(f"[WHATSAPP] Sending message via Meta Cloud API to {clean_to} (Phone ID: {active_phone_id})")

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {active_token}",
                "Content-Type": "application/json"
            },
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": clean_to,
                "type": "text",
                "text": {"preview_url": False, "body": text},
            },
        )
        logger.info(f"[WHATSAPP] Meta API response status: {resp.status_code}")
        if resp.status_code >= 400:
            err_json = resp.json() if "json" in resp.headers.get("content-type", "") else {"error": resp.text}
            logger.warning(f"[WHATSAPP] Meta API returned error: {err_json}")
            
            # Check if failure is due to Meta 24-hour messaging policy (code 131030 or 131047)
            err_code = err_json.get("error", {}).get("code")
            if err_code in (131030, 131047):
                logger.info(f"[WHATSAPP] 24-hour policy triggered for {clean_to}. Falling back to pre-approved 'hello_world' template...")
                try:
                    template_resp = await client.post(
                        url,
                        headers={
                            "Authorization": f"Bearer {active_token}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "messaging_product": "whatsapp",
                            "to": clean_to,
                            "type": "template",
                            "template": {
                                "name": "hello_world",
                                "language": {"code": "en_US"}
                            }
                        }
                    )
                    logger.info(f"[WHATSAPP] Template fallback response status: {template_resp.status_code}")
                    if template_resp.status_code < 400:
                        return template_resp.json()
                except Exception as ex:
                    logger.error(f"[WHATSAPP] Template fallback request error: {ex}")

            return {"status": "error", "error": err_json}
        return resp.json()
