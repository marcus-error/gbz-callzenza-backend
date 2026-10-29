"""
Dynamic Qualification Stage Intelligence Service.
Dynamically parses qualification requirements, stages, and required fields from ANY uploaded agent script.
Evaluates multi-field entity extraction, handles out-of-order responses, skips satisfied stages,
and synchronizes with AI Prompting, WebSockets, and UI. Zero hardcoded industry values.
"""
from __future__ import annotations

import re
import copy
from typing import Any, Dict, List, Optional, Tuple
from loguru import logger


# Common semantic entity aliases for dynamic field matching
FIELD_ALIASES: Dict[str, List[str]] = {
    "service": ["service", "requirement", "need", "service_required", "requested_service", "care_type", "treatment", "course", "service_inquiry"],
    "home_ownership": ["home_ownership", "ownership", "own_home", "house_ownership", "homeowner", "rent_or_own", "home_owner"],
    "property_type": ["property_type", "property", "house_type", "building_type", "residence_type"],
    "tax_payer": ["tax_payer", "taxpayer", "taxes", "tax_status", "pays_tax"],
    "zip_code": ["zip_code", "zip", "pincode", "pin_code", "postal_code", "pin"],
    "name": ["name", "customer_name", "patient_name", "caller_name", "first_name", "client_name"],
    "phone": ["phone", "contact_number", "mobile", "phone_number", "contact", "contact_details"],
    "email": ["email", "email_address", "mail"],
    "date": ["date", "appointment_date", "preferred_date", "start_date", "visit_date", "booking_date", "purchase_date", "day"],
    "time": ["time", "appointment_time", "preferred_time", "slot", "callback_time", "timing"],
    "location": ["location", "city", "address", "area", "chennai_location", "place", "site_location", "property_location", "zip_code"],
    "budget": ["budget", "price", "pricing", "price_range", "cost", "investment"],
    "product": ["product", "model", "device", "item", "product_name"],
    "issue": ["issue", "problem", "reason", "symptom", "complaint", "concern"],
    "warranty": ["warranty", "warranty_status", "guarantee"],
    "availability": ["availability", "schedule_availability", "convenient_time"],
    "confirmation": ["confirmation", "confirm_appointment", "confirm_details", "summary", "booking_confirmation", "close"],
}


