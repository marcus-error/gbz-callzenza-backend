import requests

try:
    res = requests.post("http://localhost:8000/api/auth/login", json={"email": "admin@callzenza.com", "password": "adminpassword"})
    if res.status_code == 200:
        token = res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        
        print("Fetching /api/admin/campaigns...")
        res_camp = requests.get("http://localhost:8000/api/admin/campaigns", headers=headers)
        print("Status:", res_camp.status_code)
        if res_camp.status_code != 200:
            print(res_camp.text)
            
        print("Fetching /api/voice-agents...")
        res_va = requests.get("http://localhost:8000/api/voice-agents", headers=headers)
        print("Status:", res_va.status_code)
        if res_va.status_code != 200:
            print(res_va.text)
except Exception as e:
    print("Error:", e)
