# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ClinicScheduleEmployee(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		department: DF.Link | None
		designation: DF.Link | None
		employee: DF.Link
		employee_name: DF.Data | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		received_by_cova: DF.Check
		scheduled_from: DF.Date | None
		scheduled_to: DF.Date | None
		test_request: DF.Link | None
	# end: auto-generated types

	pass
