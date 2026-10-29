import requests

try:
    res = requests.post("http://localhost:8000/api/auth/login", json={"email": "superv@gmail.com", "password": "123456789"})
    print("superv@gmail.com (123456789):", res.status_code, res.text)
except Exception as e:
    print(e)
