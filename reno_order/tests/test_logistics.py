# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# See license.txt

"""Parts 7-8: logistics booking job and delivery webhook."""

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

from reno_order.api.v1 import logistics
from reno_order.tests.utils import make_reno_order

SECRET = "test-webhook-secret"


def response(status=200, body=None):
	r = MagicMock()
	r.status_code = status
	r.ok = 200 <= status < 300
	r.json.return_value = body if body is not None else {"booking_id": "LGX-1001"}
	r.text = json.dumps(r.json.return_value)
	return r


class TestLogistics(IntegrationTestCase):
	def setUp(self):
		settings = frappe.get_single("Reno Logistics Settings")
		settings.update(
			{
				"enabled": 1,
				"base_url": "https://logistics.example.com/v1",
				"api_key": "test-api-key",
				"webhook_secret": SECRET,
				"connect_timeout": 5,
				"read_timeout": 30,
				"max_attempts": 3,
			}
		)
		settings.save()
		frappe.clear_document_cache("Reno Logistics Settings", "Reno Logistics Settings")
		self.ro = make_reno_order().name

	def ro_values(self):
		return frappe.db.get_value(
			"Reno Order",
			self.ro,
			[
				"logistics_status",
				"logistics_booking_id",
				"logistics_attempts",
				"logistics_next_retry",
				"logistics_error",
			],
			as_dict=True,
		)

	# --- outbound booking ---

	@patch("reno_order.api.v1.logistics.requests.post", return_value=response())
	def test_booking_success(self, post):
		self.assertEqual(logistics.book_delivery(self.ro), "LGX-1001")
		values = self.ro_values()
		self.assertEqual((values.logistics_status, values.logistics_booking_id), ("Booked", "LGX-1001"))

		kwargs = post.call_args.kwargs
		self.assertEqual(post.call_args.args[0], "https://logistics.example.com/v1/bookings")
		self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-api-key")
		self.assertEqual(kwargs["headers"]["Idempotency-Key"], self.ro)
		self.assertEqual(kwargs["timeout"], (5, 30))
		self.assertEqual(kwargs["json"]["reference"], self.ro)

		log = frappe.get_last_doc("Integration Request", {"reference_docname": self.ro})
		self.assertEqual(log.status, "Completed")

	@patch("reno_order.api.v1.logistics.requests.post", side_effect=requests.Timeout("read timed out"))
	def test_timeout_is_retried_with_backoff(self, post):
		self.assertIsNone(logistics.book_delivery(self.ro))
		values = self.ro_values()
		self.assertEqual((values.logistics_status, values.logistics_attempts), ("Queued", 1))
		self.assertTrue(values.logistics_next_retry)
		self.assertIn("Timeout", values.logistics_error)
		self.assertEqual(
			frappe.get_last_doc("Integration Request", {"reference_docname": self.ro}).status, "Failed"
		)

	@patch("reno_order.api.v1.logistics.requests.post", return_value=response(503, {"error": "busy"}))
	def test_server_error_is_retried(self, post):
		logistics.book_delivery(self.ro)
		self.assertEqual(self.ro_values().logistics_status, "Queued")

	@patch("reno_order.api.v1.logistics.requests.post", return_value=response(422, {"error": "bad pincode"}))
	def test_client_error_fails_without_retry(self, post):
		logistics.book_delivery(self.ro)
		values = self.ro_values()
		self.assertEqual(values.logistics_status, "Failed")
		self.assertFalse(values.logistics_next_retry)

	@patch("reno_order.api.v1.logistics.requests.post", side_effect=requests.ConnectionError("refused"))
	def test_gives_up_after_max_attempts(self, post):
		for _ in range(3):
			logistics.book_delivery(self.ro)
		values = self.ro_values()
		self.assertEqual((values.logistics_status, values.logistics_attempts), ("Failed", 3))

	@patch("reno_order.api.v1.logistics.requests.post", return_value=response())
	def test_existing_booking_is_not_booked_again(self, post):
		logistics.book_delivery(self.ro)
		logistics.book_delivery(self.ro)  # duplicate / late job
		self.assertEqual(post.call_count, 1)

	def test_queue_uses_long_queue_and_dedup(self):
		with patch("frappe.enqueue") as enqueue:
			logistics.queue_booking(self.ro)
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(kwargs["queue"], "long")
		self.assertEqual(kwargs["job_id"], f"reno_order_logistics_booking::{self.ro}")
		self.assertTrue(kwargs["deduplicate"] and kwargs["enqueue_after_commit"])

	def test_disabled_integration_does_nothing(self):
		frappe.db.set_single_value("Reno Logistics Settings", "enabled", 0)
		frappe.clear_document_cache("Reno Logistics Settings", "Reno Logistics Settings")
		with patch("frappe.enqueue") as enqueue, patch("reno_order.api.v1.logistics.requests.post") as post:
			logistics.queue_booking(self.ro)
			self.assertIsNone(logistics.book_delivery(self.ro))
		enqueue.assert_not_called()
		post.assert_not_called()

	def test_api_key_is_stored_encrypted(self):
		stored = frappe.db.get_single_value("Reno Logistics Settings", "api_key")
		self.assertNotEqual(stored, "test-api-key")  # the column holds a mask, the secret is in __Auth

	# --- inbound webhook ---

	def call_webhook(self, body: dict, signature: str | None = None):
		raw = json.dumps(body).encode()
		signature = signature or "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
		request = MagicMock()
		request.get_data.return_value = raw
		with (
			patch.object(frappe.local, "request", request, create=True),
			patch("frappe.get_request_header", return_value=signature),
		):
			return logistics.delivery_webhook()

	def booked(self):
		frappe.db.set_value(
			"Reno Order", self.ro, {"logistics_booking_id": "LGX-2002", "logistics_status": "Booked"}
		)

	def test_webhook_updates_status(self):
		self.booked()
		out = self.call_webhook({"event_id": "evt-1", "booking_id": "LGX-2002", "event": "delivered"})
		self.assertEqual(out, {"ok": True})
		self.assertEqual(self.ro_values().logistics_status, "Delivered")

	def test_webhook_rejects_bad_signature(self):
		self.booked()
		with self.assertRaises(frappe.AuthenticationError):
			self.call_webhook(
				{"event_id": "evt-2", "booking_id": "LGX-2002", "event": "delivered"}, "sha256=bad"
			)
		self.assertEqual(self.ro_values().logistics_status, "Booked")

	def test_webhook_event_applied_once(self):
		self.booked()
		self.call_webhook({"event_id": "evt-3", "booking_id": "LGX-2002", "event": "dispatched"})
		frappe.db.set_value("Reno Order", self.ro, "logistics_status", "Delivered")
		out = self.call_webhook({"event_id": "evt-3", "booking_id": "LGX-2002", "event": "dispatched"})
		self.assertTrue(out["duplicate"])
		self.assertEqual(self.ro_values().logistics_status, "Delivered")  # not rolled back to Dispatched

	def test_webhook_unknown_booking(self):
		with self.assertRaises(frappe.DoesNotExistError):
			self.call_webhook({"event_id": "evt-4", "booking_id": "LGX-404", "event": "delivered"})
