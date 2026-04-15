#!/usr/bin/env python
"""Test script for Gemini API"""
import httpx
import asyncio
import time

async def test_health():
    """Test the health endpoint"""
    await asyncio.sleep(2)  # Wait for app to be ready
    
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            print("Testing Gemini API via /api/health...")
            response = await client.get("http://localhost:8001/api/health")
            print(f"Status Code: {response.status_code}")
            print(f"Response:\n{response.json()}")
            
            if response.status_code == 200:
                data = response.json()
                if data.get("gemini_api") == "working":
                    print("\n✓ SUCCESS! Gemini API is responding!")
                else:
                    print("\n✗ API Error:", data.get("error"))
            else:
                print(f"\n✗ HTTP Error: {response.status_code}")
    except Exception as e:
        print(f"✗ Connection Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_health())
