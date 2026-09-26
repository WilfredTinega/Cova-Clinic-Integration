# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ClinicDashboardViewer(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		full_name: DF.Data | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		user: DF.Link
		view_accidents: DF.Check
		view_biometric: DF.Check
		view_health: DF.Check
		view_overview: DF.Check
		view_requests: DF.Check
		view_results: DF.Check
		view_schedules: DF.Check
		view_sickoff: DF.Check
		view_tickets: DF.Check
		view_visits: DF.Check
	# end: auto-generated types

	pass
