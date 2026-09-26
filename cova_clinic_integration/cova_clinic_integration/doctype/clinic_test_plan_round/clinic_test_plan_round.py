# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ClinicTestPlanRound(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		clinic_test_schedule: DF.Link | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		planned_employees: DF.Int
		round_no: DF.Int
		scheduled_employees: DF.Int
		scheduled_from: DF.Date
		scheduled_to: DF.Date
		skipped_on_leave: DF.Int
		status: DF.Literal["Planned", "Scheduled"]
	# end: auto-generated types

	pass
