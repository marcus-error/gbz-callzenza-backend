import sqlite3
import json

db_path = 'callmira.db'

import os
target_db = db_path if os.path.exists(db_path) else None

if target_db:
    try:
        conn = sqlite3.connect(target_db)
        cursor = conn.cursor()
        
        # Check if column exists
        cursor.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'permissions' not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN permissions JSON DEFAULT '{}'")
            conn.commit()
            print("Successfully added permissions column to users table.")
        else:
            print("permissions column already exists.")

        if 'plain_password' not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN plain_password VARCHAR(255) DEFAULT ''")
            conn.commit()
            print("Successfully added plain_password column to users table.")
        else:
            print("plain_password column already exists.")

        # Check campaigns table
        cursor.execute("PRAGMA table_info(campaigns)")
        cmp_columns = [col[1] for col in cursor.fetchall()]
        if 'voice_agent_id' not in cmp_columns:
            cursor.execute("ALTER TABLE campaigns ADD COLUMN voice_agent_id VARCHAR(36)")
            conn.commit()
            print("Successfully added voice_agent_id column to campaigns table.")
        else:
            print("voice_agent_id column already exists.")

        if 'lead_group_id' not in cmp_columns:
            cursor.execute("ALTER TABLE campaigns ADD COLUMN lead_group_id VARCHAR(36)")
            conn.commit()
            print("Successfully added lead_group_id column to campaigns table.")
        else:
            print("lead_group_id column already exists.")

        if 'client_id' not in cmp_columns:
            cursor.execute("ALTER TABLE campaigns ADD COLUMN client_id VARCHAR(36)")
            conn.commit()
            print("Successfully added client_id column to campaigns table.")

        # Check calls table
        cursor.execute("PRAGMA table_info(calls)")
        call_columns = [col[1] for col in cursor.fetchall()]
        if 'campaign_id' not in call_columns:
            cursor.execute("ALTER TABLE calls ADD COLUMN campaign_id VARCHAR(36)")
            conn.commit()
            print("Successfully added campaign_id column to calls table.")
        if 'client_id' not in call_columns:
            cursor.execute("ALTER TABLE calls ADD COLUMN client_id VARCHAR(36)")
            conn.commit()
            print("Successfully added client_id column to calls table.")

        # Check call_transfers table
        cursor.execute("PRAGMA table_info(call_transfers)")
        tr_columns = [col[1] for col in cursor.fetchall()]
        if 'campaign_id' not in tr_columns:
            cursor.execute("ALTER TABLE call_transfers ADD COLUMN campaign_id VARCHAR(36)")
            conn.commit()
            print("Successfully added campaign_id column to call_transfers table.")
        if 'client_id' not in tr_columns:
            cursor.execute("ALTER TABLE call_transfers ADD COLUMN client_id VARCHAR(36)")
            conn.commit()
            print("Successfully added client_id column to call_transfers table.")

        conn.close()
    except Exception as e:
        print(f"Error: {e}")
else:
    print("Database not found.")
