# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from cova_clinic_integration.member_link import set_cova_member


class ClinicTestRequest(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		clinic_test_schedule: DF.Link | None
		cova_member: DF.Link | None
		cova_raw: DF.LongText | None
		date_of_birth: DF.Date | None
		department: DF.Link | None
		designation: DF.Link | None
		employee: DF.Link | None
		gender: DF.Link | None
		linked_test_result: DF.Link | None
		member_type: DF.Literal["", "Active", "Pre Employment"]
		nationa_id: DF.Data | None
		notes: DF.SmallText | None
		payroll_number: DF.Data | None
		phone_number: DF.Data | None
		received_by_cova: DF.Check
		scheduled_from: DF.Date
		scheduled_to: DF.Date
		status: DF.Literal["", "Pending", "Completed", "Cancelled"]
		test_group: DF.Data | None
		test_package: DF.Link
	# end: auto-generated types

	def validate(self):
		# Keeps the member's Connections tab complete — see member_link.
		set_cova_member(self)
		self.set_payroll_number()
		self.validate_single_preemployment_request()

	def validate_single_preemployment_request(self):
		"""Pre-employment is locked to one test, so a candidate gets one request.

		Re-registering a Job Offer, or raising one by hand, used to add a second
		request for the same National ID. A cancelled request does not count.
		"""
		if self.member_type != "Pre Employment" or not self.nationa_id:
			return
		self.nationa_id = self.nationa_id.strip()
		existing = existing_preemployment_request(self.nationa_id, exclude=self.name)
		if existing:
			frappe.throw(
				frappe._("A pre-employment test request already exists for National ID {0}: {1}").format(
					frappe.bold(self.nationa_id), frappe.utils.get_link_to_form(self.doctype, existing)
				),
				frappe.DuplicateEntryError,
			)

	def set_payroll_number(self):
		"""Make sure an Active request carries the identifier COVA knows.

		``payroll_number`` fetches from ``employee.employee_number``, which is
		**optional** in HR — so a request raised from the Employee form's
		*Request Medical Test* button (or from the list, or by hand) came out with
		an empty payroll number wherever that field is unset, and
		``submit_test_request`` then sent COVA ``memberIdentifier: ""``.

		The API path never had the problem: it fills the field with
		``employee_payroll_id()``, which falls back to the Employee ID. This puts
		every route on the same rule. Only an empty value is filled, so a fetched
		``employee_number`` still wins — inbound resolution accepts either.
		"""
		if self.member_type != "Active" or not self.employee or self.payroll_number:
			return

		# Imported here rather than at module level: api.py is a heavy module and
		# nothing else in this controller needs it.
		from cova_clinic_integration.api import employee_payroll_id

		self.payroll_number = employee_payroll_id(frappe.get_doc("Employee", self.employee))


def existing_preemployment_request(national_id, exclude=None) -> str | None:
	"""The candidate's live (not cancelled) pre-employment request, if any."""
	if not national_id:
		return None
	filters = {
		"member_type": "Pre Employment",
		"nationa_id": national_id.strip(),
		"status": ["!=", "Cancelled"],
	}
	if exclude:
		filters["name"] = ["!=", exclude]
	rows = frappe.get_all(
		"Clinic Test Request", filters=filters, pluck="name", order_by="creation asc", limit=1
	)
	return rows[0] if rows else None
