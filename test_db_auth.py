import requests

def test_login_endpoint():
    url = "http://127.0.0.1:8000/api/auth/login"
    payload = {"email": "admin@callzenza.com", "password": "password123"}
    print("Testing POST to:", url, "with payload:", payload)
    try:
        r = requests.post(url, json=payload)
        print("Status code:", r.status_code)
        print("Response body:", r.text)
    except Exception as e:
        print("Error calling endpoint:", e)

if __name__ == "__main__":
    test_login_endpoint()
