import requests

try:
    res = requests.post("http://localhost:8000/api/auth/login", json={"email": "test@gmail.com", "password": "test123456"})
    print("test@gmail.com (test123456):", res.status_code, res.text)
except Exception as e:
    print(e)
