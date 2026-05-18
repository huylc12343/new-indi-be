# app.py — thêm webhook route + socketio
from gevent import monkey
monkey.patch_all()

import os
from datetime import datetime, timezone, timedelta
import asyncio
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO, join_room
from redis import Redis
from rq import Queue
from nanoid import generate
from utils.viettel import calculate_shipping_fee
from utils.config import FLASK_DEBUG, REDIS_URL, ORDER_EXPIRE_SECONDS, CORS_ORIGINS
from utils.directus import (
    create_order, create_order_item, get_order, get_order_by_code, update_order_status, cancel_order, get_discount_code_by_code, get_order_by_payos_code
)
from utils.payos import verify_webhook_signature, create_payos_payment
from utils.validate import validate_order
from jobs.expire_order import expire_order


# Thêm import sau dòng from flask_cors import CORS
from flask_limiter import Limiter, RequestLimit

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def generate_order_code():
    first = generate('123456789', 1)   # chữ số đầu: 1-9
    rest = generate('0123456789', 7)   # 7 chữ số còn lại: 0-9
    return first + rest

app = Flask(__name__)
if CORS_ORIGINS:
    CORS(app, origins=CORS_ORIGINS.split(","))
else:
    CORS(app)

# Thêm sau khi khởi tạo app và CORS
def get_real_ip():
    return request.headers.get("X-Real-IP") or request.remote_addr

limiter = Limiter(
    app=app,
    key_func=get_real_ip,
    storage_uri=REDIS_URL,  # dùng luôn REDIS_URL từ config
    default_limits=[],      # không apply global limit
)

redis_conn = Redis.from_url(REDIS_URL)
q = Queue(connection=redis_conn)

def handle_order_breach(request_limit: RequestLimit):
    ip = get_real_ip()
    try:
        if not redis_conn.exists(f"blocked:{ip}"):
            redis_conn.setex(f"blocked:{ip}", 12 * 3600, 1)
            logger.warning(f"IP {ip} blocked for 12 hours due to repeated order attempts")
    except Exception as e:
        logger.error(f"Failed to block IP {ip}: {e}")

# Socket.IO — dùng Redis làm message queue để hoạt động đúng khi multi-instance
# Đổi async_mode
socketio = SocketIO(
    app,
    cors_allowed_origins=CORS_ORIGINS.split(",") if CORS_ORIGINS else "*",
    message_queue=REDIS_URL,
    async_mode="gevent",  # đổi từ eventlet
)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

@app.route("/orders", methods=["POST"])
@limiter.limit("5 per minute", on_breach=handle_order_breach)
def create_order_route():
    if redis_conn.get(f"blocked:{get_real_ip()}"):
        return jsonify({"error": "Too many requests"}), 429

    body = request.get_json()
    if not body:
        return jsonify({"error": "Missing request body"}), 400

    order_items = body.pop("order_items", [])
    if not order_items:
        return jsonify({"error": "order_items is required"}), 400

    shipping_fee = float(body.get("shipping_fee", 0))

    try:
        # validate
        error = validate_order({**body, "order_items": order_items})
        if error:
            return jsonify({"errors": {"message": error}}), 422

        # subtotal
        subtotal = sum(
            float(item["unit_price"]) * int(item["quantity"])
            for item in order_items
        )

        # 🔥 DISCOUNT
        discount = 0
        discount_code_value = body.get("discount_code")

        if discount_code_value:
            discount_code = get_discount_code_by_code(discount_code_value)

            if discount_code:
                if discount_code["type"] == "fixed":
                    discount = float(discount_code["value"])
                elif discount_code["type"] == "percent":
                    discount = subtotal * float(discount_code["value"]) / 100

        # total
        total = max(0, subtotal + shipping_fee - discount)

        # tạo order_code
        order_code = generate_order_code()
        expires_at = (
            datetime.now(timezone.utc).replace(microsecond=0)
            + timedelta(seconds=ORDER_EXPIRE_SECONDS)
        )

        body.update({
            "order_code": order_code,
            "status": "init",
            "subtotal": subtotal,
            "discount": discount,
            "total": total,
            "expires_at": expires_at.isoformat(),
        })

        # ✅ TẠO ORDER
        order = create_order(body)
        order_id = order["id"]

        # ✅ TẠO ITEMS
        created_items = []
        for item in order_items:
            item["merch_order_id"] = order_id
            created_items.append(create_order_item(item))

        order["order_items"] = created_items

        # ✅ PAYOS
        payment_info = create_payos_payment(
            order_code=order_code,
            amount=total,
            expires_at=expires_at,
        )
        order["payment_info"] = payment_info

        # expire job
        job = q.enqueue_in(
            timedelta(seconds=ORDER_EXPIRE_SECONDS),
            expire_order,
            order_id,
        )

        redis_conn.setex(
            f"expire_job:{order_id}",
            ORDER_EXPIRE_SECONDS + 60,
            job.id,
        )

        return jsonify(order), 201

    except Exception as e:
        if "order_id" in locals():
            cancel_order(order_id)
        raise e

