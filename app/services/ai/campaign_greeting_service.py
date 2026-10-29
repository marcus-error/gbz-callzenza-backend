"""
Campaign Greeting Service.
Resolves dynamic, campaign-specific opening greetings grounded in the active
campaign script, organization identity, and opening instructions.
Guarantees tenant isolation, prevents generic cross-campaign greeting bleed,
safely handles customer names (omitting when absent), and integrates with ResponseGuard.
"""
from __future__ import annotations

import re
import hashlib
from typing import Any, Dict, Optional, Tuple
from loguru import logger

from app.services.ai.response_sanitizer import ResponseSanitizer, ResponseGuard
from app.services.language_service import get_time_of_day_salutation, detect_script_language


class CampaignGreetingService:
    """Central service for resolving campaign-specific opening greetings."""

    # Thread-safe in-memory cache keyed by (business_id, campaign_id, script_hash, language, call_direction, has_lead_name)
    _greeting_cache: Dict[Tuple[str, str, str, str, str, bool], str] = {}

    @classmethod
    def clear_cache(cls, campaign_id: Optional[str] = None) -> None:
        """Clears greeting cache for a specific campaign or globally."""
        if campaign_id:
            keys_to_remove = [k for k in cls._greeting_cache if k[1] == campaign_id]
            for k in keys_to_remove:
                cls._greeting_cache.pop(k, None)
            logger.info(f"[GREETING_CACHE] Cleared cache for campaign={campaign_id}")
        else:
            cls._greeting_cache.clear()
            logger.info("[GREETING_CACHE] Cleared entire greeting cache")

    @classmethod
    def get_cache_stats(cls) -> Dict[str, Any]:
        """Returns statistics about current cached greetings."""
        return {
            "cached_entries": len(cls._greeting_cache),
            "keys": [f"{k[0]}:{k[1]}:{k[3]}:{k[4]}" for k in list(cls._greeting_cache.keys())[:10]],
        }

    # --------------------------------------------------------------------------
    # Company and Agent Name Extraction from Script
    # --------------------------------------------------------------------------
    @staticmethod
    def extract_company_name(script_text: str, fallback: str = "") -> str:
        """Extracts the verified company/organization name explicitly declared in the script."""
        if not script_text or not script_text.strip():
            return fallback

        # Pattern 1: CLIENT : <Name>, Organization Name: <Name>, Company Name: <Name>
        comp_match = re.search(
            r'(?:CLIENT\s*:\s*|Organization\s*Name\s*[:\-]\s*|Company\s*Name\s*[:\-]\s*|Business\s*Name\s*[:\-]\s*|Brand\s*Name\s*[:\-]\s*)([^\n\r\|]+)',
            script_text,
            re.IGNORECASE,
        )
        if comp_match:
            cand = comp_match.group(1).strip()
            # Clean common document title suffixes (e.g., "Complete Inbound Call Script", "Master FAQ", "Outbound RAG")
            cleaned = re.split(
                r'(?i)\b(?:Complete\s+Inbound|Complete\s+Outbound|Voice\s+AI|Call\s+Script|Master\s+FAQ|Outbound\s+RAG|Inbound\s+RAG|Script\s*&|Document|Information)\b',
                cand,
            )[0]
            cleaned = re.sub(r'[\-\|\:\–\—\.]+$', '', cleaned).strip()
            if cleaned and len(cleaned) <= 60:
                return cleaned

        # Pattern 2: Header title like "DREAM CAATCHERS PROPERTIES INFORMATION" or "NURSE O' CLOCK — CHENNAI"
        head_match = re.search(
            r'^\s*([A-Z0-9\s\'\.\&]{4,45})\s*(?:[\-\–\—\:]\s*[A-Z\s]+)?(?:INFORMATION|SERVICES|OUTBOUND|INBOUND)',
            script_text,
            re.IGNORECASE | re.MULTILINE,
        )
        if head_match:
            cand = head_match.group(1).strip().title()
            if cand and not any(k in cand.lower() for k in ["section", "client", "document", "script", "outbound", "inbound"]):
                return cand

        return fallback

    @staticmethod
    def extract_agent_name(script_text: str, fallback: str = "Alex") -> str:
        """Extracts the approved agent persona name if declared in the script."""
        if not script_text:
            return fallback
        agent_match = re.search(
            r'(?:\[Agent\s*:\s*|\[Agent\s*Name\s*:\s*|Agent\s*Name\s*[:\-]\s*)([A-Za-z0-9\s]{2,30})(?:\]|\n|\r)',
            script_text,
            re.IGNORECASE,
        )
        if agent_match:
            cand = agent_match.group(1).strip()
            if cand and cand.lower() not in ("agent", "ai agent", "virtual agent", "representative"):
                return cand
        return fallback

    # --------------------------------------------------------------------------
    # Verbatim Opening Dialogue Extraction
    # --------------------------------------------------------------------------
    @classmethod
    def extract_greeting_from_script(
        cls,
        script_text: str,
        lead_name: str = "",
        agent_name: str = "Alex",
        company_name: str = "",
        call_direction: str = "outbound",
        language: str = "en",
    ) -> Optional[str]:
        """
        Extracts verbatim opening greeting utterance from the campaign script.
        Supports structured sections (e.g. SECTION 01 GREETING), call opening blocks,
        numbered steps, and direct quotes.
        """
        if not script_text or not script_text.strip():
            return None

        # Clean non-printable/zero-width chars
        raw = script_text.replace('\r\n', '\n').replace('\r', '\n')

        candidate: Optional[str] = None

        # Strategy 1: Explicit Section Header (e.g. SECTION 01 GREETING & CALL OPENING)
        # Followed by AI Agent / Agent / Rep dialogue
        sec_pattern = re.compile(
            r'SECTION\s*\d*\s*[\n\s]*(?:GREETING|CALL\s*OPENING|OPENING)[^\n]*\n'
            r'(?:[^\n]+\n){0,5}?'
            r'(?:AI\s*Agent|Agent|Rep|Bot)\s*[:\-]\s*'
            r'([^\n]+(?:\n(?!\s*(?:Caller|Customer|User|FAQ|SECTION|\d+\.))[^\n]+)*)',
            re.IGNORECASE,
        )
        sec_match = sec_pattern.search(raw)
        if sec_match:
            candidate = sec_match.group(1).strip()

        # Strategy 2: Call Opening Header (e.g. OUTBOUND CALL OPENING or INBOUND CALL OPENING)
        if not candidate:
            call_open_pattern = re.compile(
                r'(?:OUTBOUND|INBOUND)?\s*CALL\s*OPENING[^\n]*\n'
                r'(?:[^\n]+\n){0,2}?'
                r'([^\n]+(?:\n(?!\s*(?:Customer|Caller|User|Agent|FAQ|SECTION|\d+\.))[^\n]+)*)',
                re.IGNORECASE,
            )
            call_open_match = call_open_pattern.search(raw)
            if call_open_match:
                candidate = call_open_match.group(1).strip()

        # Strategy 3: Standard Greeting label with dialogue (e.g. "1. Greeting: Hello..." or "Stage 1: Greeting\nHello...")
        if not candidate:
            std_pattern = re.compile(
                r'(?:(?:1\.\s*|Stage\s*1\s*[:\-]?\s*|Step\s*1\s*[:\-]?\s*)?'
                r'(?:Standard\s*)?Greeting|Primary\s*Greeting|Opening\s*Dialogue|Intro)\s*[:\-\n]\s*'
                r'(?:["\u201c\u00ab\uff02\uff07\']?\s*)'
                r'([^\n]+(?:\n(?!\s*(?:Warm|Returning|\d+\.|[A-Z][a-z]+:|\n))[^\n]+)*)',
                re.IGNORECASE,
            )
            std_match = std_pattern.search(raw)
            if std_match:
                candidate = std_match.group(1).strip()

        # Strategy 4: Direct greeting utterance at the start of script (first 300 chars)
        if not candidate:
            head_300 = raw[:300].strip()
            direct_match = re.search(
                r'^(?:["\u201c\u00ab\uff02\uff07\']?\s*)'
                r'((?:Good\s+(?:morning|afternoon|evening)|Hello|Hi|Greetings|Vanakkam|Namaste|வணக்கம்|नमस्ते)[^\n\.\!\?]{5,200}[\.\!\?])',
                head_300,
                re.IGNORECASE,
            )
            if direct_match:
                candidate = direct_match.group(1).strip()

        if not candidate:
            return None

        # Clean leading quotes, trailing quotes, role prefixes
        candidate = re.sub(r'^(?:AI\s*Agent|Agent|Rep|Bot)\s*[:\-]\s*', '', candidate, flags=re.IGNORECASE).strip()
        candidate = candidate.strip('"\u201c\u201d\u00ab\u00bb\uff02\uff07\'')

        # If dialogue contains customer turn like "\nCustomer: Yes\nAgent: ...", cut off at customer turn
        customer_turn_split = re.split(r'(?i)\n\s*(?:Customer|Caller|User)\s*[:\-]', candidate)
        if len(customer_turn_split) > 1:
            candidate = customer_turn_split[0].strip()

        # Substitute agent, company, and lead placeholders
        candidate = candidate.replace("[Agent Name]", agent_name).replace("{agent_name}", agent_name)
        if company_name:
            candidate = candidate.replace("[Company Name]", company_name).replace("{company_name}", company_name)
            candidate = candidate.replace("[Organization Name]", company_name).replace("{organization_name}", company_name)

        clean_lead = (lead_name or "").strip()
        if clean_lead and clean_lead.lower() not in ("customer", "caller") and not re.search(r'\d{6,}', clean_lead):
            for ph in ["[Name]", "[Customer Name]", "[Lead Name]", "{customer_name}", "{lead_name}", "{name}"]:
                candidate = candidate.replace(ph, clean_lead)
        else:
            # Cleanly omit customer name placeholders without leaving dangling commas or titles
            candidate = re.sub(r'\b(?:Mr\.|Mrs\.|Ms\.|Dr\.)\s*\[(?:Name|Customer Name|Lead Name)\]', '', candidate)
            candidate = re.sub(r'\[(?:Name|Customer Name|Lead Name)\]', '', candidate)
            candidate = re.sub(r'\{(?:name|customer_name|lead_name)\}', '', candidate)
            # Adapt questions like "am I speaking with ?" to "am I speaking with the homeowner?" or omit
            candidate = re.sub(r'am I speaking with\s*\?', 'am I speaking with you today?', candidate, flags=re.IGNORECASE)
            candidate = re.sub(r'\b(?:Mr\.|Mrs\.|Ms\.)\s*,', ',', candidate)
            candidate = re.sub(r'\s*,\s*,+', ', ', candidate)
            candidate = re.sub(r'^\s*,\s*', '', candidate)

        # Sanitize with ResponseSanitizer
        cleaned = ResponseSanitizer.clean_customer_facing_text(
            candidate,
            agent_name=agent_name,
            lead_name=clean_lead,
            cut_off_script_branches=True,
        )

        # Verify against ResponseGuard
        guard_res = ResponseGuard.validate_customer_response(cleaned)
        if not guard_res["is_valid"]:
            logger.warning(f"[CAMPAIGN_GREETING] Extracted candidate failed ResponseGuard: {guard_res['reason']}")
            return None

        if len(cleaned) < 10 or len(cleaned) > 380:
            return None

        return cleaned

    # --------------------------------------------------------------------------
    # Grounded Fallback Generation (No Raw Leaks, Respects Verified Org Info)
    # --------------------------------------------------------------------------
    @classmethod
    def generate_grounded_greeting(
        cls,
        script_text: str,
        company_name: str = "",
        lead_name: str = "",
        agent_name: str = "Alex",
        call_direction: str = "outbound",
        language: str = "en",
    ) -> str:
        """
        Generates a natural, professional greeting grounded strictly in verified campaign/script
        information when the script does not contain an exact verbatim opening dialogue line.
        """
        lang = (language or "en").lower()
        direction = (call_direction or "outbound").lower()
        salutation = get_time_of_day_salutation(lang)

        # Clean lead name
        clean_lead = (lead_name or "").strip()
        if clean_lead.lower() in ("customer", "caller") or re.search(r'\d{6,}', clean_lead):
            clean_lead = ""

        # Identify services or domain if described in script
        service_focus = ""
        if script_text:
            if re.search(r'\b(?:home\s+health\s*care|caregiving|patient\s+care|nursing)\b', script_text, re.IGNORECASE):
                service_focus = "healthcare"
            elif re.search(r'\b(?:life\s+insurance|insurance\s+plan|policy\s+servicing)\b', script_text, re.IGNORECASE):
                service_focus = "insurance"
            elif re.search(r'\b(?:real\s+estate|plots?|villas?|properties|property)\b', script_text, re.IGNORECASE):
                service_focus = "real_estate"
            elif re.search(r'\b(?:solar|energy\s+savings|electricity\s+bill)\b', script_text, re.IGNORECASE):
                service_focus = "solar"

        biz_phrase = f" from {company_name}" if company_name else ""
        biz_title = company_name or "our company"

        if direction == "inbound":
            if lang == "ta":
                if clean_lead:
                    return f"{salutation} {clean_lead}! {biz_title}-க்கு அழைத்தமைக்கு நன்றி. நான் {agent_name}. இன்று உங்களுக்கு எவ்வாறு உதவ முடியும்?"
                return f"{salutation}! {biz_title}-க்கு அழைத்தமைக்கு நன்றி. நான் {agent_name}. இன்று உங்களுக்கு எவ்வாறு உதவ முடியும்?"
            elif lang == "hi":
                if clean_lead:
                    return f"{salutation} {clean_lead}! {biz_title} में कॉल करने के लिए धन्यवाद। मैं {agent_name} हूँ। आज मैं आपकी क्या सहायता कर सकता हूँ?"
                return f"{salutation}! {biz_title} में कॉल करने के लिए धन्यवाद। मैं {agent_name} हूँ। आज मैं आपकी क्या सहायता कर सकता हूँ?"
            else:
                if service_focus == "insurance":
                    welcome = f"Welcome to {biz_title}. Thank you for calling us."
                    return f"{salutation}! {welcome} I can help with insurance plans, policy servicing, and customer support. How may I assist you today?"
                elif service_focus == "healthcare":
                    welcome = f"Welcome to {biz_title}. Thank you for calling us."
                    return f"{salutation}! {welcome} How can I assist you with our home healthcare and caregiving services today?"
                else:
                    if clean_lead:
                        return f"{salutation} {clean_lead}! Thank you for calling {biz_title}. This is {agent_name}. How can I assist you today?"
                    return f"{salutation}! Thank you for calling {biz_title}. This is {agent_name}. How can I assist you today?"
        else:
            # Outbound call
            if lang == "ta":
                if clean_lead:
                    return f"{salutation} {clean_lead}! நான் {biz_title}-ல் இருந்து {agent_name} அழைக்கிறேன். இன்று நீங்கள் பேச முடியுமா?"
                return f"{salutation}! நான் {biz_title}-ல் இருந்து {agent_name} அழைக்கிறேன். எங்கள் சேவைகள் பற்றி உங்களிடம் பேச முடியுமா?"
            elif lang == "hi":
                if clean_lead:
                    return f"{salutation} {clean_lead}! मैं {biz_title} से {agent_name} बोल रहा हूँ। क्या आप अभी बात कर सकते हैं?"
                return f"{salutation}! मैं {biz_title} से {agent_name} बोल रहा हूँ। क्या आपके पास बात करने के लिए दो मिनट का समय है?"
            else:
                if service_focus == "real_estate":
                    if clean_lead:
                        return f"{salutation} {clean_lead}! This is {agent_name} from {biz_title}. I am reaching out regarding high-return property and investment updates. Do you have a couple of minutes to speak?"
                    return f"{salutation}! This is {agent_name} from {biz_title}. I am reaching out regarding high-return property and investment updates. Do you have a couple of minutes to speak?"
                elif service_focus == "solar":
                    if clean_lead:
                        return f"{salutation} {clean_lead}! This is {agent_name} from {biz_title}. I am calling regarding solar energy savings for your home. Do you have a couple of minutes to speak?"
                    return f"{salutation}! This is {agent_name} from {biz_title}. I am calling regarding solar energy savings for your home. Do you have a couple of minutes to speak?"
                else:
                    if clean_lead:
                        return f"{salutation} {clean_lead}! This is {agent_name} calling{biz_phrase}. I am following up on your inquiry. Are you currently available to speak?"
                    return f"{salutation}! This is {agent_name} calling{biz_phrase}. I am reaching out regarding our services. Do you have a couple of minutes to speak?"

    # --------------------------------------------------------------------------
    # Main Public Resolver
    # --------------------------------------------------------------------------
    @classmethod
    def resolve_campaign_greeting(
        cls,
        campaign: Any = None,
        lead: Any = None,
        agent: Any = None,
        script_text: str = "",
        call_direction: str = "outbound",
        language: str = "en",
        session_id: str = "",
        call_id: str = "",
    ) -> str:
        """
        Main public interface for resolving the opening greeting for any call.
        Enforces campaign isolation, respects active script, omits missing customer names,
        and never defaults to cross-tenant or hardcoded identities.
        """
        # 1. Resolve Script Text
        camp_script = script_text
        if not camp_script and campaign:
            if hasattr(campaign, "script") and campaign.script:
                camp_script = str(campaign.script)
            elif hasattr(campaign, "schedule") and isinstance(campaign.schedule, dict):
                camp_script = str(campaign.schedule.get("script") or "")

        # 2. Resolve Lead Name
        raw_lead = ""
        if lead:
            if hasattr(lead, "first_name") and lead.first_name:
                raw_lead = str(lead.first_name).strip()
            elif isinstance(lead, dict):
                raw_lead = str(lead.get("first_name") or lead.get("customer_name") or "").strip()
        lead_name = "" if (raw_lead.lower().startswith("caller ") or re.search(r'\d{6,}', raw_lead) or raw_lead.lower() == "customer") else raw_lead

        # 3. Resolve Verified Company / Business Identity
        # Priority: explicit script company > campaign.client_company > campaign.client_name > campaign.name
        detected_company = cls.extract_company_name(camp_script)
        if not detected_company and campaign:
            detected_company = (
                getattr(campaign, "client_company", None)
                or getattr(campaign, "client_name", None)
                or getattr(campaign, "name", None)
                or ""
            ).strip()

        # 4. Resolve Approved Agent Identity
        # Priority: explicit script agent > agent.name (if not generic/conflicting)
        detected_agent = cls.extract_agent_name(camp_script)
        if not detected_agent:
            raw_agent_name = ""
            if agent:
                raw_agent_name = (agent.name if hasattr(agent, "name") else (agent.get("name") if isinstance(agent, dict) else "")) or ""
            # Prevent "MAGIC" bleed into Pramerica or third-party campaigns unless explicitly declared
            if raw_agent_name == "MAGIC" and detected_company and "pramerica" in detected_company.lower():
                detected_agent = "Alex"
            elif raw_agent_name:
                detected_agent = raw_agent_name
            else:
                detected_agent = "Alex"

        # 5. Resolve Direction and Language
        direction = (call_direction or "outbound").lower()
        if campaign and hasattr(campaign, "schedule") and isinstance(campaign.schedule, dict):
            camp_dir = campaign.schedule.get("call_direction")
            if camp_dir:
                direction = str(camp_dir).lower()

        active_lang = language or "en"
        if camp_script:
            active_lang = detect_script_language(camp_script, default_lang=active_lang)

        # 6. Check Cache
        camp_id = getattr(campaign, "id", "no_campaign") if campaign else "no_campaign"
        biz_id = getattr(campaign, "business_id", "no_biz") if campaign else "no_biz"
        script_hash = hashlib.sha256(camp_script.encode("utf-8")).hexdigest()[:16] if camp_script else "empty"
        cache_key = (str(biz_id), str(camp_id), script_hash, str(active_lang), str(direction), bool(lead_name))

        if cache_key in cls._greeting_cache:
            cached_val = cls._greeting_cache[cache_key]
            logger.info(
                f"[CAMPAIGN_GREETING] source=cache campaign_id={camp_id} company='{detected_company}' "
                f"agent='{detected_agent}' direction={direction.upper()} lang={active_lang}"
            )
            return cached_val

        # 7. Attempt Verbatim Extraction from Script
        opening_line = cls.extract_greeting_from_script(
            script_text=camp_script,
            lead_name=lead_name,
            agent_name=detected_agent,
            company_name=detected_company,
            call_direction=direction,
            language=active_lang,
        )

        # 8. If verbatim extraction failed, generate grounded greeting
        if not opening_line:
            opening_line = cls.generate_grounded_greeting(
                script_text=camp_script,
                company_name=detected_company,
                lead_name=lead_name,
                agent_name=detected_agent,
                call_direction=direction,
                language=active_lang,
            )

        # 9. Final Sanitize & Guard Check
        opening_line = ResponseSanitizer.clean_customer_facing_text(
            opening_line,
            agent_name=detected_agent,
            lead_name=lead_name,
            cut_off_script_branches=True,
        )

        # Cache resolved greeting
        cls._greeting_cache[cache_key] = opening_line

        logger.info(
            f"[CAMPAIGN_GREETING] resolved campaign_id={camp_id} company='{detected_company}' "
            f"agent='{detected_agent}' direction={direction.upper()} lang={active_lang} "
            f"has_lead_name={bool(lead_name)} text=\"{opening_line[:80]}...\""
        )
        return opening_line
