import requests

payload = {
    "email": "super@gmail.com",
    "password": "password123" # assuming this is what was used, or I will use superv@gmail.com. Let's see what password the db script showed... wait, I didn't print plain passwords. Let's create a new one first or try typical ones.
}
try:
    res = requests.post("http://localhost:8000/api/auth/login", json={"email": "super@gmail.com", "password": "password"})
    print("super@gmail.com (password):", res.status_code, res.text)
except Exception as e:
    print(e)
