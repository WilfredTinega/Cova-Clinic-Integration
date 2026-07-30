# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class ClinicTestResult(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from cova_clinic_integration.cova_clinic_integration.doctype.test_result.test_result import TestResult
		from frappe.types import DF

		clinic_visit_reference: DF.Link | None
		clinical_outcome: DF.Data | None
		cova_raw: DF.LongText | None
		employee: DF.Link | None
		full_name: DF.Data | None
		member_type: DF.Literal["", "Active", "Pre Employment"]
		payroll_number: DF.Data | None
		request_id: DF.Data | None
		results: DF.Table[TestResult]
		test_package: DF.Data | None
	# end: auto-generated types

	pass
