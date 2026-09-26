# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt
"""Clinic Ticket — the record that an employee has been allowed to go to the
clinic.

A ticket is issued (by hand, or from an approved *Medical* Gate Pass on sites
that run upande_ta), turns *Visited* when the employee's Clinic Checkin
arrives inside its validity window, and is *Expired* by the daily job once
that window has passed unused."""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, getdate, now_datetime, today

OPEN = "Issued"


class ClinicTicket(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		appointment_time: DF.Time | None
		arrived: DF.Check
		clinic_checkin: DF.Link | None
		company: DF.Link | None
		department: DF.Link | None
		designation: DF.Link | None
		employee: DF.Link
		employee_name: DF.Data | None
		issued_by: DF.Link | None
		left_for_clinic_at: DF.Datetime | None
		minutes_to_reach: DF.Int
		payroll_number: DF.Data | None
		reason: DF.SmallText
		status: DF.Literal["Issued", "Visited", "Expired", "Cancelled"]
		ticket_date: DF.Date
		time_issued: DF.Time | None
		urgency: DF.Literal["Routine", "Urgent", "Emergency"]
		valid_until: DF.Date | None
		visited_at: DF.Datetime | None
	# end: auto-generated types

	def validate(self):
		self.issued_by = self.issued_by or frappe.session.user
		self.valid_until = self.valid_until or self.ticket_date
		if getdate(self.valid_until) < getdate(self.ticket_date):
			frappe.throw(_("Valid Until cannot be before the Ticket Date."))
		self.validate_one_open_ticket()

	def validate_one_open_ticket(self):
		if self.status != OPEN:
			return
		clash = frappe.db.get_value(
			"Clinic Ticket",
			{
				"employee": self.employee,
				"status": OPEN,
				"name": ["!=", self.name],
				"ticket_date": ["<=", self.valid_until],
				"valid_until": [">=", self.ticket_date],
			},
			"name",
		)
		if clash:
			frappe.throw(
				_("{0} already has an open clinic ticket for these dates: {1}").format(
					self.employee_name or self.employee, frappe.utils.get_link_to_form("Clinic Ticket", clash)
				),
				title=_("Ticket Already Issued"),
			)


def open_ticket_for(employee, on_date):
	"""The Issued ticket that covers ``on_date`` for this employee, if any."""
	return frappe.db.get_value(
		"Clinic Ticket",
		{
			"employee": employee,
			"status": OPEN,
			"ticket_date": ["<=", on_date],
			"valid_until": [">=", on_date],
		},
		"name",
		order_by="ticket_date asc",
	)


def left_for_clinic_at(ticket):
	"""When the employee set off: the gate's actual time out on the ticket's
	Gate Pass when the gate recorded one, otherwise when the ticket was issued."""
	gate_pass = ticket.get("gate_pass")
	if gate_pass and frappe.db.exists("Gate Pass", gate_pass):
		gp = frappe.db.get_value("Gate Pass", gate_pass, ["date", "actual_time_out"], as_dict=True)
		if gp and gp.actual_time_out:
			return get_datetime(f"{gp.date} {gp.actual_time_out}")
	if ticket.get("time_issued"):
		return get_datetime(f"{ticket.ticket_date} {ticket.time_issued}")
	return None


def mark_visited(checkin):
	"""Called from Clinic Checkin.after_insert: the employee has reached the
	clinic, so the ticket that let them go is used up — flagged as arrived,
	with when they arrived and how long they took to get there.

	A check-in made from the dashboard's clinic desk names its ticket in
	``checkin.flags.clinic_ticket``; a biometric punch finds the open one."""
	if not checkin.employee:
		return
	when = get_datetime(checkin.get("time") or checkin.get("time_in") or now_datetime())
	name = checkin.flags.get("clinic_ticket") or open_ticket_for(checkin.employee, getdate(when))
	if not name:
		return
	fields = ["name", "ticket_date", "time_issued", "status"]
	if frappe.db.has_column("Clinic Ticket", "gate_pass"):
		fields.append("gate_pass")
	ticket = frappe.db.get_value("Clinic Ticket", name, fields, as_dict=True)
	if not ticket or ticket.status != OPEN:
		return

	left = left_for_clinic_at(ticket)
	minutes = int((when - left).total_seconds() // 60) if left and when >= left else None
	frappe.db.set_value(
		"Clinic Ticket",
		name,
		{
			"status": "Visited",
			"arrived": 1,
			"clinic_checkin": checkin.name,
			"visited_at": when,
			"left_for_clinic_at": left,
			"minutes_to_reach": minutes,
		},
	)


def expire_tickets():
	"""Daily: an Issued ticket whose last valid day has gone was never used."""
	for name in frappe.get_all(
		"Clinic Ticket", filters={"status": OPEN, "valid_until": ["<", today()]}, pluck="name"
	):
		frappe.db.set_value("Clinic Ticket", name, "status", "Expired")


# ─── Gate Pass (upande_ta) ──────────────────────────────────────────────
# Gate Pass carries a `clinic_ticket` link (setup.install_gate_pass_link) when
# it was raised from a ticket. One ticket has at most one live pass: a draft,
# pending or approved one blocks another, while a rejected or cancelled one
# frees the ticket for a fresh pass.


def _is_live(doc):
	return doc.docstatus < 2 and doc.get("workflow_state") != "Rejected"


def gate_pass_validate(doc, method=None):
	"""Refuse a second live Gate Pass for the same clinic ticket."""
	ticket = doc.get("clinic_ticket")
	if not ticket or not _is_live(doc):
		return
	status = frappe.db.get_value("Clinic Ticket", ticket, "status")
	if status in ("Cancelled", "Expired"):
		frappe.throw(_("Clinic Ticket {0} is {1}; issue a new ticket first.").format(ticket, status))
	for other in frappe.get_all(
		"Gate Pass",
		filters={"clinic_ticket": ticket, "name": ["!=", doc.name], "docstatus": ["<", 2]},
		fields=["name", "docstatus", "workflow_state"],
	):
		if other.get("workflow_state") != "Rejected":
			frappe.throw(
				_("Clinic Ticket {0} already has Gate Pass {1}. Use that one rather than raising another.").format(
					ticket, frappe.utils.get_link_to_form("Gate Pass", other.name)
				),
				title=_("Gate Pass Already Raised"),
			)


def gate_pass_after_insert(doc, method=None):
	"""Link the ticket to its pass straight away, so the ticket stops offering
	to raise one while the pass is still waiting for approval."""
	if doc.get("clinic_ticket") and frappe.db.has_column("Clinic Ticket", "gate_pass"):
		frappe.db.set_value("Clinic Ticket", doc.clinic_ticket, "gate_pass", doc.name)


def gate_pass_released(doc, method=None):
	"""Gate Pass on_cancel / on_trash: free the ticket for a new pass."""
	if not frappe.db.has_column("Clinic Ticket", "gate_pass"):
		return
	for name in frappe.get_all("Clinic Ticket", filters={"gate_pass": doc.name}, pluck="name"):
		frappe.db.set_value("Clinic Ticket", name, "gate_pass", None)


def ticket_from_gate_pass(doc, method=None):
	"""Gate Pass on_submit (upande_ta). A pass raised from a ticket is already
	linked to it; a rejected one lets go of it. An approved *Medical* pass
	raised on its own is the permission to go to the clinic, so it issues the
	ticket — or links the one already issued for that employee and day."""
	if doc.get("workflow_state") == "Rejected":
		gate_pass_released(doc)
		return
	if doc.get("clinic_ticket"):
		return
	if doc.get("pass_type") != "Medical":
		return
	if doc.get("workflow_state") and doc.workflow_state != "Approved":
		return
	if not frappe.db.has_column("Clinic Ticket", "gate_pass"):
		return
	if frappe.db.exists("Clinic Ticket", {"gate_pass": doc.name}):
		return

	existing = frappe.db.get_value(
		"Clinic Ticket",
		{"employee": doc.employee, "status": OPEN, "ticket_date": doc.date, "gate_pass": ["in", ["", None]]},
		"name",
	)
	if existing:
		frappe.db.set_value("Clinic Ticket", existing, "gate_pass", doc.name)
		return

	ticket = frappe.new_doc("Clinic Ticket")
	ticket.update({
		"employee": doc.employee,
		"ticket_date": doc.date,
		"time_issued": doc.get("time_out"),
		"reason": doc.get("reason") or _("Medical gate pass {0}").format(doc.name),
		"issued_by": doc.get("hr_approver") or frappe.session.user,
		"gate_pass": doc.name,
	})
	ticket.insert(ignore_permissions=True)
