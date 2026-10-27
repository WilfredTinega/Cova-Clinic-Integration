# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

# Controller for the Test Result child table. The tests live in
# test_test_result.py, alongside every other doctype's test module.

from frappe.model.document import Document


class TestResult(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		outcome: DF.Literal["", "FitForWork", "FitWithRestrictions", "InconclusiveRetestRequired", "UnfitForWork"]
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		reason: DF.SmallText | None
		select_tezd: DF.Literal["", "High Risk", "Medium Risk", "Low Risk", "No Risk"]
		test: DF.Link | None
	# end: auto-generated types

	pass
