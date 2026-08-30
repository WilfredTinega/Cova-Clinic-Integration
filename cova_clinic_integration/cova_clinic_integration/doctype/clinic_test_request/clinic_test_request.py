# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from cova_clinic_integration.member_link import set_cova_member


class ClinicTestRequest(Document):

	def validate(self):
		# Keeps the member's Connections tab complete — see member_link.
		set_cova_member(self)
		self.set_payroll_number()

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
