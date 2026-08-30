# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from cova_clinic_integration.member_link import set_cova_member

SICK_LEAVE_TYPE = "Sick Leave (Full Pay)"


class ClinicCheckin(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		cova_member: DF.Link | None
		employee: DF.Link
		employee_payroll_number: DF.Data | None
		end_date: DF.Date | None
		full_name: DF.Data | None
		leave_application: DF.Link | None
		log_type: DF.Literal["", "IN", "OUT"]
		payroll_number: DF.Data | None
		reason: DF.LongText | None
		start_date: DF.Date | None
		time: DF.Datetime | None
		time_in: DF.Datetime | None
		time_out: DF.Datetime | None
	# end: auto-generated types

	def validate(self):
		# Punches identify their employee through b_employee, which set_cova_member
		# handles — see member_link.
		set_cova_member(self)

	def after_insert(self):
		# "Sick Leave Application" — auto-create an approved Sick Leave for the
		# sick-off window and link it back onto this record.
		self.create_sick_leave_application()

	def on_update(self):
		# Also handle records that only get their sick-off window on a later save.
		self.create_sick_leave_application()

	def create_sick_leave_application(self):
		# Only sick-off records carry a start/end window; biometric punches (or any
		# payload without both dates) must NOT trigger a Leave Application.
		if not (self.employee and self.start_date and self.end_date):
			return

		existing_leave = frappe.db.exists(
			"Leave Application",
			{
				"employee": self.employee,
				"from_date": self.start_date,
				"to_date": self.end_date,
				"leave_type": SICK_LEAVE_TYPE,
			},
		)
		if existing_leave:
			# Idempotent: leave already exists for this window — just keep the link.
			if not self.leave_application:
				self.db_set("leave_application", existing_leave)
			return

		try:
			employee = frappe.get_doc("Employee", self.employee)

			balance_result = frappe.db.sql(
				"""
				SELECT
					la.total_leaves_allocated - COALESCE(
						(
							SELECT SUM(lle.leaves)
							FROM `tabLeave Ledger Entry` lle
							WHERE lle.employee = la.employee
							AND lle.leave_type = la.leave_type
							AND lle.docstatus = 1
							AND lle.transaction_type = 'Leave Application'
							AND lle.from_date >= la.from_date
							AND lle.to_date <= la.to_date
						), 0
					) as balance
				FROM `tabLeave Allocation` la
				WHERE la.employee = %(employee)s
				AND la.leave_type = %(leave_type)s
				AND %(date)s BETWEEN la.from_date AND la.to_date
				AND la.docstatus = 1
				ORDER BY la.from_date DESC
				LIMIT 1
			""",
				{
					"employee": self.employee,
					"leave_type": SICK_LEAVE_TYPE,
					"date": str(self.start_date),
				},
			)

			available_balance = (
				balance_result[0][0]
				if balance_result and balance_result[0][0] is not None
				else 0
			)

			leave = frappe.new_doc("Leave Application")
			leave.employee = self.employee
			leave.employee_name = employee.employee_name
			leave.company = employee.company
			leave.department = employee.department
			leave.leave_type = SICK_LEAVE_TYPE
			leave.from_date = self.start_date
			leave.to_date = self.end_date
			leave.posting_date = frappe.utils.nowdate()
			leave.description = self.reason
			leave.leave_approver = employee.leave_approver
			leave.leave_approver_name = employee.leave_approver
			leave.status = "Approved"
			leave.leave_balance = available_balance
			leave.flags.ignore_permissions = True

			leave.insert(ignore_permissions=True)
			leave.submit()
			# workflow_state only exists as a column where a Workflow has been
			# defined on Leave Application. It has been on the live site since
			# forever, but writing it unconditionally makes this whole block die
			# with "Unknown column 'workflow_state'" on a site that has none —
			# taking the leave_balance write and the link-back down with it.
			if frappe.db.has_column("Leave Application", "workflow_state"):
				leave.db_set("workflow_state", "Approved by HR")
			leave.db_set("leave_balance", available_balance)

			self.db_set("leave_application", leave.name)

		except Exception:
			frappe.log_error(title="Sick Leave - FAILED", message=frappe.get_traceback())
