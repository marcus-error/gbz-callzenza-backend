import sqlite3
import uuid
import datetime
import sys
import os

sys.path.insert(0, ".")
from app.core.security import hash_password

db_path   = "callzenza.db"
business_id = "fa5ffab7-4039-47e3-9c05-3546b2109a12"  # Default Business
email     = "portaluser@callzenza.com"
password  = "Portal@123"
hashed    = hash_password(password)
now       = datetime.datetime.utcnow().isoformat()

conn = sqlite3.connect(db_path)
cur  = conn.cursor()

cur.execute("SELECT id FROM users WHERE email = ?", (email,))
existing = cur.fetchone()

if existing:
    print(f"User already exists (id={existing[0]}). Updating role + password...")
    cur.execute(
        "UPDATE users SET role='AI_AGENT_PORTAL_USER', is_active=1, hashed_password=? WHERE email=?",
        (hashed, email),
    )
else:
    user_id = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO users
            (id, email, hashed_password, full_name, first_name, last_name, role, is_active,
             business_id, created_at, email_verified, permissions, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, email, hashed, "AI Agent Portal User", "Portal", "User",
         "AI_AGENT_PORTAL_USER", 1, business_id, now, 1, '{}', 'active'),
    )
    print(f"Created portal user  (id={user_id})")

conn.commit()
conn.close()

print()
print("=" * 44)
print("  AI AGENT PORTAL — LOGIN CREDENTIALS")
print("=" * 44)
print(f"  URL      : http://localhost:3000/agent-portal/login")
print(f"  Email    : {email}")
print(f"  Password : {password}")
print("=" * 44)
