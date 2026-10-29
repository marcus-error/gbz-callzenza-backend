import requests

# 1. Login as admin to get token
res_admin = requests.post("http://localhost:8000/api/auth/login", json={"email": "admin@callzenza.com", "password": "adminpassword"})
if res_admin.status_code != 200:
    print("Admin login failed", res_admin.text)
    # try another admin
    res_admin = requests.post("http://localhost:8000/api/auth/login", json={"email": "admin@callmira.com", "password": "adminpassword"})

if res_admin.status_code == 200:
    token = res_admin.json()["access_token"]
    
    # 2. Create agent
    headers = {"Authorization": f"Bearer {token}"}
    agent_payload = {
        "name": "Test Agent Supervisor",
        "type": "VOICE",
        "email": "agent_sup@gmail.com",
        "password": "agentpassword123"
    }
    res_agent = requests.post("http://localhost:8000/api/mgmt/agents", json=agent_payload, headers=headers)
    print("Create Agent:", res_agent.status_code, res_agent.text)
    
    # 3. Login as agent
    res_login = requests.post("http://localhost:8000/api/auth/login", json={"email": "agent_sup@gmail.com", "password": "agentpassword123"})
    print("Agent Login:", res_login.status_code, res_login.text)
