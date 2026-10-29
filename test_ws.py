import asyncio
import websockets

async def test():
    try:
        async with websockets.connect("ws://127.0.0.1:8000/api/ws/call-events?token=test") as websocket:
            print("Connected!")
            await websocket.send("test")
            while True:
                response = await websocket.recv()
                print(f"Received: {response}")
    except websockets.exceptions.ConnectionClosed:
        print("Connection closed")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(test())
    except KeyboardInterrupt:
        pass
