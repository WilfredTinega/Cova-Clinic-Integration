# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ClinicTestGroup(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		department: DF.Link | None
		designation: DF.Link | None
		employees_per_designation: DF.Int
		enabled: DF.Check
		group_name: DF.Data
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		test_package: DF.Link
	# end: auto-generated types

	pass
