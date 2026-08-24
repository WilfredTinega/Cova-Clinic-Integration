# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class HealthMonthlyReport(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from cova_clinic_integration.cova_clinic_integration.doctype.health_report.health_report import (
			HealthReport,
		)

		medical_cases: DF.Table[HealthReport]
		month: DF.Literal["", "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
		posting_date: DF.Date | None
		total_cases: DF.Int
	# end: auto-generated types

	def validate(self):
		# COVA's webhook sends the total alongside the rows, but a report typed in
		# by hand (or edited afterwards) must stay consistent with its children.
		self.month = (self.month or "").upper()
		self.total_cases = sum((row.case_count or 0) for row in self.medical_cases)

		seen = set()
		for row in self.medical_cases:
			if row.medical_case in seen:
				frappe.throw(
					frappe._("Medical Case {0} is listed more than once.").format(
						frappe.bold(row.medical_case)
					)
				)
			seen.add(row.medical_case)
