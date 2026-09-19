# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class TestPackage(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		cova_code: DF.Data | None
		description: DF.SmallText | None
		disabled: DF.Check
		package_name: DF.Data
	# end: auto-generated types

	def validate(self):
		# The name IS the package_name (autoname field:package_name), and it is
		# what every Clinic Test Request row already holds as plain text. A
		# trailing space would mint a second package that reads identically to
		# the first in every list and dropdown.
		self.package_name = (self.package_name or "").strip()


def cova_package_code(package: str) -> str:
	"""What COVA is told the package is.

	Historically the app sent ``test_package.replace(" ", "")`` — "Annual Medical"
	went out as "AnnualMedical". That rule is kept for every package that does not
	override it, so nothing on the wire changes by introducing this doctype; a
	package whose COVA code is not simply its de-spaced name (an "X-Ray" is not
	necessarily an "X-Ray" to them) fills in ``cova_code`` instead.
	"""
	if not package:
		return ""

	code = frappe.db.get_value("Test Package", package, "cova_code")
	return (code or package).replace(" ", "")
