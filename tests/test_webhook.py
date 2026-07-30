"""
test_webhook.py

Simulates a Lemon Squeezy `subscription_created` webhook, signed correctly,
sent to your live SQLVerify endpoint. Use this to test the Discord
notification without needing a real purchase.

Usage:
    python test_webhook.py
"""

import hashlib
import hmac
import json
import requests
import os
from dotenv import load_dotenv

load_dotenv()

# ⚠️ Must match LEMONSQUEEZY_WEBHOOK_SECRET set in Render
WEBHOOK_SECRET = os.getenv("LEMONSQUEEZY_WEBHOOK_SECRET")

URL = "https://sqlverify.com/api/webhooks/lemonsqueezy"

payload = {
    "meta": {
        "event_name": "subscription_updated",
        "custom_data": {"user_id": "test-user-123"}
    },
    "data": {
        "id": "999999",
        "attributes": {
            "customer_id": "123456",
            "user_email": "testuser@example.com",
            "order_id": "888888",
            "product_id": "111",
            "variant_id": "222",  # doesn't need to match a real tier for this test
        }
    }
}

body = json.dumps(payload).encode("utf-8")
signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()

response = requests.post(
    URL,
    data=body,
    headers={
        "Content-Type": "application/json",
        "X-Signature": signature,
    },
)

print(f"Status: {response.status_code}")
print(f"Response: {response.text}")