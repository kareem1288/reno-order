# Copyright (c) 2026, Shaik Khaja Kareem and contributors
# For license information, please see license.txt

"""Site Supervisor mobile API (Part 6).

Authentication: every endpoint is whitelisted without allow_guest, so an unauthenticated
call gets HTTP 403 from Frappe before any code here runs. The mobile app authenticates as
the supervisor's own user with a token (User > API Access > Generate Keys):

	Authorization: token <api_key>:<api_secret>

Per-user keys mean every change is attributed to the real person, can be revoked for one
device, and passes through the same permission rules as the desk (DocPerm + the Reno Order
has_permission hook + lifecycle rules). OAuth 2 bearer tokens work the same way if the app
later needs refresh tokens.

Errors come back as Frappe's JSON error envelope with a meaningful status code:
403 PermissionError (not your order / wrong role), 404 DoesNotExistError, 417 ValidationError
(bad transition, bad input) plus a human-readable message in `_server_messages`.
"""

import base64
import binascii

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime

from reno_order.api.v1.lifecycle import transition

MAX_REMARKS_LENGTH = 2000
MAX_PHOTO_BYTES = 10 * 1024 * 1024
# Magic numbers of the image formats phones produce: the extension alone proves nothing.
IMAGE_SIGNATURES = {
	b"\xff\xd8\xff": "jpg",
	b"\x89PNG\r\n\x1a\n": "png",
	b"RIFF": "webp",  # RIFF....WEBP, checked below
}


@frappe.whitelist(methods=["GET"])
def get_my_installations() -> list[dict]:
	"""GET /api/method/reno_order.api.v1.installation.get_my_installations

	Submitted orders assigned to the calling supervisor that are not closed or cancelled.
	"""
	return frappe.get_list(  # get_list, not get_all: applies the permission hooks
		"Reno Order",
		filters={
			"site_supervisor": frappe.session.user,
			"docstatus": 1,
			"status": ("in", ("Confirmed", "In Production", "Ready for Installation", "Installed")),
		},
		fields=[
			"name",
			"customer_name",
			"customer_address",
			"expected_installation_date",
			"status",
			"is_overdue",
		],
		order_by="expected_installation_date asc",
	)


@frappe.whitelist(methods=["POST"])
def update_installation_status(reno_order: str, status: str) -> dict:
	"""POST {"reno_order": "RO-00001", "status": "Installed"}

	All checks (role, assignment, allowed transition, Sales Order submitted) are the
	lifecycle's: the same rules as the desk buttons.
	"""
	_require_order(reno_order)
	if not isinstance(status, str) or not status.strip():
		frappe.throw(_("status is required."), title=_("Invalid Request"))
	ro = transition(reno_order, status.strip())
	return _summary(ro)


@frappe.whitelist(methods=["POST"])
def add_installation_remarks(reno_order: str, remarks: str) -> dict:
	"""POST {"reno_order": "RO-00001", "remarks": "Installation completed successfully."}

	Remarks are appended with a timestamp and author, never overwritten.
	"""
	ro = _require_order(reno_order, for_update=True)
	remarks = cstr(remarks).strip()
	if not remarks:
		frappe.throw(_("remarks cannot be empty."), title=_("Invalid Request"))
	if len(remarks) > MAX_REMARKS_LENGTH:
		frappe.throw(
			_("remarks must be at most {0} characters.").format(MAX_REMARKS_LENGTH),
			title=_("Invalid Request"),
		)

	entry = f"[{now_datetime():%Y-%m-%d %H:%M}] {frappe.session.user}: {remarks}"
	ro.installation_remarks = "\n".join(filter(None, [ro.installation_remarks, entry]))
	ro.save()  # checks permission; validate_editable_after_submit allows remarks for supervisors
	return _summary(ro)