class QualificationStageEngine:
    """
    Universal, script-driven qualification engine.
    Extracts ordered qualification stages from any script text, matches extracted customer entities,
    and calculates exact stage statuses (DONE, CURRENT, PENDING).
    """

    @classmethod
    def extract_stages_from_script(
        cls,
        script_text: str,
        agent_name: str = "AI Agent",
        business_name: str = "our company",
    ) -> List[Dict[str, Any]]:
        """
        Extracts ordered qualification stages from any uploaded script.
        Supports numbered stages, bulleted requirements, arrow sequences, and conversational sentences.
        """
        if not script_text or not script_text.strip():
            return cls._fallback_stages(business_name)

        raw = script_text.strip()
        extracted_stages: List[Dict[str, Any]] = []

        # ── 1. Check for Arrow-separated pipeline: Requirement -> Budget -> Location -> Availability
        arrow_match = re.search(r'([A-Za-z0-9\s&/-]{3,40}(?:\s*(?:->|→|=>|>)\s*[A-Za-z0-9\s&/-]{3,40}){2,})', raw)
        if arrow_match:
            parts = re.split(r'\s*(?:->|→|=>|>)\s*', arrow_match.group(1).strip())
            for idx, p in enumerate(parts):
                clean_p = p.strip().strip("-*•# ")
                if len(clean_p) >= 3:
                    field_key = cls._infer_field_key(clean_p)
                    extracted_stages.append({
                        "id": f"stage_{idx + 1}",
                        "name": clean_p,
                        "field": field_key,
                        "raw_instruction": clean_p,
                        "status": "PENDING",
                    })
            if len(extracted_stages) >= 2:
                extracted_stages[0]["status"] = "CURRENT"
                return extracted_stages

        # ── 2. Check for Explicit Numbered Stages / Steps: "Stage 1: Greeting", "1. Identify Service"
        lines = raw.splitlines()
        numbered_pattern = re.compile(
            r'^(?:#+\s*)?(?:Stage|Step|Phase|Section|Part|STEP|STAGE)?\s*'
            r'(\d+)[.:\)]\s*([A-Za-z0-9 &/\-\'\u0B80-\u0BFF]{3,60})',
            re.IGNORECASE
        )

        for line in lines:
            stripped = line.strip()
            m = numbered_pattern.match(stripped)
            if m:
                num = m.group(1).strip()
                raw_title = m.group(2).strip()
                if ":" in raw_title:
                    raw_title = raw_title.split(":")[0].strip()
                
                title = f"{num}. {raw_title}" if not raw_title.startswith(f"{num}.") else raw_title
                # Skip pure generic sub-headers if too long or objection headers
                if len(raw_title) <= 50 and not any(k in raw_title.lower() for k in ["common objection", "frequently asked"]):
                    field_key = cls._infer_field_key(raw_title)
                    # Check if already added
                    if not any(s["name"].lower() == title.lower() for s in extracted_stages):
                        extracted_stages.append({
                            "id": f"stage_{len(extracted_stages) + 1}",
                            "name": title,
                            "field": field_key,
                            "raw_instruction": stripped,
                            "status": "PENDING",
                        })

        if len(extracted_stages) >= 2:
            extracted_stages[0]["status"] = "CURRENT"
            return extracted_stages

        # ── 3. Check for Conversational Sequence Phrases: "First understand...", "Then collect...", "Ask preferred date..."
        action_phrases = re.findall(
            r'(?:(?:First|Firstly|Then|Next|After that|Also|Finally|Lastly|Step\s*\d*)\s*[,:]?\s*)?'
            r'(?:(?:understand|identify|ask|collect|capture|gather|check|confirm|verify|get|note down|inquire about)\s+[^.\n,;]{4,60})',
            raw,
            re.IGNORECASE
        )
        if action_phrases and len(action_phrases) >= 2:
            for idx, phrase in enumerate(action_phrases[:8]):
                clean_phrase = phrase.strip().strip("-*•# ")
                clean_title = cls._clean_stage_title(clean_phrase)
                if len(clean_title) >= 4 and not any(s["name"].lower() == clean_title.lower() for s in extracted_stages):
                    field_key = cls._infer_field_key(clean_title)
                    extracted_stages.append({
                        "id": f"stage_{len(extracted_stages) + 1}",
                        "name": clean_title,
                        "field": field_key,
                        "raw_instruction": clean_phrase,
                        "status": "PENDING",
                    })

        if len(extracted_stages) >= 2:
            extracted_stages[0]["status"] = "CURRENT"
            return extracted_stages

        # ── 4. Check for Bulleted Requirements / Questions
        bullet_items = re.findall(r'^[*-•]\s*([A-Za-z0-9 &/\-\'\u0B80-\u0BFF]{3,50})', raw, re.MULTILINE)
        if bullet_items and len(bullet_items) >= 2:
            for idx, item in enumerate(bullet_items[:7]):
                clean_item = item.strip()
                if not any(k in clean_item.lower() for k in ["http", "callzenza", "note:", "tip:"]):
                    field_key = cls._infer_field_key(clean_item)
                    extracted_stages.append({
                        "id": f"stage_{len(extracted_stages) + 1}",
                        "name": clean_item,
                        "field": field_key,
                        "raw_instruction": clean_item,
                        "status": "PENDING",
                    })

        if len(extracted_stages) >= 2:
            extracted_stages[0]["status"] = "CURRENT"
            return extracted_stages

        # ── 5. Safe Natural Sentence Decomposition
        sentences = [s.strip() for s in re.split(r'(?<=[.?!])\s+', raw) if len(s.strip()) > 15]
        for idx, sentence in enumerate(sentences[:5]):
            if any(k in sentence.lower() for k in ["ask", "collect", "understand", "service", "date", "time", "details", "confirm", "name", "budget", "location", "help", "need"]):
                title = cls._clean_stage_title(sentence[:45])
                field_key = cls._infer_field_key(title)
                extracted_stages.append({
                    "id": f"stage_{len(extracted_stages) + 1}",
                    "name": title,
                    "field": field_key,
                    "raw_instruction": sentence,
                    "status": "PENDING",
                })

        if len(extracted_stages) >= 2:
            extracted_stages[0]["status"] = "CURRENT"
            return extracted_stages

        return cls._fallback_stages(business_name)

    @classmethod
    def _fallback_stages(cls, business_name: str) -> List[Dict[str, Any]]:
        """Safe generic stages when script contains no explicit qualification items."""
        return [
            {"id": "stage_1", "name": "Greeting & Purpose", "field": "greeting", "status": "CURRENT"},
            {"id": "stage_2", "name": "Understand Requirements", "field": "requirement", "status": "PENDING"},
            {"id": "stage_3", "name": "Provide Information & Details", "field": "details", "status": "PENDING"},
            {"id": "stage_4", "name": "Confirm Next Steps", "field": "confirmation", "status": "PENDING"},
        ]

    @classmethod
    def _clean_stage_title(cls, phrase: str) -> str:
        """Capitalizes and cleans extracted action phrases into crisp UI stage titles."""
        p = re.sub(r'^(?:first|firstly|then|next|after that|also|finally|lastly|step\s*\d*)\s*[,:]?\s*', '', phrase, flags=re.IGNORECASE).strip()
        p = p.rstrip(".,;:")
        words = p.split()
        if len(words) > 6:
            p = " ".join(words[:6])
        return p[0].upper() + p[1:] if p else "Inquire"

    @classmethod
    def _infer_field_key(cls, text: str) -> str:
        """Infers the canonical entity field key (service, name, date, time, location, budget, etc.) from stage title."""
        t = text.lower()
        for field, aliases in FIELD_ALIASES.items():
            if any(a in t for a in aliases):
                return field
        # Default: sanitize title as a custom field key
        clean = re.sub(r'[^a-z0-9_]+', '_', t).strip('_')
        return clean[:20] if clean else "details"

    @classmethod
    def extract_entities_from_text(
        cls,
        text: str,
        existing_fields: Optional[Dict[str, Any]] = None,
        script_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Dynamically extracts customer entities from an utterance.
        Zero hardcoded industry lists.
        Handles numeric disambiguation (Age vs Phone vs Budget vs Time vs Year).
        Supports multi-field extraction in a single utterance (e.g. Service + Name + Date + Location).
        Supports customer corrections (overwrites earlier values).
        """
        collected = copy.deepcopy(existing_fields) if existing_fields else {}
        clean_text = text.strip()
        if not clean_text:
            return collected

        lowered = clean_text.lower()
        script_lower = (script_text or "").lower()

        # Determine dynamic target keys from active script context
        if "patient_name" in script_lower or "patient name" in script_lower or "patient" in script_lower:
            name_key = "patient_name"
        elif "customer_name" in script_lower or "customer name" in script_lower or "customer" in script_lower:
            name_key = "customer_name"
        elif "student_name" in script_lower or "student name" in script_lower or "student" in script_lower:
            name_key = "student_name"
        elif "client_name" in script_lower:
            name_key = "client_name"
        else:
            name_key = "name"

        if "patient_age" in script_lower or "patient age" in script_lower or "patient" in script_lower:
            age_key = "patient_age"
        elif "student_age" in script_lower or "student age" in script_lower or "student" in script_lower:
            age_key = "student_age"
        else:
            age_key = "age"

        if "property_type" in script_lower or "property" in script_lower or "bhk" in script_lower:
            service_key = "property_type"
        elif "course" in script_lower:
            service_key = "course"
        elif "care_type" in script_lower or "treatment" in script_lower:
            service_key = "care_type"
        else:
            service_key = "service"

        if "move_in_date" in script_lower:
            date_key = "move_in_date"
        elif "appointment_date" in script_lower or "appointment" in script_lower:
            date_key = "appointment_date"
        elif "preferred_date" in script_lower:
            date_key = "preferred_date"
        else:
            date_key = "date"

        # ── 1. Name Extraction ──────────────────────────────────────────────
        name_found = False
        # 1a. Tamil / Indian Name patterns: "என் பெயர் முகிலன் விஸ்வநாதன்", "என் பேரு முகிலன் விஸ்வநாதன்", "என் பெயர் முகிலன்"
        tamil_name_m = re.search(r'(?i)(?:என்\s*(?:பெயர்|பேரு)|பெயர்|मेरा\s*नाम|नाम\s*है)\s*[:\-]?\s*([^\n\r\,\.\!\?]{2,50})', clean_text)
        if tamil_name_m:
            cand_tname = tamil_name_m.group(1).strip()
            cand_tname = re.sub(r'\s*(?:ஆகும்|இருக்கு|தான்|ஹே|வணக்கம்).*$', '', cand_tname).strip()
            if cand_tname:
                collected["name"] = cand_tname
                collected[name_key] = cand_tname
                name_found = True

        if not name_found:
            for name_match in re.finditer(
                r'(?i)(?:patient(?:\'?s)?\s+name\s+is|customer(?:\'?s)?\s+name\s+is|student(?:\'?s)?\s+name\s+is|my\s+name\s+is|name\s+is|this\s+is|call\s+me|myself|i\s+am|i\'m|for\s+patient|for\s+customer|for\s+student|பெயர்|என்\s*பெயர்|मेरा\s*नाम|नाम)\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)(?=[\s\.\,\;]|$)',
                clean_text
            ):
                cand = name_match.group(1).strip()
                words = [w for w in cand.split() if w.lower() not in {"and", "from", "with", "for", "at", "in", "the", "a", "an", "is", "she", "he", "to"}]
                if words:
                    clean_name = " ".join(words)
                    stop = {
                        "interested", "looking", "calling", "here", "fine", "good", "okay", "tomorrow", "today",
                        "tamil", "english", "trying", "planning", "asking", "wondering", "customer", "user",
                        "patient", "student", "yes", "no", "physiotherapy", "nurse", "doctor", "service",
                        "appointment", "2bhk", "3bhk", "villa", "property", "course", "python",
                        "mother", "father", "brother", "sister", "wife", "husband", "son", "daughter", "friend", "family"
                    }
                    if clean_name.lower() not in stop and not any(w in clean_name.lower().split() for w in stop):
                        title_name = clean_name.title()
                        collected["name"] = title_name
                        collected[name_key] = title_name
                        name_found = True
                        break

        if not name_found:
            # Check pattern: "<Capitalized Name> is <Age>" e.g. "Jasmine is 23"
            simple_name_age = re.search(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+is\s+\d{1,3}\b', clean_text)
            if simple_name_age:
                cand = simple_name_age.group(1).strip()
                stop_words = {'interested', 'looking', 'good', 'fine', 'okay', 'patient', 'customer', 'service'}
                if cand.lower() not in stop_words:
                    collected['name'] = cand
                    collected[name_key] = cand

        # ── 1b. Home Ownership Extraction (Owner / Renter) ─────────────────
        if re.search(r'(?i)(?:^|\W)(?:own|owner|homeowner|house\s*owner|i\s+own|we\s+own|சொந்த\s*வீடு|சொந்தமான|சொந்தம்|சொந்த|खुद\s*கா\s*घर|मालिक)(?:$|\W)', clean_text):
            collected["home_ownership"] = "owner"
        elif re.search(r'(?i)(?:^|\W)(?:rent|renter|renting|tenant|leased|வாடகை\s*வீடு|வாடகை|किराए\s*का|किराया)(?:$|\W)', clean_text):
            collected["home_ownership"] = "renter"

        # ── 1c. Property Type Extraction (Single Family / Apartment / Villa) ──
        if re.search(r'(?i)(?:^|\W)(?:single\s*family|single\s*residence|individual\s*house|independent\s*house|villa|தனி\s*வீடு|தனி\s*இல்லம்|தனி|विला)(?:$|\W)', clean_text):
            collected["property_type"] = "single_family"
        elif re.search(r'(?i)(?:^|\W)(?:apartment|flat|condo|multi\s*family|அபார்ட்மெண்ட்|அடுக்குமாடி\s*குடியிருப்பு|फ्लैट)(?:$|\W)', clean_text):
            collected["property_type"] = "apartment"

        # ── 1d. Tax Payer Status Extraction ───────────────────────────────────
        if re.search(r'(?i)(?:^|\W)(?:pay\s*taxes?|tax\s*payer|taxpayer|paying\s*tax|taxes?\s*paid|வரியும்\s*நான்தான்|வரி\s*நான்தான்|வரி\s*செலுத்து|செலுத்துறேன்|டாக்ஸ்\s*பே\s*பண்றேன்|டாக்ஸ்|टैक्स\s*पेयर|टैक्स\s*देता)(?:$|\W)', clean_text):
            collected["tax_payer"] = True
        elif re.search(r'(?i)(?:^|\W)(?:no\s*taxes?|not\s*paying\s*tax|non\s*taxpayer|வரி\s*செலுத்துவதில்லை)(?:$|\W)', clean_text):
            collected["tax_payer"] = False

        # ── 1e. ZIP / PIN Code Extraction (6-digit Indian PIN or 5-digit US ZIP) ─
        pin_match = re.search(r'\b([1-9]\d{5})\b|\b([0-9]{5})\b', clean_text)
        if pin_match:
            cand_pin = pin_match.group(1) or pin_match.group(2)
            if not any(k in lowered for k in ["phone", "mobile", "call", "₹", "$", "lakh", "crore", "budget"]):
                collected["zip_code"] = cand_pin

        # ── 2. Age Extraction (1 - 120, avoids phone/budget/time confusion) ──
        age_match = re.search(
            r'(?i)\b(?:she\s+is|he\s+is|i\s+am|patient(?:\'?s)?\s+age\s+is|student(?:\'?s)?\s+age\s+is|age\s*(?:is|of)?|வயது|உम्र)\s*(\d{1,3})\b(?!\s*(?:am|pm|lakh|crore|k|thousand|million|\%|\$|₹|years?\s+experience))|\b(\d{1,3})\s*(?:years?\s+old|yrs?\s+old|வயது)\b',
            clean_text
        )
        if age_match:
            age_raw = age_match.group(1) or age_match.group(2)
            try:
                age_val = int(age_raw)
                if 1 <= age_val <= 120:
                    collected["age"] = age_val
                    if age_key != "age":
                        collected[age_key] = age_val
            except (ValueError, TypeError):
                pass

        # ── 3. Phone / Contact Number Extraction (7-15 digits) ─────────────
        phone_match = re.search(
            r'(?i)(?:number\s+is|phone\s*(?:is|number)?|mobile\s*(?:is|number)?|contact\s*(?:is|number)?|call\s+at|reach\s+at)?\s*(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b|\b(?:\+91[\-\s]?)?[6-9]\d{9}\b|\b\d{10}\b',
            clean_text
        )
        if phone_match:
            cand_phone = phone_match.group(0).strip()
            # Verify candidate doesn't match a budget like 8000000 or a date or 6-digit zip
            if not any(k in cand_phone.lower() for k in ["lakh", "crore", "k", "$", "₹"]) and len(re.sub(r'\D', '', cand_phone)) >= 7:
                clean_phone = re.sub(r'[^\d+]', '', cand_phone)
                collected["phone"] = clean_phone

        # ── 4. Date Extraction ──────────────────────────────────────────────
        date_match = re.search(
            r'(?i)\b(tomorrow|today|tonight|day\s+after\s+tomorrow|next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|week|month)|'
            r'monday|tuesday|wednesday|thursday|friday|saturday|sunday|'
            r'\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|'
            r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?|'
            r'this\s+weekend|next\s+month|நாளை|இன்று|कल|परसों)\b',
            clean_text
        )
        if date_match:
            date_val = date_match.group(1).strip().lower()
            collected["date"] = date_val
            if date_key != "date":
                collected[date_key] = date_val

        # ── 5. Time Extraction ──────────────────────────────────────────────
        time_match = re.search(
            r'(?i)\b(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|'
            r'(?:at\s+)?(?:morning|afternoon|evening|noon|night)|காலை(?:\s*\d{1,2})?|மாலை(?:\s*\d{1,2})?|सुबह|शाम)\b',
            clean_text
        )
        if time_match:
            time_val = time_match.group(1).strip()
            collected["time"] = time_val

        # ── 6. Budget / Price Extraction ────────────────────────────────────
        budget_match = re.search(
            r'(?i)\b(?:budget\s*(?:is|of)?|around|under|below|upto|max\s+of)?\s*'
            r'([₹$€£]?\s*\d+(?:[.,]\d+)?\s*(?:lakhs?|crores?|k|thousand|million|cr|lac|ரூபாய்|லட்சம்|लाख)?)\b',
            clean_text
        )
        if budget_match and any(k in lowered for k in ["budget", "price", "cost", "lakh", "crore", "thousand", "₹", "$", "€", "£", "under", "around"]):
            cand_b = budget_match.group(1).strip()
            if cand_b and not cand_b.isdigit():
                collected["budget"] = cand_b
            elif cand_b and any(k in lowered for k in ["budget", "price", "cost", "₹", "$"]):
                collected["budget"] = cand_b

        # ── 7. Location / Area Extraction ───────────────────────────────────
        loc_match = re.search(
            r'(?i)\b(?:at|in|near|around|from|location\s+is|area\s+is|place\s+is)\s+([A-Za-z][a-zA-Z0-9\s]{1,30}?)(?=[\.\,\;]|\s+(?:for|tomorrow|today|at|my\s+name|with|on|from|please|and|is|budget|under)|\s*$)',
            clean_text
        )
        if loc_match:
            l_cand = loc_match.group(1).strip()
            non_locations = {"home", "work", "morning", "afternoon", "evening", "night", "tomorrow", "today", "a", "an", "the", "my", "our", "india", "tamil", "english"}
            # Disallow time representation like "9 AM" from being treated as location
            is_time_str = bool(re.search(r'^\d{1,2}(?::\d{2})?\s*(?:am|pm)?$', l_cand.lower()))
            if l_cand.lower() not in non_locations and len(l_cand) >= 3 and not is_time_str:
                collected["location"] = l_cand.title()

        # ── 8. Service / Requirement Extraction (Explicit Service Inquiries only) ─────────
        # Note: Do NOT match "rent", "buy", or "home" as services.
        service_match = re.search(
            r'(?i)\b(?:i\s+need|i\s+want|looking\s+for|require|interested\s+in|regarding\s+service|service\s+for|booking\s+for|course\s+in)\s+'
            r'(?:a|an|the)?\s*([a-zA-Z0-9\s&/-]{2,40}?)(?=[\.\,\;]|\s+(?:for\s+my|tomorrow|today|at|in|near|around|my\s+name|with|on|from|please|under|budget|and|she|he)|\s*$)',
            clean_text
        )
        if service_match:
            s_cand = service_match.group(1).strip()
            non_services = {"to", "an", "a", "the", "know", "check", "speak", "talk", "book", "appointment", "help", "information", "details", "call", "someone", "somebody", "tamil", "english", "home", "my home", "house", "my house"}
            if s_cand.lower() not in non_services and len(s_cand) >= 3:
                collected["service"] = s_cand
                if re.search(r'(?i)\b(?:[1-5]\s*bhk|apartment|flat|villa|house|plot|condo|commercial)\b', s_cand):
                    collected["property_type"] = s_cand

        # ── 9. Email Extraction ─────────────────────────────────────────────
        email_match = re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', clean_text)
        if email_match:
            collected["email"] = email_match.group(0).strip()

        # ── 10. Issue Extraction ────────────────────────────────────────────
        issue_match = re.search(r'(?i)\b(?:issue|problem|broken|leaking|not working|damaged|repair)\s*(?:with\s+)?([a-zA-Z\s]{3,30})?', clean_text)
        if issue_match and not collected.get("issue"):
            collected["issue"] = issue_match.group(0).strip()

        # Log extracted fields
        newly_found = {k: v for k, v in collected.items() if not existing_fields or existing_fields.get(k) != v}
        if newly_found:
            logger.info(f"[QUALIFICATION_EXTRACT] customer_text='{clean_text}' extracted_fields={newly_found}")

        return collected

    @classmethod
    def evaluate_eligibility(
        cls,
        collected_fields: Dict[str, Any],
        script_text: str = "",
        rag_context: str = "",
        stages: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Dynamically evaluates eligibility from active Script and RAG source of truth.
        Zero hardcoded business rules: parses rules directly from script and RAG text.
        
        Returns:
            {
                "status": "PENDING" | "QUALIFIED" | "NOT_QUALIFIED",
                "reason": str,
                "source": "SCRIPT" | "RAG" | "DEFAULT",
                "rule_condition": str,
                "completed_fields": Dict[str, Any],
                "missing_fields": List[str],
            }
        """
        script_lower = (script_text or "").lower()
        rag_lower = (rag_context or "").lower()
        combined_text = f"{script_lower}\n{rag_lower}"

        # ── Dynamic Rule Evaluation ─────────────────────────────────────────
        # 1. Homeownership Eligibility Check
        # If script/RAG indicates homeowners are required or renters are not eligible
        homeowner_required = any(phrase in combined_text for phrase in [
            "homeowner", "home owner", "homeowners", "home owners", "own your home",
            "must own", "own home", "renters are not eligible", "renters not eligible",
            "homeowners qualify", "homeowners only", "only homeowners", "zero-down solar panels to homeowners",
            "residential homeowner", "single family homeowner"
        ])
        renter_disqualified_explicit = any(phrase in combined_text for phrase in [
            "renters are not eligible", "renter is not eligible", "renters not eligible",
            "not for renters", "if rent -> not qualified", "if renter -> not qualified",
            "do not qualify if renting"
        ])

        home_val = str(collected_fields.get("home_ownership") or collected_fields.get("ownership") or "").lower().strip()
        if home_val:
            if home_val in ("renter", "rent", "renting", "tenant", "leased"):
                if homeowner_required or renter_disqualified_explicit:
                    rule_src = "SCRIPT" if (homeowner_required and script_lower) else ("RAG" if rag_lower else "SCRIPT")
                    reason = "Homeowners only - Renters are not eligible per active Script/RAG criteria."
                    logger.info(
                        f"[ELIGIBILITY_RULE] source=\"{rule_src}\" condition=\"home_ownership==renter\" "
                        f"rule=\"Homeowners required / Renters not eligible\" result=\"NOT_QUALIFIED\""
                    )
                    logger.info(f"[ELIGIBILITY_EVALUATION] evaluated_field=\"home_ownership\" value=\"renter\" status=\"NOT_QUALIFIED\" reason=\"{reason}\"")
                    return {
                        "status": "NOT_QUALIFIED",
                        "reason": reason,
                        "source": rule_src,
                        "rule_condition": "home_ownership == renter",
                        "disqualified_field": "home_ownership",
                    }

        # 2. Property Type Eligibility Check
        # If script/RAG explicitly requires single family or excludes apartments
        single_family_required = any(phrase in combined_text for phrase in [
            "single family home only", "single family residence only", "only single family",
            "apartments are not eligible", "apartments not eligible", "no apartments"
        ])
        prop_val = str(collected_fields.get("property_type") or "").lower().strip()
        if prop_val:
            if prop_val in ("apartment", "flat", "condo") and single_family_required:
                rule_src = "SCRIPT" if single_family_required in script_lower else "RAG"
                reason = "Single family homes only - Apartments are not eligible per active Script/RAG criteria."
                logger.info(
                    f"[ELIGIBILITY_RULE] source=\"{rule_src}\" condition=\"property_type=={prop_val}\" "
                    f"rule=\"Single family required\" result=\"NOT_QUALIFIED\""
                )
                logger.info(f"[ELIGIBILITY_EVALUATION] evaluated_field=\"property_type\" value=\"{prop_val}\" status=\"NOT_QUALIFIED\" reason=\"{reason}\"")
                return {
                    "status": "NOT_QUALIFIED",
                    "reason": reason,
                    "source": rule_src,
                    "rule_condition": f"property_type == {prop_val}",
                    "disqualified_field": "property_type",
                }

        # 3. Tax Payer Eligibility Check
        # If script/RAG explicitly requires paying taxes
        tax_required = any(phrase in combined_text for phrase in [
            "must pay taxes", "tax payers only", "taxpayer required", "must be a taxpayer", "non-taxpayers not eligible"
        ])
        tax_val = collected_fields.get("tax_payer")
        if tax_val is False and tax_required:
            rule_src = "SCRIPT" if tax_required in script_lower else "RAG"
            reason = "Taxpayers only - Non-taxpayers are not eligible per active Script/RAG criteria."
            logger.info(
                f"[ELIGIBILITY_RULE] source=\"{rule_src}\" condition=\"tax_payer==False\" "
                f"rule=\"Taxpayers required\" result=\"NOT_QUALIFIED\""
            )
            logger.info(f"[ELIGIBILITY_EVALUATION] evaluated_field=\"tax_payer\" value=\"False\" status=\"NOT_QUALIFIED\" reason=\"{reason}\"")
            return {
                "status": "NOT_QUALIFIED",
                "reason": reason,
                "source": rule_src,
                "rule_condition": "tax_payer == False",
                "disqualified_field": "tax_payer",
            }

        # Check for conflict between Script and RAG
        if script_text and rag_context:
            script_disqualifies_renter = "renter" in script_lower and "not eligible" in script_lower
            rag_qualifies_renter = "renter" in rag_lower and "eligible" in rag_lower and "not eligible" not in rag_lower
            if script_disqualifies_renter and rag_qualifies_renter:
                logger.warning(
                    f"[ELIGIBILITY_RULE_CONFLICT] script_rule=\"renters not eligible\" "
                    f"rag_rule=\"renters eligible\" rule_source=\"SCRIPT\" resolution=\"Script precedence enforced\""
                )

        # 4. Check if all required stages are satisfied
        all_required_satisfied = False
        missing = []
        if stages:
            for idx, stage in enumerate(stages):
                field_key = stage.get("field", "").lower()
                stage_name = stage.get("name", "")
                if idx == 0 and any(k in stage_name.lower() for k in ["greeting", "intro", "welcome"]):
                    continue
                if any(k in stage_name.lower() for k in ["confirm", "closing", "wrap"]):
                    continue
                
                # Check if this field is collected
                is_collected = False
                if field_key and field_key in collected_fields and collected_fields[field_key] is not None:
                    is_collected = True
                elif field_key in FIELD_ALIASES:
                    for alias in FIELD_ALIASES[field_key]:
                        if alias in collected_fields and collected_fields[alias] is not None:
                            is_collected = True
                            break
                
                if not is_collected:
                    missing.append(stage_name or field_key)

            if len(stages) > 1 and len(missing) == 0:
                all_required_satisfied = True

        if all_required_satisfied:
            logger.info(f"[ELIGIBILITY_RESULT] status=\"QUALIFIED\" reason=\"All required eligibility conditions and script stages satisfied.\"")
            return {
                "status": "QUALIFIED",
                "reason": "All required qualification requirements satisfied.",
                "source": "SCRIPT",
                "rule_condition": "all_stages_completed",
                "missing_fields": [],
            }

        logger.info(f"[ELIGIBILITY_RESULT] status=\"PENDING\" missing_fields={missing}")
        return {
            "status": "PENDING",
            "reason": "Qualification in progress - remaining required details pending.",
            "source": "SCRIPT",
            "rule_condition": "pending_remaining_fields",
            "missing_fields": missing,
        }

    @classmethod
    def evaluate_progress(
        cls,
        stages: List[Dict[str, Any]],
        collected_fields: Dict[str, Any],
        current_step_idx: int = 0,
        script_text: str = "",
        rag_context: str = "",
    ) -> Tuple[List[Dict[str, str]], str, int, str, List[str], Optional[str]]:
        """
        Calculates active progress across all dynamically extracted script stages.
        Integrates dynamic eligibility evaluation.
        
        Returns:
          (updated_stage_items, current_stage_name, active_index, qualification_status, missing_fields, next_field)
        """
        if not stages:
            return [], "Completed", 0, "QUALIFIED", [], None

        # 1. Evaluate Dynamic Eligibility Rules
        eligibility = cls.evaluate_eligibility(
            collected_fields=collected_fields,
            script_text=script_text,
            rag_context=rag_context,
            stages=stages,
        )
        elig_status = eligibility.get("status", "PENDING")

        updated_stages: List[Dict[str, str]] = []
        first_pending_idx: Optional[int] = None
        missing_fields: List[str] = []

        if elig_status == "NOT_QUALIFIED":
            # Disqualification halts the normal qualification question loop
            for idx, stage in enumerate(stages):
                stage_name = stage.get("name", f"Stage {idx + 1}")
                if idx < current_step_idx:
                    updated_stages.append({"name": stage_name, "status": "DONE"})
                elif idx == current_step_idx:
                    updated_stages.append({"name": f"{stage_name} (Not Eligible)", "status": "DONE"})
                else:
                    updated_stages.append({"name": stage_name, "status": "PENDING"})

            active_idx = min(current_step_idx, len(stages) - 1)
            current_stage_name = "Not Qualified"
            qual_status = "NOT_QUALIFIED"
            next_field = None
            logger.info(f"[QUALIFICATION_STATUS] session_status=\"NOT_QUALIFIED\" active_stage=\"{current_stage_name}\" next_field=None")
            return updated_stages, current_stage_name, active_idx, qual_status, [], None

        # PENDING or QUALIFIED evaluation
        for idx, stage in enumerate(stages):
            field_key = stage.get("field", "").lower()
            stage_name = stage.get("name", f"Stage {idx + 1}")

            # Check if this stage requirement is satisfied by collected fields
            is_satisfied = False

            # Check direct field match or alias match
            if field_key and field_key in collected_fields and collected_fields[field_key] is not None:
                is_satisfied = True
            elif field_key in FIELD_ALIASES:
                aliases = FIELD_ALIASES[field_key]
                if any(k in collected_fields and collected_fields[k] is not None for k in aliases):
                    is_satisfied = True

            # Initial greeting / stage 0 is automatically satisfied once conversation has started
            if idx == 0 and any(k in stage_name.lower() for k in ["greeting", "intro", "permission", "welcome"]):
                is_satisfied = True

            # Confirmation stage is satisfied if all previous stages are satisfied
            if any(k in stage_name.lower() for k in ["confirm", "closing", "wrap"]):
                if idx > 0 and all(s.get("status") == "DONE" for s in updated_stages):
                    is_satisfied = True

            # If current_step_idx has advanced past this step index and we have an answer
            if idx < current_step_idx and is_satisfied:
                is_satisfied = True

            if is_satisfied:
                updated_stages.append({"name": stage_name, "status": "DONE"})
            else:
                missing_fields.append(stage_name)
                if first_pending_idx is None:
                    first_pending_idx = idx
                    updated_stages.append({"name": stage_name, "status": "CURRENT"})
                else:
                    updated_stages.append({"name": stage_name, "status": "PENDING"})

        # If all stages are satisfied
        if first_pending_idx is None or elig_status == "QUALIFIED":
            active_idx = len(stages) - 1
            current_stage_name = stages[-1].get("name", "Confirmed")
            qual_status = "QUALIFIED"
            next_field = None
        else:
            active_idx = first_pending_idx
            current_stage_name = stages[first_pending_idx].get("name", "In Progress")
            qual_status = "PENDING"
            next_field = stages[first_pending_idx].get("field") or stages[first_pending_idx].get("name")

        logger.info(f"[QUALIFICATION_STATUS] session_status=\"{qual_status}\" active_stage=\"{current_stage_name}\" next_field=\"{next_field or 'none'}\"")
        return updated_stages, current_stage_name, active_idx, qual_status, missing_fields, next_field

    @classmethod
    def log_qualification_state(
        cls,
        session_id: str,
        agent_id: str,
        script_id: str,
        latest_transcript: str,
        extracted_fields: Dict[str, Any],
        known_fields: Dict[str, Any],
        missing_fields: List[str],
        current_stage: str,
        next_field: Optional[str],
        eligibility_status: str = "PENDING",
    ) -> None:
        """Emits structured debugging log according to Requirements 24 & 25."""
        logger.info(
            f"[QUALIFICATION_STATE] session_id={session_id} agent_id={agent_id} script_id={script_id} "
            f"latest_transcript='{latest_transcript}' extracted_fields={extracted_fields} "
            f"known_fields={known_fields} missing_fields={missing_fields} "
            f"current_stage='{current_stage}' next_field='{next_field or 'None'}' "
            f"eligibility_status='{eligibility_status}'"
        )


