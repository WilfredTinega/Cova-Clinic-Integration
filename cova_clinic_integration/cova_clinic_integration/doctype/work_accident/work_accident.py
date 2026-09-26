# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt
"""Work Accident — one workplace accident or injury: who, when, where, what
kind and how bad, the treatment, the work days it cost and the follow-up.

Recording one can send the employee to the clinic: that issues a Clinic Ticket
whose urgency follows the severity, linked back here."""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, nowtime

# How urgently the clinic should see the employee, by severity.
URGENCY_BY_SEVERITY = {"Minor": "Routine", "Moderate": "Urgent", "Serious": "Emergency", "Fatal": "Emergency"}


class WorkAccident(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		accident_date: DF.Date
		accident_time: DF.Time | None
		accident_type: DF.Literal[
			"Slip, Trip or Fall",
			"Cut or Laceration",
			"Struck by Object",
			"Caught in Machinery",
			"Manual Handling or Strain",
			"Chemical Exposure",
			"Burn or Scald",
			"Animal or Insect Bite",
			"Vehicle",
			"Electrical",
			"Other",
		]
		body_part: DF.Literal[
			"",
			"Head",
			"Eye",
			"Face",
			"Neck",
			"Back",
			"Chest or Abdomen",
			"Arm or Shoulder",
			"Hand or Finger",
			"Leg or Knee",
			"Foot or Toe",
			"Multiple",
			"None",
		]
		clinic_ticket: DF.Link | None
		company: DF.Link | None
		corrective_action: DF.SmallText | None
		days_lost: DF.Int
		department: DF.Link | None
		description: DF.SmallText
		designation: DF.Link | None
		dosh_report_date: DF.Date | None
		employee: DF.Link
		employee_name: DF.Data | None
		first_aid_details: DF.SmallText | None
		first_aid_given: DF.Check
		first_aider: DF.Link | None
		hospital: DF.Data | None
		location: DF.Data | None
		lost_time_injury: DF.Check
		payroll_number: DF.Data | None
		referred_to_hospital: DF.Check
		reported_by: DF.Link | None
		reported_to_dosh: DF.Check
		return_to_work_date: DF.Date | None
		root_cause: DF.SmallText | None
		severity: DF.Literal["Minor", "Moderate", "Serious", "Fatal"]
		status: DF.Literal["Reported", "Under Investigation", "Closed"]
		witness: DF.Data | None
	# end: auto-generated types

	def validate(self):
		self.reported_by = self.reported_by or frappe.session.user
		if getdate(self.accident_date) > getdate():
			frappe.throw(_("The accident date cannot be in the future."))
		if self.return_to_work_date and getdate(self.return_to_work_date) < getdate(self.accident_date):
			frappe.throw(_("Back at Work On cannot be before the accident."))
		self.lost_time_injury = 1 if int(self.days_lost or 0) > 0 or self.severity == "Fatal" else 0
		# A first aider attending means first aid was given.
		if self.first_aider:
			self.first_aid_given = 1
		if not self.first_aid_given:
			self.first_aid_details = None
		if not self.referred_to_hospital:
			self.hospital = None
		if not self.reported_to_dosh:
			self.dosh_report_date = None

	@frappe.whitelist()
	def send_to_clinic_from_form(self):
		self.check_permission("write")
		return self.send_to_clinic()

	def send_to_clinic(self, ignore_permissions=False):
		"""Issue a Clinic Ticket for the injured employee, dated the day of the
		accident, and link it here. Does nothing if one is already linked."""
		if self.clinic_ticket:
			return self.clinic_ticket
		ticket = frappe.get_doc(
			{
				"doctype": "Clinic Ticket",
				"employee": self.employee,
				"ticket_date": self.accident_date,
				"appointment_time": self.accident_time or nowtime(),
				"time_issued": nowtime(),
				"urgency": URGENCY_BY_SEVERITY.get(self.severity, "Routine"),
				"reason": _("Work accident {0}: {1} — {2}").format(
					self.name, self.accident_type, self.description
				),
			}
		).insert(ignore_permissions=ignore_permissions)
		self.db_set("clinic_ticket", ticket.name)
		return ticket.name
