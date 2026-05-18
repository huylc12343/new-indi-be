
from datetime import datetime

import requests
from utils.config import DIRECTUS_URL, DIRECTUS_TOKEN
import os
import httpx
from fastapi import HTTPException

from dotenv import load_dotenv
load_dotenv()

DIRECTUS_URL = os.getenv("DIRECTUS_URL")
DIRECTUS_TOKEN = os.getenv("DIRECTUS_TOKEN")
print("DIRECTUS_URL =", os.getenv("DIRECTUS_URL"))
print("DIRECTUS_TOKEN =", DIRECTUS_TOKEN)
DIRECTUS_HEADERS = {
    "Authorization": f"Bearer {DIRECTUS_TOKEN}",
    "Content-Type": "application/json",
}

def create_order(payload: dict):
    payload["order_id"]= "DH"+str(datetime.now().timestamp()).split(".")[0][-6:]
    payload["payos_order_code"]= payload["order_id"]
    res = requests.post(f"{DIRECTUS_URL}/items/merch_orders", headers=DIRECTUS_HEADERS, json=payload)
    res.raise_for_status()
    print("ORDER STATUS:", res.status_code)
    print("ORDER RESPONSE:", res.text)
    if res.status_code not in [200, 201]:
        raise HTTPException(status_code=500, detail=res.text)
    return res.json()["data"]

def create_order_item(payload: dict):
    res = requests.post(
        f"{DIRECTUS_URL}/items/merch_order_items",
        headers=DIRECTUS_HEADERS,
        json=payload
    )

    print("STATUS:", res.status_code)
    print("RESPONSE:", res.text)

    if res.status_code not in [200, 201]:
        raise HTTPException(status_code=res.status_code, detail=res.text)

    return res.json()["data"]

def get_order(order_id: str):
    res = requests.get(
        f"{DIRECTUS_URL}/items/merch_orders/{order_id}",
        headers=DIRECTUS_HEADERS,
        params={"fields[]": ["*", "merch_order_items.*"]},
    )
    res.raise_for_status()
    return res.json()["data"]
def get_order_by_code(order_code: str):
    """Tìm order theo order_code (string 6 số)"""
    res = requests.get(
        f"{DIRECTUS_URL}/items/merch_orders",
        headers=DIRECTUS_HEADERS,
        params={
            "filter[order_code][_eq]": order_code,
            "fields[]": ["*", "merch_order_items.*"],
            "limit": 1,
        },
    )
    res.raise_for_status()
    data = res.json().get("data", [])
    return data[0] if data else None
def update_order_status(order_id: str, status: str):
    res = requests.patch(
        f"{DIRECTUS_URL}/items/merch_orders/{order_id}",
        headers=DIRECTUS_HEADERS,
        json={"status": status},
    )
    res.raise_for_status()
    
def cancel_order(order_id: str):
    update_order_status(order_id, "cancel")


def get_discount_code_by_code(code: str):
    url = (
        f"{DIRECTUS_URL}/items/discount_codes"
        f"?filter[code][_eq]={code}"
        f"&filter[status][_eq]=available"
        f"&fields=*"
    )

    headers = {
        "Authorization": f"Bearer {DIRECTUS_TOKEN}"
    }

    res = requests.get(url, headers=headers)

    print("DISCOUNT STATUS:", res.status_code)
    print("DISCOUNT RESPONSE:", res.text)

    res.raise_for_status()

    data = res.json().get("data", [])

    if not data:
        return None

    return data[0]

def get_order_by_payos_code(order_code: int):
    res = requests.get(
        f"{DIRECTUS_URL}/items/merch_orders",
        headers=DIRECTUS_HEADERS,
        params={
            "filter[payos_order_code][_eq]": order_code,
            "limit": 1,
        },
    )

    res.raise_for_status()
    data = res.json().get("data", [])
    return data[0] if data else None