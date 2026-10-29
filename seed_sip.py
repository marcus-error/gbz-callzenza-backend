"""
CallZenza SIP Database Seeder.
NOTE: In accordance with the dynamic, multi-tenant architecture, NO hardcoded or dummy
SIP accounts or credentials are seeded.
Administrators create and manage live SIP configurations securely via the Admin Dashboard.
"""
import asyncio
from loguru import logger


async def seed_asterisk():
    logger.info(
        "[SIP SEEDER] Dynamic SIP architecture is active. "
        "No hardcoded SIP accounts are seeded. "
        "Please configure your organization's SIP accounts through the Admin Dashboard."
    )


if __name__ == "__main__":
    asyncio.run(seed_asterisk())
