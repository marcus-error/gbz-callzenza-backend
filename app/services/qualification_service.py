"""
Module 12 — AI Qualification Engine.

Configurable, weighted scoring (business owner sets weights + thresholds via
QualificationRule, module 12). Evaluates structured lead attributes, extracted
criteria, and dynamic campaign qualification rules with zero hardcoded business rules.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from app.db.models import Lead, QualificationRule


def _sub_score(lead: Lead, criterion: str, criteria_data: Optional[Dict[str, Any]] = None) -> float:
    """Returns 0.0–1.0 for a single qualification criterion."""
    c_lower = criterion.lower().strip()

    # 1. Direct evaluation from extracted criteria dictionary if provided
    if criteria_data and c_lower in criteria_data:
        val = criteria_data[c_lower]
        if isinstance(val, bool):
            return 1.0 if val else 0.0
        if isinstance(val, (int, float)):
            return min(max(float(val), 0.0), 1.0)
        if isinstance(val, dict):
            status = str(val.get("status", "")).lower()
            if status == "complete":
                return 1.0
            if status == "partial":
                return 0.5
            return 0.0
        if isinstance(val, str) and val.strip():
            return 1.0

    # 2. Direct evaluation from standard Lead object attributes
    val_attr = getattr(lead, c_lower, None)
    if val_attr and str(val_attr).strip():
        if c_lower == "icp":
            return 1.0 if lead.company else 0.3
        if c_lower == "decision_maker":
            title = (lead.job_title or "").lower()
            senior = any(k in title for k in ("founder", "ceo", "cto", "vp", "director", "head", "owner", "manager"))
            return 1.0 if senior else (0.4 if lead.job_title else 0.0)
        if c_lower == "requirement":
            length = len(lead.requirement or "")
            return min(length / 120, 1.0)
        return 1.0

    # 3. Fallback for legacy default criteria keys when attribute is empty
    if c_lower == "icp":
        return 1.0 if lead.company else 0.3
    if c_lower == "budget":
        return 1.0 if (lead.budget and lead.budget.strip()) else 0.0
    if c_lower == "timeline":
        return 1.0 if (lead.timeline and lead.timeline.strip()) else 0.0
    if c_lower == "requirement":
        length = len(lead.requirement or "")
        return min(length / 120, 1.0)
    if c_lower == "decision_maker":
        title = (lead.job_title or "").lower()
        senior = any(k in title for k in ("founder", "ceo", "cto", "vp", "director", "head", "owner", "manager"))
        return 1.0 if senior else (0.4 if lead.job_title else 0.0)

    # 4. Keyword presence search in lead.requirement summary text
    req_text = (lead.requirement or "").lower()
    if c_lower in req_text or f"{c_lower}:" in req_text:
        return 1.0

    return 0.0


def score_lead(lead: Lead, rule: QualificationRule, criteria_data: Optional[Dict[str, Any]] = None) -> tuple[int, str]:
    """Calculates weighted qualification score (0-100) and status (UNQUALIFIED, NURTURE, QUALIFIED)."""
    weights = rule.weights or {}
    if not weights:
        weights = {
            "icp": 30,
            "budget": 25,
            "timeline": 20,
            "requirement": 15,
            "decision_maker": 10,
        }
    total_weight = sum(weights.values()) or 1
    raw = sum(_sub_score(lead, criterion, criteria_data) * weight for criterion, weight in weights.items())
    score = round((raw / total_weight) * 100)
    score = max(0, min(100, score))

    if score <= rule.unqualified_max:
        status = "UNQUALIFIED"
    elif score <= rule.nurture_max:
        status = "NURTURE"
    else:
        status = "QUALIFIED"
    return score, status


def get_missing_criteria(lead: Lead, rule: QualificationRule, criteria_data: Optional[Dict[str, Any]] = None) -> List[str]:
    """Identifies unfulfilled campaign qualification criteria sorted by weight priority."""
    weights = rule.weights or {}
    if not weights:
        weights = {
            "icp": 30,
            "budget": 25,
            "timeline": 20,
            "requirement": 15,
            "decision_maker": 10,
        }
    missing: List[str] = []
    sorted_criteria = sorted(weights.items(), key=lambda x: x[1], reverse=True)
    for criterion, _ in sorted_criteria:
        if _sub_score(lead, criterion, criteria_data) < 0.5:
            missing.append(criterion)
    return missing

