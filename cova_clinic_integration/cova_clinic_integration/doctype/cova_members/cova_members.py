# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class CovaMembers(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		branch_code: DF.Data | None
		company_code: DF.Data | None
		cova_member_id: DF.Data | None
		cova_raw: DF.LongText | None
		date_of_birth: DF.Datetime | None
		employee: DF.Link | None
		full_name: DF.Data | None
		gender: DF.Literal["", "Male", "Female"]
		last_visit: DF.Date | None
		member_type: DF.Literal["", "Active", "Pre Employment"]
		national_id: DF.Data | None
		package_code: DF.Data | None
		patient_id: DF.Data | None
		payroll_number: DF.Data | None
		phone_number: DF.Phone | None
		status: DF.Literal["", "Active", "Inactive"]
		test_request_reference: DF.Link | None
		test_result_reference: DF.Link | None
		visit_reference: DF.Link | None
		wallet_id: DF.Data | None
		wallet_reference: DF.Data | None
	# end: auto-generated types

	pass
