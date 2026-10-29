import requests

try:
    res = requests.post("http://localhost:8000/api/auth/login", json={"email": "admin@callzenza.com", "password": "admin123"})
    print("Login:", res.status_code)
    if res.status_code == 200:
        token = res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        
        print("Fetching /api/clients...")
        res_t = requests.get("http://localhost:8000/api/clients", headers=headers)
        print("GET /api/clients:", res_t.status_code)
        if res_t.status_code != 200:
            print(res_t.text)
        else:
            print("Success, length:", len(res_t.json()))
    else:
        print("Login failed:", res.text)
except Exception as e:
    print("Error:", e)
