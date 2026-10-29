import requests

try:
    res = requests.post("http://localhost:8000/api/auth/login", json={"email": "test@gmail.com", "password": "test123456"})
    if res.status_code == 200:
        token = res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        
        print("Fetching /api/voice/transfers...")
        res_t = requests.get("http://localhost:8000/api/voice/transfers", headers=headers)
        print("Transfers:", res_t.status_code)
        if res_t.status_code != 200:
            print(res_t.text)
            
        print("Fetching /api/voice/campaign-clients...")
        res_c = requests.get("http://localhost:8000/api/voice/campaign-clients", headers=headers)
        print("Campaign-Clients:", res_c.status_code)
        if res_c.status_code != 200:
            print(res_c.text)
except Exception as e:
    print("Error:", e)
