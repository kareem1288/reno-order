# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# See license.txt

"""Part 6: Site Supervisor mobile API."""

import base64
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from reno_order.api.v1 import delivery_note, installation
from reno_order.api.v1.lifecycle import transition
from reno_order.tests.utils import (
	RENO_ORDER_LINKED_DOCTYPES,
	make_reno_order,
	make_user,
	submit_sales_order_for,
)

IGNORE_TEST_RECORD_DEPENDENCIES = RENO_ORDER_LINKED_DOCTYPES

SUPERVISOR = "reno.api.supervisor@example.com"
OTHER_SUPERVISOR = "reno.api.supervisor2@example.com"
SALES = "reno.api.sales@example.com"


def _image(fmt: str) -> bytes:
	from io import BytesIO

	from PIL import Image

	buf = BytesIO()
	Image.new("RGB", (8, 8), "orange").save(buf, format=fmt)
	return buf.getvalue()


PNG = _image("PNG")
JPEG = _image("JPEG")


class TestInstallationAPI(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user(SUPERVISOR, ["Site Supervisor"])
		make_user(OTHER_SUPERVISOR, ["Site Supervisor"])
		make_user(SALES, ["Sales User"])

	def setUp(self):
		patcher = patch.object(delivery_note, "queue_delivery_note")
		patcher.start()
		self.addCleanup(patcher.stop)
		ro = make_reno_order(site_supervisor=SUPERVISOR)
		submit_sales_order_for(ro)
		transition(ro.name, "Ready for Installation")
		self.ro = ro.name

	def tearDown(self):
		frappe.set_user("Administrator")

	# --- authentication / authorisation ---

	def test_guest_cannot_call_any_endpoint(self):
		frappe.set_user("Guest")
		for fn in (
			installation.get_my_installations,
			installation.update_installation_status,
			installation.add_installation_remarks,
			installation.upload_installation_photo,
		):
			with self.assertRaises(frappe.PermissionError, msg=fn.__name__):
				frappe.is_whitelisted(fn)

	def test_other_supervisor_is_refused(self):
		frappe.set_user(OTHER_SUPERVISOR)
		self.assertRaises(
			frappe.PermissionError, installation.update_installation_status, self.ro, "Installed"
		)
		self.assertRaises(frappe.PermissionError, installation.add_installation_remarks, self.ro, "hi")
		self.assertRaises(
			frappe.PermissionError,
			installation.upload_installation_photo,
			self.ro,
			"a.png",
			base64.b64encode(PNG).decode(),
		)

	def test_unknown_order_is_404(self):
		frappe.set_user(SUPERVISOR)
		self.assertRaises(frappe.DoesNotExistError, installation.add_installation_remarks, "RO-99999", "x")

	# --- status ---

	def test_supervisor_marks_installed(self):
		frappe.set_user(SUPERVISOR)
		out = installation.update_installation_status(self.ro, "Installed")
		self.assertEqual((out["status"], out["downstream_status"]), ("Installed", "Queued"))

	def test_invalid_transition_is_rejected(self):
		frappe.set_user(SUPERVISOR)
		self.assertRaises(frappe.ValidationError, installation.update_installation_status, self.ro, "Closed")
		self.assertRaises(frappe.ValidationError, installation.update_installation_status, self.ro, "")

	def test_supervisor_lists_own_installations(self):
		make_reno_order(site_supervisor=OTHER_SUPERVISOR)
		frappe.set_user(SUPERVISOR)
		names = [r.name for r in installation.get_my_installations()]
		self.assertIn(self.ro, names)
		self.assertTrue(
			all(frappe.db.get_value("Reno Order", n, "site_supervisor") == SUPERVISOR for n in names)
		)

	# --- remarks ---

	def test_remarks_are_appended(self):
		frappe.set_user(SUPERVISOR)
		installation.add_installation_remarks(self.ro, "Cabinets delivered.")
		out = installation.add_installation_remarks(self.ro, "Installation completed successfully.")
		lines = out["installation_remarks"].splitlines()
		self.assertEqual(len(lines), 2)
		self.assertIn(SUPERVISOR, lines[1])
		self.assertTrue(lines[1].endswith("Installation completed successfully."))

	def test_remarks_are_validated(self):
		frappe.set_user(SUPERVISOR)
		self.assertRaises(frappe.ValidationError, installation.add_installation_remarks, self.ro, "   ")
		self.assertRaises(frappe.ValidationError, installation.add_installation_remarks, self.ro, "x" * 2001)

	# --- photos ---

	def test_photo_upload(self):
		frappe.set_user(SUPERVISOR)
		out = installation.upload_installation_photo(self.ro, "site 1.jpg", base64.b64encode(JPEG).decode())
		file = frappe.get_doc("File", {"file_url": out["file_url"]})
		self.assertEqual((file.attached_to_doctype, file.attached_to_name), ("Reno Order", self.ro))
		self.assertTrue(file.is_private)
		self.assertEqual(file.file_name, "site-1.jpg")

	def test_photo_must_be_an_image(self):
		frappe.set_user(SUPERVISOR)
		fake = base64.b64encode(b"<?php echo 'hi'; ?>").decode()
		self.assertRaises(
			frappe.ValidationError, installation.upload_installation_photo, self.ro, "x.jpg", fake
		)
		# right magic number, not an image
		truncated = base64.b64encode(JPEG[:12]).decode()
		self.assertRaises(
			frappe.ValidationError, installation.upload_installation_photo, self.ro, "x.jpg", truncated
		)
		self.assertRaises(
			frappe.ValidationError, installation.upload_installation_photo, self.ro, "x.jpg", "%%%"
		)

	def test_photo_size_limit(self):
		frappe.set_user(SUPERVISOR)
		with patch.object(installation, "MAX_PHOTO_BYTES", 32):
			self.assertRaises(
				frappe.ValidationError,
				installation.upload_installation_photo,
				self.ro,
				"big.png",
				base64.b64encode(PNG).decode(),
			)
