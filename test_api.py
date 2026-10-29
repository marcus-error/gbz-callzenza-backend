import asyncio
import httpx

async def test_api():
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        print("Logging in...")
        resp = await client.post("/api/auth/login", json={"email": "superadmin@callzenza.com", "password": "password123"})
        if resp.status_code != 200:
            print("Login failed:", resp.status_code, resp.text)
            resp = await client.post("/api/auth/login", json={"email": "sanjay1@gmail.com", "password": "password123"})
            if resp.status_code != 200:
                print("Fallback login failed:", resp.status_code, resp.text)
                return

        token = resp.json().get("access_token")
        headers = {"Authorization": f"Bearer {token}"}
        
        urls = [
            "/api/business/me",
            "/api/lead-groups",
            "/api/campaigns",
            "/api/agents",
            "/api/admin/users"
        ]

        for u in urls:
            print(f"Testing {u}")
            r = await client.get(u, headers=headers)
            print(r.status_code)
            if r.status_code == 500:
                print(r.text)

asyncio.run(test_api())
