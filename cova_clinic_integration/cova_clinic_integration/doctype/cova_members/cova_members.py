# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document
from frappe.model.naming import append_number_if_name_exists, make_autoname


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
		gender: DF.Link | None
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

	def autoname(self):
		"""Name the member after whoever it identifies.

		The doctype used to declare ``{full_name}.-.{national_id}.-.{employee}``
		under the "Expression" naming rule, but that rule wants a ``format:``
		prefix — frappe matched no rule at all and every row fell through to a
		hash (``mv2picvuc9``).

		A plain ``format:`` string does not fit either, because the two member
		types carry different identifiers and the blank one would leave a dangling
		separator:

		* Active         - Employee + payroll number, national id usually empty
		* Pre Employment - national id, no Employee yet

		So the name is joined from the parts that are actually present.
		"""
		identifier = self.national_id or self.payroll_number or self.employee
		parts = [str(p).strip() for p in (self.full_name, identifier) if p and str(p).strip()]

		if not parts:
			# Nothing to name it after — a hash is still better than failing the insert.
			self.name = make_autoname("hash", self.doctype)
			return

		self.name = append_number_if_name_exists(self.doctype, " - ".join(parts))
