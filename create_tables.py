"""
create_tables.py
Creates the core tables for the app in MySQL using credentials from .env.

Usage:
    pip install pymysql python-dotenv
    python create_tables.py
"""

import os
import pymysql
from dotenv import load_dotenv

load_dotenv()  # reads .env in the same folder

conn = pymysql.connect(
    host=os.getenv("DB_HOST", "localhost"),
    port=int(os.getenv("DB_PORT", 3306)),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD", ""),
    charset="utf8mb4",
)

DB_NAME = os.getenv("DB_NAME", "callzenza_db")

TABLES = {}

TABLES["users"] = """
CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(150) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(30) DEFAULT 'ADMIN',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

TABLES["lead_groups"] = """
CREATE TABLE IF NOT EXISTS lead_groups (
    id INT AUTO_INCREMENT PRIMARY KEY,
    filename VARCHAR(255) NOT NULL,
    lead_count INT DEFAULT 0,
    status VARCHAR(30) DEFAULT 'IDLE',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

TABLES["leads"] = """
CREATE TABLE IF NOT EXISTS leads (
    id INT AUTO_INCREMENT PRIMARY KEY,
    group_id INT,
    first_name VARCHAR(100) DEFAULT '',
    last_name VARCHAR(100) DEFAULT '',
    email VARCHAR(255) DEFAULT '',
    phone VARCHAR(50) NOT NULL,
    company VARCHAR(255) DEFAULT '',
    requirement TEXT,
    budget VARCHAR(100) DEFAULT '',
    location VARCHAR(255) DEFAULT '',
    status VARCHAR(30) DEFAULT 'NEW',
    call_status VARCHAR(50) DEFAULT 'NEW',
    call_result VARCHAR(255) DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES lead_groups(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

TABLES["ai_agents"] = """
CREATE TABLE IF NOT EXISTS ai_agents (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(150) NOT NULL,
    type VARCHAR(50) DEFAULT 'SALES',
    system_prompt TEXT,
    voice_id VARCHAR(100),
    status VARCHAR(30) DEFAULT 'ACTIVE',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

TABLES["campaigns"] = """
CREATE TABLE IF NOT EXISTS campaigns (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    agent_id INT,
    twilio_number VARCHAR(50) DEFAULT '',
    status VARCHAR(30) DEFAULT 'IDLE',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (agent_id) REFERENCES ai_agents(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

TABLES["campaign_leads"] = """
CREATE TABLE IF NOT EXISTS campaign_leads (
    id INT AUTO_INCREMENT PRIMARY KEY,
    campaign_id INT NOT NULL,
    lead_id INT NOT NULL,
    status VARCHAR(30) DEFAULT 'PENDING',
    call_id INT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE,
    FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

TABLES["calls"] = """
CREATE TABLE IF NOT EXISTS calls (
    id INT AUTO_INCREMENT PRIMARY KEY,
    lead_id INT,
    agent_id INT,
    direction VARCHAR(20) DEFAULT 'OUTBOUND',
    from_number VARCHAR(50) DEFAULT '',
    to_number VARCHAR(50) DEFAULT '',
    status VARCHAR(30) DEFAULT 'QUEUED',
    call_sid VARCHAR(100) DEFAULT '',
    duration_seconds INT DEFAULT 0,
    transcript TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE,
    FOREIGN KEY (agent_id) REFERENCES ai_agents(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

def main():
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` CHARACTER SET utf8mb4;")
        cur.execute(f"USE `{DB_NAME}`;")

        # Order matters: tables with no FK dependencies first
        creation_order = ["users", "lead_groups", "ai_agents", "leads", "campaigns", "campaign_leads", "calls"]
        for table_name in creation_order:
            print(f"Creating table: {table_name}")
            cur.execute(TABLES[table_name])

    conn.commit()
    print(f"\nDone. All tables created in database '{DB_NAME}'.")

if __name__ == "__main__":
    main()
    conn.close()