@app.route("/orders/<order_id>", methods=["GET"])
def get_order_route(order_id: str):
    order = get_order(order_id)
    if not order:
        return jsonify({"error": "Order not found"}), 404
    return jsonify(order)

@app.route("/shipping/fee", methods=["POST"])
def get_shipping_fee():
    body = request.get_json()

    address = body.get("address")
    subtotal = body.get("subtotal", 0)

    if not address:
        return jsonify({"error": "address is required"}), 400

    try:
        # ✅ FIX CHUẨN: dùng asyncio.run (không tạo loop thủ công)
        shipping_data = calculate_shipping_fee(address, subtotal)

        return jsonify(shipping_data)

    except Exception as e:
        logger.error(f"Shipping error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/webhook/payos", methods=["POST"])
def payos_webhook():
    body = request.get_json()

    if not body:
        return jsonify({"error": "Missing body"}), 400

    data = body.get("data", {})
    signature = body.get("signature", "")

    if not verify_webhook_signature(data, signature):
        return jsonify({"error": "Invalid signature"}), 401

    if body.get("code") != "00" or not body.get("success"):
        return jsonify({"status": "ignored"}), 200

    order_code = str(data.get("orderCode", ""))
    order = get_order_by_code(order_code)

    if not order:
        return jsonify({"error": "Order not found"}), 404

    # ✅ idempotent
    if order["status"] != "init":
        logger.info(f"Order {order['id']} already processed")
        return jsonify({"status": "ignored"}), 200

    # ✅ check tiền
    paid_amount = data.get("amount", 0)
    if paid_amount != order["total"]:
        logger.warning(f"Amount mismatch: {paid_amount} != {order['total']}")
        return jsonify({"status": "ignored"}), 200

    order_id = order["id"]

    # ❌ bỏ cancel ticket job
    _cancel_expire_job(order_id)

    # ✅ update luôn sang PAID
    update_order_status(order_id, "paid")
    order["status"] = "paid"

    # 🔥 emit realtime
    socketio.emit(
        "payment_success",
        {"order_id": order_id, "order": order},
        to=order_id
    )

    return jsonify({"status": "ok"}), 200

def _cancel_expire_job(order_id: str):
    """
    Tìm và cancel job expire_order đang chờ trong queue.
    RQ không có API tìm job theo args nên ta lưu job_id vào Redis khi enqueue.
    """
    job_id_key = f"expire_job:{order_id}"
    job_id = redis_conn.get(job_id_key)
    if job_id:
        from rq.job import Job
        try:
            job = Job.fetch(job_id.decode(), connection=redis_conn)
            job.cancel()
        except Exception:
            pass  # job đã chạy hoặc không tồn tại
        redis_conn.delete(job_id_key)


# Socket.IO events
@socketio.on("join_order")
def on_join_order(data):
    """Client gọi emit('join_order', {order_id: '...'}) để subscribe"""
    order_id = data.get("order_id")
    if order_id:
        join_room(order_id)

@app.route("/dev/fake-webhook/<order_code>", methods=["POST"])
def fake_webhook(order_code):
    order = get_order_by_code(order_code)
    if not order:
        return jsonify({"error": "not found"}), 404

    # 🔥 payload giống PayOS
    body = {
        "code": "00",
        "desc": "success",
        "data": {
            "orderCode": int(order_code),
            "amount": int(float(order["total"])),
            "description": f"TEST {order['order_code']}",
            "accountNumber": "123456789",
            "reference": "FT123456",
            "transactionDateTime": datetime.now().isoformat(),
        },
        "signature": "fake_signature"
    }

    # 🔥 gọi trực tiếp logic webhook thật
    with app.test_request_context(
        "/webhook/payos",
        method="POST",
        json=body
    ):
        return payos_webhook()

if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        debug=FLASK_DEBUG,
    )