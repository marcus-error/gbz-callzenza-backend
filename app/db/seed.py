"""
Module 55 — seed data. Run with:

    python -m app.db.seed

Creates 2 businesses (Real Estate, IT Services), owner users, AI agents,
leads, conversations, experts, and deals so the dashboard has real numbers
to show immediately after `docker compose up`.
"""
from __future__ import annotations

import asyncio
import random

from app.core.security import hash_password
from app.db.database import AsyncSessionLocal, init_db
from app.db.models import (
    AIAgent,
    Business,
    Deal,
    Expert,
    Lead,
    QualificationRule,
    User,
)

FIRST_NAMES = ["Alex", "Jordan", "Priya", "Sam", "Maria", "Chen", "Liam", "Nina", "Omar", "Grace"]
LAST_NAMES = ["Rivera", "Chen", "Patel", "Okafor", "Novak", "Silva", "Kim", "Ahmed", "Bianchi", "Dubois"]
COMPANIES = ["Acme Corp", "Northwind", "Globex", "Initech", "Umbrella LLC", "Soylent Co"]
SOURCES = ["WEBSITE", "LANDING_PAGE", "GOOGLE_FORMS", "WHATSAPP", "MANUAL"]
STAGES = ["NEW", "QUALIFIED", "DISCOVERY", "PROPOSAL", "NEGOTIATION", "WON", "LOST"]


async def seed_business(db, name: str, industry: str, owner_email: str, owner_password: str) -> Business:
    owner = User(
        email=owner_email,
        hashed_password=hash_password(owner_password),
        full_name=f"{name} Owner",
        role="BUSINESS_OWNER",
    )
    db.add(owner)
    await db.flush()

    business = Business(name=name, industry=industry, owner_user_id=owner.id)
    db.add(business)
    await db.flush()

    owner.business_id = business.id
    db.add(owner)
    db.add(QualificationRule(business_id=business.id))

    agent = AIAgent(
        business_id=business.id,
        name=f"{name} AI Sales Agent",
        type="SALES",
        system_prompt=f"You are a friendly AI sales agent for {name}, a {industry} business.",
        qualification_prompt="Find out budget, timeline, and decision-making authority.",
    )
    db.add(agent)

    experts = [
        Expert(business_id=business.id, name=f"{fn} {ln}", email=f"{fn.lower()}.{ln.lower()}@{name.lower().replace(' ', '')}.com", skills=[industry])
        for fn, ln in random.sample(list(zip(FIRST_NAMES, LAST_NAMES)), 2)
    ]
    db.add_all(experts)
    await db.flush()

    for _ in range(15):
        fn, ln = random.choice(FIRST_NAMES), random.choice(LAST_NAMES)
        status = random.choice(["NEW", "ENGAGED", "QUALIFYING", "QUALIFIED", "ASSIGNED", "WON", "NURTURE"])
        qual = "QUALIFIED" if status in ("QUALIFIED", "ASSIGNED", "WON") else random.choice(["UNQUALIFIED", "NURTURE"])
        lead = Lead(
            business_id=business.id,
            source=random.choice(SOURCES),
            first_name=fn,
            last_name=ln,
            email=f"{fn.lower()}.{ln.lower()}@example.com",
            phone="+1-555-0100",
            company=random.choice(COMPANIES),
            job_title=random.choice(["CEO", "Manager", "Director", "Analyst", ""]),
            requirement=f"Looking for {industry.lower()} solutions for a growing team.",
            budget=random.choice(["$500/mo", "$2,000/mo", "$10,000/mo", ""]),
            timeline=random.choice(["This week", "Next month", "This quarter", ""]),
            status=status,
            score=random.randint(10, 95),
            qualification_status=qual,
            agent_id=agent.id,
            assigned_expert_id=random.choice(experts).id if status in ("ASSIGNED", "WON") else None,
        )
        db.add(lead)
        await db.flush()

        if status in ("ASSIGNED", "WON"):
            deal = Deal(
                business_id=business.id,
                lead_id=lead.id,
                expert_id=lead.assigned_expert_id,
                title=f"{lead.company} — {industry} deal",
                value=random.choice([1500, 5000, 12000, 25000]),
                stage="WON" if status == "WON" else random.choice(STAGES[:5]),
                probability=random.randint(20, 90),
            )
            db.add(deal)

    return business


async def main() -> None:
    import os
    seed_pw = os.getenv("SEED_OWNER_PASSWORD")
    if not seed_pw or not seed_pw.strip():
        raise RuntimeError("SEED_OWNER_PASSWORD environment variable must be set to run database seed script.")

    await init_db()
    async with AsyncSessionLocal() as db:
        await seed_business(db, "Skyline Realty", "Real Estate", "owner@skylinerealty.com", seed_pw.strip())
        await seed_business(db, "NimbusIT Services", "IT Services", "owner@nimbusit.com", seed_pw.strip())
        await db.commit()
    print("Seed complete successfully.")


if __name__ == "__main__":
    asyncio.run(main())
