# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# For license information, please see license.txt

"""Third-party integration: delivery booking with an external logistics system (Parts 7, 8).

Outbound: when an order becomes Ready for Installation, a background job books delivery of
its materials to the site: POST {base_url}/bookings. The provider can take 10-20 s, so the
user's status change only queues the job (queue "long": slow I/O must not occupy the
"default" workers that run quick jobs such as the Delivery Note).

	Authentication    Authorization: Bearer <api_key>; key and webhook secret are Password
	                  fields on Reno Logistics Settings, stored encrypted, never in the repo
	Request/response  JSON in, {"booking_id": ...} out; every call is an Integration Request
	                  (URL, payload, response or error) linked to the Reno Order
	Timeouts          (connect, read) from settings, so a hung provider cannot pin a worker
	Errors            timeout / connection error / 429 / 5xx -> retry; other 4xx -> Failed
	                  at once (retrying a rejected payload cannot help)
	Retries           exponential backoff (2, 4, 8 ... minutes, max 60) up to max_attempts,
	                  driven by a 5-minute scheduler job (retry_due_bookings)
	Duplicates        job_id + deduplicate while queued; an existing booking id makes the
	                  job a no-op; and every request carries Idempotency-Key: <order name>,
	                  so even two requests that both reach the provider make one booking.
	                  No row lock is held during the HTTP call: a 20 s lock would block
	                  every user touching the order.

Inbound: delivery_webhook receives status updates, authenticated by an HMAC-SHA256
signature over the raw body (X-Reno-Signature: sha256=<hex>) instead of a user session,
and de-duplicated by the provider's event id.
"""

import hashlib
import hmac
import json

import frappe
import requests
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

SERVICE = "Reno Logistics"
RETRIABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
WEBHOOK_STATUS = {"dispatched": "Dispatched", "delivered": "Delivered"}


class RetriableError(Exception):
	pass


def get_settings():
	return frappe.get_cached_doc("Reno Logistics Settings")


def is_enabled() -> bool:
	return bool(cint(get_settings().enabled))


def queue_booking(reno_order: str):
	if not is_enabled():
		return
	frappe.db.set_value("Reno Order", reno_order, "logistics_status", "Queued", update_modified=False)
	frappe.enqueue(
		"reno_order.api.v1.logistics.book_delivery",
		queue="long",
		timeout=120,
		job_id=f"reno_order_logistics_booking::{reno_order}",
		deduplicate=True,
		enqueue_after_commit=True,
		reno_order=reno_order,
	)


def book_delivery(reno_order: str) -> str | None:
	"""Background job. Returns the booking id, or None if not booked (yet)."""
	settings = get_settings()
	ro = frappe.get_doc("Reno Order", reno_order)
	if not cint(settings.enabled) or ro.docstatus != 1 or ro.status == "Cancelled":
		return None
	if ro.logistics_booking_id:
		return ro.logistics_booking_id  # already booked: a duplicate or late retry

	url = f"{settings.base_url.rstrip('/')}/bookings"
	payload = build_payload(ro)
	log = frappe.get_doc(
		{
			"doctype": "Integration Request",
			"integration_request_service": SERVICE,
			"request_description": "Book delivery",
			"reference_doctype": "Reno Order",
			"reference_docname": ro.name,
			"url": url,
			"data": json.dumps(payload, indent=1, default=str),
			"status": "Queued",
		}
	).insert(ignore_permissions=True)

	attempts = cint(ro.logistics_attempts) + 1
	try:
		booking_id = _post_booking(settings, url, payload, idempotency_key=ro.name)
	except RetriableError as e:
		_fail(ro, log, attempts, str(e), retry=attempts < cint(settings.max_attempts))
		return None
	except Exception as e:
		_fail(ro, log, attempts, str(e), retry=False)
		return None

	log.db_set({"status": "Completed", "output": json.dumps({"booking_id": booking_id})})
	ro.db_set(
		{
			"logistics_status": "Booked",
			"logistics_booking_id": booking_id,
			"logistics_attempts": attempts,
			"logistics_next_retry": None,
			"logistics_error": None,
		},
		notify=True,
	)
	return booking_id


