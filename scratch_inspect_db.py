import sqlite3

conn = sqlite3.connect("callzenza.db")
c = conn.cursor()

print("=== CAMPAIGNS ===")
for r in c.execute("SELECT id, name, auto_dial_ratio, trunk_id, status FROM campaigns WHERE name LIKE '%Nurse%' OR id = 'be56c8d0-b9f3-4a47-9206-d17270257cd7'"):
    print(r)

print("\n=== RECENT CALLS FOR CAMPAIGN be56c8d0-b9f3-4a47-9206-d17270257cd7 ===")
for r in c.execute("SELECT id, to_number, status, initiated_at, connected_at, ended_at, duration FROM calls WHERE campaign_id = 'be56c8d0-b9f3-4a47-9206-d17270257cd7' ORDER BY initiated_at DESC LIMIT 16"):
    print(r)

print("\n=== CAMPAIGN_LEADS FOR Nurse o clock ===")
for r in c.execute("SELECT id, lead_id, call_id, status, attempts FROM campaign_leads WHERE campaign_id = 'be56c8d0-b9f3-4a47-9206-d17270257cd7'"):
    print(r)

conn.close()
