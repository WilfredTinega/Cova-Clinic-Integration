# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, date_diff, flt, getdate

from cova_clinic_integration.clinic_log import ERROR, SUCCESS, WARNING, log_clinic
from cova_clinic_integration.cova_clinic_integration.doctype.clinic_ticket.clinic_ticket import mark_visited
from cova_clinic_integration.member_link import set_cova_member

SICK_LEAVE_TYPE = "Sick Leave (Full Pay)"
HALF_PAY_SICK_LEAVE_TYPE = "Sick Leave (Half Pay)"


class ClinicCheckin(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		cova_member: DF.Link | None
		cova_raw: DF.LongText | None
		employee: DF.Link
		employee_payroll_number: DF.Data | None
		end_date: DF.Date | None
		facility: DF.Data | None
		full_name: DF.Data | None
		is_external: DF.Check
		leave_application: DF.Link | None
		log_type: DF.Literal["", "IN", "OUT"]
		payroll_number: DF.Data | None
		reason: DF.LongText | None
		sick_off_given: DF.Int
		start_date: DF.Date | None
		time: DF.Datetime | None
		time_in: DF.Datetime | None
		time_out: DF.Datetime | None
	# end: auto-generated types

	def validate(self):
		# Punches and sick-offs both name their person in `employee`; what tells
		# them apart is the payload. See member_link for the lookup.
		set_cova_member(self)
		self.validate_external_facility()

	def validate_external_facility(self):
		# Facility and the sick off it gave only mean something for an external
		# visit; clear them otherwise so a stale value never reads as one.
		if not self.is_external:
			self.facility = None
			self.sick_off_given = 0
			return
		if not (self.facility or "").strip() and not self.flags.from_clinic_post:
			frappe.throw(frappe._("Facility is required for an external clinic check-in."))
		if (self.sick_off_given or 0) < 0:
			frappe.throw(frappe._("Sick Off Given cannot be negative."))

	def after_insert(self):
		# "Sick Leave Application" — auto-create an approved Sick Leave for the
		# sick-off window and link it back onto this record.
		self.create_sick_leave_application()
		# The employee has reached the clinic: use up the ticket that sent them.
		mark_visited(self)

	def on_update(self):
		# Also handle records that only get their sick-off window on a later save.
		self.create_sick_leave_application()

	def create_sick_leave_application(self):
		# Only sick-off records carry a start/end window; biometric punches (or any
		# payload without both dates) must NOT trigger a Leave Application.
		if not (self.employee and self.start_date and self.end_date):
			return

		# Days already on leave are left alone (HRMS refuses an overlap); the rest
		# are filed as one application per unbroken run.
		runs = sick_off_runs(self.employee, self.start_date, self.end_date)
		if not runs:
			# Idempotent: the window is already covered — just keep the link.
			if not self.leave_application:
				existing_leave = existing_sick_leave(self.employee, self.start_date, self.end_date)
				if existing_leave:
					self.db_set("leave_application", existing_leave)
			return

		employee = frappe.get_doc("Employee", self.employee)
		for from_date, to_date in runs:
			window = "{0} to {1}".format(from_date, to_date)
			if not has_sick_allocation(self.employee, from_date):
				# HRMS will not take sick leave outside an allocation, so park it as
				# a draft for HR to submit once the allocation exists. Attendance
				# stays as it is until then; submitting flips the days to On Leave.
				frappe.db.savepoint("clinic_sick_off")
				try:
					leave_name = self.make_draft_sick_leave(employee, from_date, to_date)
				except Exception as e:
					frappe.db.rollback(save_point="clinic_sick_off")
					frappe.log_error(title="Sick Leave - FAILED", message=frappe.get_traceback())
					self.log_sick_leave(ERROR, "Draft sick leave for {0} not created: {1}".format(window, e))
					continue
				self.log_sick_leave(
					WARNING, "No sick-leave allocation: draft {0} for {1}".format(leave_name, window)
				)
				if not self.leave_application:
					self.db_set("leave_application", leave_name)
				continue

			# A backdated window is already marked — Present where the employee
			# clocked in before going to the clinic (HRMS refuses leave over those),
			# Absent for the rest. The sick off wins: cancel that attendance and let
			# the leave mark every day On Leave. If the leave still fails, the
			# cancellation is undone.
			frappe.db.savepoint("clinic_sick_off")
			try:
				cancel_window_attendance(self.employee, from_date, to_date)
				leave_name = self.make_sick_leave(employee, from_date, to_date)
			except Exception as e:
				frappe.db.rollback(save_point="clinic_sick_off")
				frappe.log_error(title="Sick Leave - FAILED", message=frappe.get_traceback())
				self.log_sick_leave(ERROR, "Sick leave for {0} not filed: {1}".format(window, e))
				continue
			self.log_sick_leave(SUCCESS, "Sick leave {0} filed for {1}".format(leave_name, window))
			if not self.leave_application:
				self.db_set("leave_application", leave_name)

	def log_sick_leave(self, status, message):
		log_clinic(
			"sick_leave",
			status,
			message=message,
			employee=self.employee,
			reference_doctype=self.doctype,
			reference_name=self.name,
		)

	def make_draft_sick_leave(self, employee, from_date, to_date):
		approver = resolve_leave_approver(employee)
		leave = frappe.new_doc("Leave Application")
		leave.update(
			{
				"employee": employee.name,
				"employee_name": employee.employee_name,
				"company": employee.company,
				"department": employee.department,
				"leave_type": SICK_LEAVE_TYPE,
				"from_date": from_date,
				"to_date": to_date,
				"total_leave_days": date_diff(to_date, from_date) + 1,
				"posting_date": frappe.utils.nowdate(),
				"description": self.reason,
				"leave_approver": approver,
				"leave_approver_name": frappe.utils.get_fullname(approver) if approver else None,
				"status": "Open",
			}
		)
		leave.flags.ignore_permissions = True
		# HRMS's own validate refuses a period outside any allocation; that is
		# exactly the gap this draft is waiting on, and submitting re-runs it.
		leave.flags.ignore_validate = True
		leave.insert(ignore_permissions=True)
		leave.add_comment(
			"Comment",
			"No Sick Leave allocation covers {0} to {1} for {2}. Created as a draft from clinic "
			"sick off {3}: allocate sick leave, then submit.".format(
				from_date, to_date, employee.employee_name or employee.name, self.name
			),
		)
		return leave.name

	def make_sick_leave(self, employee, from_date, to_date):
		leave_type, available_balance = pick_sick_leave_type(employee.name, from_date, to_date)

		leave = frappe.new_doc("Leave Application")
		leave.employee = employee.name
		leave.employee_name = employee.employee_name
		leave.company = employee.company
		leave.department = employee.department
		leave.leave_type = leave_type
		leave.from_date = from_date
		leave.to_date = to_date
		leave.posting_date = frappe.utils.nowdate()
		leave.description = self.reason
		approver = resolve_leave_approver(employee)
		leave.leave_approver = approver
		leave.leave_approver_name = frappe.utils.get_fullname(approver) if approver else None
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
		return leave.name


def has_sick_allocation(employee, date) -> bool:
	return bool(
		frappe.db.exists(
			"Leave Allocation",
			{
				"employee": employee,
				"leave_type": ["in", [SICK_LEAVE_TYPE, HALF_PAY_SICK_LEAVE_TYPE]],
				"docstatus": 1,
				"from_date": ["<=", date],
				"to_date": [">=", date],
			},
		)
	)


def cancel_window_attendance(employee, from_date, to_date):
	"""Cancel the submitted attendance in the window — Present, Absent, Half Day,
	whatever the day was marked as — so the sick off is what stands for it."""
	for name in frappe.get_all(
		"Attendance",
		filters={
			"employee": employee,
			"attendance_date": ["between", [from_date, to_date]],
			"status": ["!=", "On Leave"],
			"docstatus": 1,
		},
		pluck="name",
	):
		attendance = frappe.get_doc("Attendance", name)
		attendance.flags.ignore_permissions = True
		attendance.cancel()


def sick_off_runs(employee, start_date, end_date) -> list[tuple]:
	"""The unbroken stretches of ``start_date``..``end_date`` not already inside
	an Open / Approved Leave Application (``validate_leave_overlap``)."""
	start_date, end_date = getdate(start_date), getdate(end_date)

	taken = set()
	for leave in frappe.get_all(
		"Leave Application",
		filters={
			"employee": employee,
			"docstatus": ["<", 2],
			"status": ["in", ["Open", "Approved"]],
			"from_date": ["<=", end_date],
			"to_date": [">=", start_date],
		},
		fields=["from_date", "to_date"],
	):
		day = max(getdate(leave.from_date), start_date)
		while day <= min(getdate(leave.to_date), end_date):
			taken.add(day)
			day = add_days(day, 1)

	runs, run_start, day = [], None, start_date
	while day <= end_date:
		if day in taken:
			if run_start:
				runs.append((run_start, add_days(day, -1)))
				run_start = None
		elif not run_start:
			run_start = day
		day = add_days(day, 1)
	if run_start:
		runs.append((run_start, end_date))
	return runs


def existing_sick_leave(employee, start_date, end_date) -> str | None:
	rows = frappe.get_all(
		"Leave Application",
		filters={
			"employee": employee,
			"leave_type": ["in", [SICK_LEAVE_TYPE, HALF_PAY_SICK_LEAVE_TYPE]],
			"docstatus": ["<", 2],
			"status": ["in", ["Open", "Approved"]],
			"from_date": ["<=", end_date],
			"to_date": [">=", start_date],
		},
		pluck="name",
		order_by="from_date asc",
		limit=1,
	)
	return rows[0] if rows else None


def pick_sick_leave_type(employee, from_date, to_date) -> tuple[str, float]:
	"""Full pay while the balance covers the stretch, half pay once it does not.

	Both sick types refuse a negative balance, so a stretch longer than what is
	left on full pay was simply lost. Same rule the clinic's /sick-off page
	applies; the whole stretch goes on one type, it is not split.
	"""
	days = date_diff(to_date, from_date) + 1
	full_pay = sick_leave_balance(employee, SICK_LEAVE_TYPE, from_date)
	if full_pay >= days or not frappe.db.exists("Leave Type", HALF_PAY_SICK_LEAVE_TYPE):
		return SICK_LEAVE_TYPE, full_pay
	half_pay = sick_leave_balance(employee, HALF_PAY_SICK_LEAVE_TYPE, from_date)
	if half_pay >= days:
		return HALF_PAY_SICK_LEAVE_TYPE, half_pay
	return SICK_LEAVE_TYPE, full_pay


def sick_leave_balance(employee, leave_type, date) -> float:
	"""What is left on the allocation covering ``date``, read off the leave ledger.

	Allocations post positive entries and applications negative ones, so the
	sum inside the allocation's period is the balance. hrms' own balance helpers
	check the session user's access to the employee, which an API check-in does
	not always have.
	"""
	allocation = frappe.db.get_value(
		"Leave Allocation",
		{
			"employee": employee,
			"leave_type": leave_type,
			"docstatus": 1,
			"from_date": ["<=", date],
			"to_date": [">=", date],
		},
		["from_date", "to_date"],
		as_dict=True,
		order_by="from_date desc",
	)
	if not allocation:
		return 0
	return flt(
		frappe.db.sql(
			"""
			SELECT SUM(leaves) FROM `tabLeave Ledger Entry`
			WHERE employee = %(employee)s AND leave_type = %(leave_type)s
			AND docstatus = 1 AND is_expired = 0
			AND from_date >= %(from_date)s AND to_date <= %(to_date)s
			""",
			{
				"employee": employee,
				"leave_type": leave_type,
				"from_date": allocation.from_date,
				"to_date": allocation.to_date,
			},
		)[0][0]
	)


def resolve_leave_approver(employee) -> str | None:
	"""Who the auto-created sick leave is filed under.

	HRMS refuses to save a Leave Application with an empty ``leave_approver``
	when HR Settings has "Leave Approver Mandatory In Leave Application" set —
	which is its default — and a check-in has nobody in front of it to pick one,
	so the whole leave was being swallowed for any employee whose record carries
	no approver. Walk the same chain the Leave Application form walks
	(employee → department → reporting manager), and only where the setting
	actually demands a value fall back to whoever the check-in came in as.
	"""
	if employee.leave_approver:
		return employee.leave_approver

	if employee.department:
		approver = frappe.db.get_value(
			"Department Approver",
			{"parent": employee.department, "parentfield": "leave_approvers", "idx": 1},
			"approver",
		)
		if approver:
			return approver

	if employee.reports_to:
		approver = frappe.db.get_value("Employee", employee.reports_to, "user_id")
		if approver:
			return approver

	if not frappe.db.get_single_value("HR Settings", "leave_approver_mandatory_in_leave_application"):
		return None

	# Nothing on the employee record, and HRMS will not take the leave without
	# one: file it under the user the check-in arrived as.
	user = frappe.session.user
	if user and user != "Guest" and frappe.db.exists("User", user):
		return user
	return "Administrator"
