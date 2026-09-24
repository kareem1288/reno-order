"""Mock external logistics API for demos (Part 7). Standard library only.

	python scripts/mock_logistics_server.py serve --port 8765 --api-key demo-key [--delay 12] [--fail-rate 0.2]
	python scripts/mock_logistics_server.py webhook http://127.0.0.1:8004 demo-secret LGX-0001 delivered

serve    POST /bookings with "Authorization: Bearer <api-key>". Sleeps --delay seconds
         (the assignment's slow 10-20 s API), answers 503 on a --fail-rate share of calls,
         and returns the same booking for a repeated Idempotency-Key.
webhook  Sends a correctly signed delivery event to the site's delivery_webhook.
"""

import argparse
import hashlib
import hmac
import json
import random
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def serve(port: int, api_key: str, delay: float, fail_rate: float):
	bookings: dict[str, str] = {}

	class Handler(BaseHTTPRequestHandler):
		def do_POST(self):
			if self.path.rstrip("/") != "/bookings":
				return self.reply(404, {"error": "not found"})
			if self.headers.get("Authorization") != f"Bearer {api_key}":
				return self.reply(401, {"error": "invalid api key"})
			body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
			time.sleep(delay)
			if random.random() < fail_rate:
				return self.reply(503, {"error": "temporarily unavailable"})
			key = self.headers.get("Idempotency-Key") or str(uuid.uuid4())
			booking_id = bookings.setdefault(key, f"LGX-{len(bookings) + 1:04d}")
			print(f"booked {body.get('reference')} -> {booking_id} ({len(body.get('items', []))} items)")
			self.reply(201, {"booking_id": booking_id, "status": "booked"})

		def reply(self, status, payload):
			data = json.dumps(payload).encode()
			self.send_response(status)
			self.send_header("Content-Type", "application/json")
			self.send_header("Content-Length", str(len(data)))
			self.end_headers()
			self.wfile.write(data)

	print(f"mock logistics API on http://127.0.0.1:{port} (delay {delay}s, fail rate {fail_rate})")
	ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def send_webhook(site_url: str, secret: str, booking_id: str, event: str):
	body = json.dumps({"event_id": str(uuid.uuid4()), "booking_id": booking_id, "event": event}).encode()
	signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
	request = urllib.request.Request(
		f"{site_url.rstrip('/')}/api/method/reno_order.api.v1.logistics.delivery_webhook",
		data=body,
		headers={"Content-Type": "application/json", "X-Reno-Signature": signature},
		method="POST",
	)
	with urllib.request.urlopen(request, timeout=10) as response:
		print(response.status, response.read().decode())


if __name__ == "__main__":
	parser = argparse.ArgumentParser()
	sub = parser.add_subparsers(dest="command", required=True)
	s = sub.add_parser("serve")
	s.add_argument("--port", type=int, default=8765)
	s.add_argument("--api-key", default="demo-key")
	s.add_argument("--delay", type=float, default=12)
	s.add_argument("--fail-rate", type=float, default=0.0)
	w = sub.add_parser("webhook")
	w.add_argument("site_url")
	w.add_argument("secret")
	w.add_argument("booking_id")
	w.add_argument("event", choices=["dispatched", "delivered"])
	args = parser.parse_args()
	if args.command == "serve":
		serve(args.port, args.api_key, args.delay, args.fail_rate)
	else:
		send_webhook(args.site_url, args.secret, args.booking_id, args.event)