@frappe.whitelist(methods=["POST"])
def upload_installation_photo(
	reno_order: str, filename: str | None = None, filedata: str | None = None
) -> dict:
	"""Attach a site photo. Either multipart/form-data with a `file` part (preferred from a
	phone), or JSON {"reno_order": ..., "filename": "kitchen.jpg", "filedata": "<base64>"}.

	The photo is stored as a private File attached to the Reno Order.
	"""
	ro = _require_order(reno_order)
	ro.check_permission("write")  # same right as editing remarks
	if ro.docstatus != 1:
		frappe.throw(_("Photos can only be added to a submitted order."), title=_("Invalid Request"))

	content, filename = _read_upload(filename, filedata)
	extension = _image_extension(content)
	name = _safe_filename(filename, extension)

	file = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": name,
			"attached_to_doctype": "Reno Order",
			"attached_to_name": ro.name,
			"is_private": 1,
			"content": content,
		}
	)
	# The permission decision was made above against the Reno Order; File's own check would
	# ask for write on the parent through a different path, so it is not repeated here.
	file.flags.ignore_permissions = True
	file.insert()
	ro.add_comment("Attachment", _("Site photo {0} added by {1}").format(file.file_name, frappe.session.user))
	return {"reno_order": ro.name, "file_url": file.file_url, "file_name": file.file_name}


def _require_order(reno_order: str, for_update: bool = False):
	if not isinstance(reno_order, str) or not reno_order.strip():
		frappe.throw(_("reno_order is required."), title=_("Invalid Request"))
	if not frappe.db.exists("Reno Order", reno_order):
		frappe.throw(_("Reno Order {0} not found.").format(reno_order), frappe.DoesNotExistError)
	ro = frappe.get_doc("Reno Order", reno_order, for_update=for_update)
	ro.check_permission("read")  # 403 for an order that is not theirs, same as not existing to them
	return ro


def _read_upload(filename, filedata) -> tuple[bytes, str]:
	files = getattr(frappe.request, "files", None) if frappe.request else None
	if files and "file" in files:
		upload = files["file"]
		content = upload.stream.read(MAX_PHOTO_BYTES + 1)
		filename = filename or upload.filename
	elif filedata:
		try:
			content = base64.b64decode(filedata.split(",", 1)[-1], validate=True)  # allow data: URLs
		except binascii.Error, ValueError:
			frappe.throw(_("filedata is not valid base64."), title=_("Invalid Request"))
	else:
		frappe.throw(
			_("Send the photo as a `file` part or as base64 `filedata`."), title=_("Invalid Request")
		)

	if not content:
		frappe.throw(_("The photo is empty."), title=_("Invalid Request"))
	if len(content) > MAX_PHOTO_BYTES:
		frappe.throw(
			_("The photo is larger than {0} MB.").format(MAX_PHOTO_BYTES // (1024 * 1024)),
			title=_("Invalid Request"),
		)
	return content, filename or "site-photo"


def _image_extension(content: bytes) -> str:
	"""Check the magic number, then make Pillow decode it: a renamed script or a truncated
	upload fails here instead of being stored as a "photo"."""
	from io import BytesIO

	from PIL import Image, UnidentifiedImageError

	for signature, extension in IMAGE_SIGNATURES.items():
		if content.startswith(signature) and (extension != "webp" or content[8:12] == b"WEBP"):
			try:
				with Image.open(BytesIO(content)) as image:
					image.verify()
			except UnidentifiedImageError, OSError, SyntaxError:
				break
			return extension
	frappe.throw(_("Only JPEG, PNG or WebP photos are accepted."), title=_("Invalid Request"))


def _safe_filename(filename: str, extension: str) -> str:
	stem = "".join(c if c.isalnum() or c in "-_" else "-" for c in cstr(filename).rsplit(".", 1)[0])
	return f"{stem.strip('-')[:80] or 'site-photo'}.{extension}"


def _summary(ro) -> dict:
	return {
		"reno_order": ro.name,
		"status": ro.status,
		"installation_remarks": ro.installation_remarks,
		"downstream_status": ro.downstream_status,
	}
