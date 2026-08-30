# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import flt

from cova_clinic_integration.member_link import set_cova_member


class ClinicVisitCost(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from cova_clinic_integration.cova_clinic_integration.doctype.visit_line_item.visit_line_item import VisitLineItem
		from frappe.types import DF

		benefit_consultation: DF.Currency
		benefit_diagnostic: DF.Currency
		benefit_laboratory: DF.Currency
		benefit_pharmacy: DF.Currency
		benefit_specialist: DF.Currency
		candidate_name: DF.Data | None
		cova_member: DF.Link | None
		cova_raw: DF.LongText | None
		employee: DF.Link
		full_name: DF.Data | None
		payroll_number: DF.Data | None
		total_cost: DF.Currency
		visit_date: DF.Date
		visit_line_item: DF.Table[VisitLineItem]
	# end: auto-generated types

	def validate(self):
		# Keeps the member's Connections tab complete — see member_link.
		set_cova_member(self)
		self.set_payroll_number()
		self.set_total_cost()

	def set_payroll_number(self):
		"""Same rule as Clinic Test Request: `employee_number` is optional in HR,
		so a visit entered by hand would carry a blank payroll number even though
		every visit COVA pushes in has one."""
		if not self.employee or self.payroll_number:
			return

		from cova_clinic_integration.api import employee_payroll_id

		self.payroll_number = employee_payroll_id(frappe.get_doc("Employee", self.employee))

	def set_total_cost(self):
		"""Total is derived from the line items, never typed.

		The API set it itself before inserting; a visit entered or corrected by
		hand had no one to add it up, so the read-only total stayed at 0 however
		many line items were on it. Summing here covers both routes — COVA's
		payload produces the same number it always did."""
		if not self.get("visit_line_item"):
			return
		self.total_cost = sum(flt(row.cost) for row in self.visit_line_item)