def _post_booking(settings, url, payload, idempotency_key) -> str:
	try:
		response = requests.post(
			url,
			json=payload,
			headers={
				"Authorization": f"Bearer {settings.get_password('api_key')}",
				"Idempotency-Key": idempotency_key,
			},
			timeout=(cint(settings.connect_timeout) or 5, cint(settings.read_timeout) or 30),
		)
	except (requests.Timeout, requests.ConnectionError) as e:
		raise RetriableError(f"{type(e).__name__}: {e}") from e

	if response.status_code in RETRIABLE_STATUS:
		raise RetriableError(f"HTTP {response.status_code}: {response.text[:300]}")
	if not response.ok:
		raise frappe.ValidationError(f"HTTP {response.status_code}: {response.text[:300]}")
	try:
		booking_id = response.json()["booking_id"]
	except (ValueError, KeyError, TypeError) as e:
		raise frappe.ValidationError(f"Unexpected response: {response.text[:300]}") from e
	return str(booking_id)


def _fail(ro, log, attempts, error, retry):
	log.db_set({"status": "Failed", "error": error})
	next_retry = add_to_date(now_datetime(), minutes=min(2**attempts, 60)) if retry else None
	ro.db_set(
		{
			"logistics_status": "Queued" if retry else "Failed",
			"logistics_attempts": attempts,
			"logistics_next_retry": next_retry,
			"logistics_error": error[:500],
		},
		notify=True,
	)
	frappe.log_error(
		title=f"Reno Order {ro.name}: logistics booking failed (attempt {attempts}{', will retry' if retry else ''})",
		message=error,
		reference_doctype="Reno Order",
		reference_name=ro.name,
	)


def build_payload(ro) -> dict:
	address = (
		frappe.db.get_value(
			"Address", ro.customer_address, ["address_line1", "city", "pincode", "phone"], as_dict=True
		)
		if ro.customer_address
		else None
	)
	return {
		"reference": ro.name,
		"customer": ro.customer_name,
		"delivery_address": address or {},
		"delivery_date": str(ro.expected_installation_date),
		"items": [
			{"item_code": r.item_code, "description": r.item_name, "qty": r.qty, "uom": r.uom}
			for r in ro.items
		],
	}


# --- scheduler (hooks.py: every 5 minutes) ---


def retry_due_bookings():
	if not is_enabled():
		return
	for name in frappe.get_all(
		"Reno Order",
		filters={
			"docstatus": 1,
			"logistics_status": "Queued",
			"logistics_booking_id": ("is", "not set"),
			"logistics_next_retry": ("<=", now_datetime()),
		},
		pluck="name",
	):
		queue_booking(name)


# --- inbound webhook ---


@frappe.whitelist(allow_guest=True, methods=["POST"])
def delivery_webhook():
	"""POST /api/method/reno_order.api.v1.logistics.delivery_webhook

	Body: {"event_id": "...", "booking_id": "...", "event": "dispatched" | "delivered"}
	Header: X-Reno-Signature: sha256=<hex HMAC of the raw body with the webhook secret>
	"""
	raw = frappe.request.get_data() or b""
	verify_signature(raw, frappe.get_request_header("X-Reno-Signature") or "")

	try:
		event = json.loads(raw)
		event_id, booking_id, kind = str(event["event_id"]), str(event["booking_id"]), event["event"]
	except ValueError, KeyError, TypeError:
		frappe.throw(_("Invalid webhook payload."), title=_("Invalid Request"))
	if kind not in WEBHOOK_STATUS:
		frappe.throw(_("Unknown event {0}.").format(kind), title=_("Invalid Request"))

	if frappe.db.exists(
		"Integration Request", {"integration_request_service": SERVICE, "request_id": event_id}
	):
		return {"ok": True, "duplicate": True}  # providers retry webhooks; apply each event once

	reno_order = frappe.db.get_value("Reno Order", {"logistics_booking_id": booking_id}, "name")
	if not reno_order:
		frappe.throw(_("Unknown booking {0}.").format(booking_id), frappe.DoesNotExistError)

	frappe.get_doc(
		{
			"doctype": "Integration Request",
			"integration_request_service": SERVICE,
			"request_description": f"Webhook: {kind}",
			"is_remote_request": 1,
			"request_id": event_id,
			"reference_doctype": "Reno Order",
			"reference_docname": reno_order,
			"data": raw.decode(errors="replace"),
			"status": "Completed",
		}
	).insert(ignore_permissions=True)  # the caller is authenticated by the HMAC, not a user
	frappe.db.set_value("Reno Order", reno_order, "logistics_status", WEBHOOK_STATUS[kind])
	return {"ok": True}


def verify_signature(raw: bytes, header: str):
	secret = get_settings().get_password("webhook_secret", raise_exception=False)
	if not secret:
		frappe.throw(_("Webhook secret is not configured."), frappe.AuthenticationError)
	expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
	if not hmac.compare_digest(expected, header):  # constant time: no timing oracle
		frappe.throw(_("Invalid signature."), frappe.AuthenticationError)
