"""
Module 10/24 — Excel/CSV lead import (Pandas + OpenPyXL, TS-024).

Accepts messy real-world spreadsheets: column names are matched
case-insensitively against a set of known aliases, so "Phone", "phone",
"Phone Number", and "Mobile" all map to the same Lead field.
"""
from __future__ import annotations

import io
from typing import Any

import pandas as pd

COLUMN_ALIASES: dict[str, list[str]] = {
    "first_name": ["first name", "firstname", "first", "first_name", "given name", "customer first name", "fname", "lead first name"],
    "last_name": ["last name", "lastname", "last", "surname", "last_name", "customer last name", "lname", "lead last name"],
    "name": ["full name", "lead name", "customer name", "fullname", "leadname", "customername", "name", "contact name", "customer", "lead", "client name", "client_name"],
    "phone": ["phone", "phone number", "mobile", "mobile number", "contact", "contact number", "cell", "telephone", "whatsapp", "number", "mobile_number", "phone_number", "phone no", "mobile no", "contact no", "cust phone", "customer phone", "num", "mob", "phonenumber", "phone_no", "mobile_no", "contact_no", "ph", "ph no", "ph_no"],
    "mobile_number": ["mobile number", "mobile", "phone", "phone number", "contact number", "mobile_number", "mob", "phone no", "mobile no"],
    "alternate_number": ["alternate number", "secondary contact number", "alternate phone", "alternate_number"],
    "email": ["email", "email address", "e-mail", "mail", "emailid", "email_id", "email_address", "cust email", "customer email", "email no", "mail id", "mail_id"],
    "company": ["company", "company name", "organization", "business name", "client name", "client", "business", "company_name", "client_company", "org", "firm", "company/organization", "company / organization"],
    "job_title": ["job title", "title", "designation", "role", "position", "job_title"],
    "requirement": ["requirement", "requirements", "notes", "message", "interest", "product interest", "inquiry", "description", "remarks"],
    "budget": ["budget", "budget range", "monthly budget", "amount"],
    "timeline": ["timeline", "timeframe", "schedule", "when"],
    "location": ["place", "location", "city", "town", "district", "address", "state", "country", "city location", "citylocation", "city / location", "city/location", "location/city", "place/location", "geo", "area", "region"],
    "address": ["address", "full address", "street address"],
    "pincode": ["pincode", "area pincode", "zipcode", "zip", "postal code"],
}


def _normalize_columns(columns: list[str]) -> dict[str, str]:
    """Returns {actual_column_name: canonical_field_name} for recognized columns."""
    mapping: dict[str, str] = {}
    for col in columns:
        col_str = str(col).strip()
        key = col_str.lower().replace("_", " ").replace("-", " ")
        key_clean = "".join(c for c in key if c.isalnum() or c.isspace()).strip()

        matched = False
        for field, aliases in COLUMN_ALIASES.items():
            for alias in aliases:
                alias_clean = "".join(c for c in alias if c.isalnum() or c.isspace()).strip()
                if key == alias or key_clean == alias_clean:
                    mapping[col] = field
                    matched = True
                    break
            if matched:
                break
    return mapping


def normalize_phone_number(phone: Any) -> str:
    if phone is None:
        return ""
    p_str = str(phone).strip()
    if not p_str or p_str.lower() == "nan":
        return ""
    # Strip Excel float representation e.g. 9876543210.0 -> 9876543210
    if p_str.endswith(".0"):
        p_str = p_str[:-2]
    has_plus = p_str.startswith("+")
    cleaned = "".join(c for c in p_str if c.isdigit())
    if not cleaned:
        return ""
    return ("+" + cleaned) if has_plus else cleaned


def parse_leads_file(content: bytes, filename: str) -> list[dict[str, Any]]:
    """Returns a list of normalized lead dicts ready to pass into the Lead model."""
    lower = filename.lower()
    if lower.endswith(".csv"):
        # Try UTF-8 first, fallback to latin-1
        try:
            df = pd.read_csv(io.BytesIO(content), dtype=str, encoding="utf-8").fillna("")
        except UnicodeDecodeError:
            df = pd.read_csv(io.BytesIO(content), dtype=str, encoding="latin-1").fillna("")
    else:
        df = pd.read_excel(io.BytesIO(content), dtype=str).fillna("")

    col_map = _normalize_columns(list(df.columns))
    rows: list[dict[str, Any]] = []

    for idx, row in df.iterrows():
        record: dict[str, Any] = {}
        extra_info = []

        for col in df.columns:
            val = str(row[col]).strip()
            if not val or val.lower() == "nan":
                continue

            field = col_map.get(col)
            if field:
                if field not in record:  # Keep first matched column
                    record[field] = val
            else:
                extra_info.append(f"{col}: {val}")

        first_name = record.get("first_name", "").strip()
        last_name = record.get("last_name", "").strip()
        full_name = record.get("name", "").strip()

        if not first_name and not last_name and full_name:
            parts = full_name.split(" ", 1)
            first_name = parts[0]
            last_name = parts[1] if len(parts) > 1 else ""
        elif first_name and not last_name:
            parts = first_name.split(" ", 1)
            if len(parts) > 1:
                first_name = parts[0]
                last_name = parts[1]

        if first_name and last_name and first_name.lower().endswith(last_name.lower()):
            first_name = first_name[:len(first_name) - len(last_name)].strip()

        record["first_name"] = first_name
        record["last_name"] = last_name
        if "name" in record:
            del record["name"]

        raw_phone = record.get("phone") or record.get("mobile_number") or ""
        if raw_phone:
            record["phone"] = normalize_phone_number(raw_phone)
            record["mobile_number"] = record["phone"]

        if not record.get("location") and record.get("place"):
            record["location"] = record.get("place")
        elif not record.get("place") and record.get("location"):
            record["place"] = record.get("location")

        if extra_info:
            existing_req = record.get("requirement", "") or record.get("remarks", "")
            req_append = "; ".join(extra_info)
            formatted = f"{existing_req} | {req_append}".strip(" |") if existing_req else req_append
            record["remarks"] = formatted
            record["requirement"] = formatted

        if not record.get("requirement"):
            record["requirement"] = "Interested in campaign offering"

        record["_row_number"] = idx + 2
        if record and any(v for k, v in record.items() if k != "_row_number"):
            rows.append(record)

    return rows

