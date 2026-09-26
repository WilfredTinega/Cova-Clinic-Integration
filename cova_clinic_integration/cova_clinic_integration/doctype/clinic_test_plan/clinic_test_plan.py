# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt
"""Clinic Test Plan — the target for one test group (Food Handler,
Cholinesterase for the sprayers, …): how many employees it covers, over what
period they are all to be tested, and in how many rounds.

Each round becomes a Clinic Test Schedule when it is scheduled. Only employees
who are present are scheduled: anyone on approved leave during the round is
left out and, having no request, is carried into the next round. Later rounds
take a larger share when earlier ones fell short, so the period still covers
everyone."""

import math
import random

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, date_diff, getdate

from cova_clinic_integration.cova_clinic_integration.doctype.clinic_test_schedule.clinic_test_schedule import (
	group_employees,
)
from cova_clinic_integration.cova_clinic_integration.doctype.cova_clinic_settings.cova_clinic_settings import (
	get_clinic_company,
	get_test_groups,
)


class ClinicTestPlan(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from cova_clinic_integration.cova_clinic_integration.doctype.clinic_test_plan_round.clinic_test_plan_round import ClinicTestPlanRound

		company: DF.Link
		employees_per_round: DF.Int
		notes: DF.SmallText | None
		number_of_rounds: DF.Int
		period_from: DF.Date
		period_to: DF.Date
		progress: DF.Percent
		remaining_employees: DF.Int
		rounds: DF.Table[ClinicTestPlanRound]
		scheduled_employees: DF.Int
		send_to_cova: DF.Check
		status: DF.Literal["Draft", "In Progress", "Completed"]
		test_group: DF.Autocomplete
		test_package: DF.Link | None
		tested_employees: DF.Int
		total_employees: DF.Int
	# end: auto-generated types

	def validate(self):
		self.company = self.company or get_clinic_company()
		if getdate(self.period_to) < getdate(self.period_from):
			frappe.throw(_("Period To cannot be before Period From."))
		if int(self.number_of_rounds or 0) < 1:
			frappe.throw(_("A plan needs at least one test round."))
		group = self.group()
		self.test_package = group["test_package"]
		for i, r in enumerate(self.rounds, 1):
			r.round_no = i
			if getdate(r.scheduled_to) < getdate(r.scheduled_from):
				frappe.throw(_("Round {0}: To cannot be before From.").format(i))
		self.update_metrics()

	def group(self):
		groups = get_test_groups()
		if self.test_group not in groups:
			frappe.throw(_("Test group {0} is not set up in Cova Clinic Settings.").format(self.test_group))
		return groups[self.test_group]

	def employees(self, **kw):
		group = self.group()
		return group_employees(
			self.company, group["departments"], group["designations"], **kw
		)

	def update_metrics(self):
		"""Group size, and how far through the period the plan has got."""
		members = {e.name for e in self.employees()}
		self.total_employees = len(members)
		self.employees_per_round = math.ceil(self.total_employees / max(int(self.number_of_rounds or 1), 1))

		requests = frappe.get_all(
			"Clinic Test Request",
			filters={
				"test_package": self.test_package,
				"scheduled_from": ["between", [self.period_from, self.period_to]],
				"status": ["!=", "Cancelled"],
				"employee": ["in", list(members) or [""]],
			},
			fields=["employee", "status"],
		)
		scheduled = {r.employee for r in requests}
		tested = {r.employee for r in requests if r.status == "Completed"}
		self.scheduled_employees = len(scheduled)
		self.tested_employees = len(tested)
		self.remaining_employees = self.total_employees - len(scheduled)
		self.progress = round(len(tested) * 100.0 / self.total_employees, 1) if self.total_employees else 0

		for r in self.rounds:
			if r.status != "Scheduled":
				r.planned_employees = self.employees_per_round
		if self.total_employees and len(tested) >= self.total_employees:
			self.status = "Completed"
		elif any(r.status == "Scheduled" for r in self.rounds):
			self.status = "In Progress"
		else:
			self.status = "Draft"

	@frappe.whitelist()
	def generate_rounds(self):
		"""Split the period into ``number_of_rounds`` back-to-back windows."""
		if not self.flags.ignore_permissions:
			self.check_permission("write")
		if any(r.status == "Scheduled" for r in self.rounds):
			frappe.throw(_("Some rounds are already scheduled; edit the remaining rounds instead."))
		n = max(int(self.number_of_rounds or 1), 1)
		days = date_diff(self.period_to, self.period_from) + 1
		if days < n:
			frappe.throw(_("The period has {0} days, too short for {1} rounds.").format(days, n))
		self.set("rounds", [])
		start = getdate(self.period_from)
		for i in range(n):
			length = days // n + (1 if i < days % n else 0)
			end = add_days(start, length - 1)
			self.append("rounds", {"round_no": i + 1, "scheduled_from": start, "scheduled_to": end})
			start = add_days(end, 1)
		self.save()
		return {"rounds": len(self.rounds)}

	@frappe.whitelist()
	def schedule_next_round(self):
		"""Schedule the next Planned round: pick its share of the employees who
		are still untested this period and present for the round (not on
		leave), raise their test requests and send them to Cova."""
		if not self.flags.ignore_permissions:
			self.check_permission("write")
		if not self.rounds:
			self.generate_rounds()
		pending = [r for r in self.rounds if r.status == "Planned"]
		if not pending:
			frappe.throw(_("Every round of this plan is already scheduled."))
		rnd = pending[0]
		window = (rnd.scheduled_from, rnd.scheduled_to)
		common = {"package": self.test_package, "already_from": self.period_from, "already_to": self.period_to}

		available = self.employees(**common, leave_window=window)
		on_leave = self.employees(**common, leave_window=window, on_leave=True)
		# Catch up on earlier shortfalls: the rounds left share whoever is left.
		share = math.ceil((len(available) + len(on_leave)) / len(pending))
		picked = random.sample(available, min(share, len(available)))

		group = self.group()
		schedule = frappe.new_doc("Clinic Test Schedule")
		schedule.update({
			"title": _("{0} — Round {1} of {2}").format(self.test_group, rnd.round_no, len(self.rounds)),
			"test_group": self.test_group,
			"test_package": self.test_package,
			"company": self.company,
			"scheduled_from": rnd.scheduled_from,
			"scheduled_to": rnd.scheduled_to,
			"skip_already_scheduled": 1,
			"skip_employees_on_leave": 1,
			"send_to_cova": self.send_to_cova,
			"notes": self.notes,
		})
		for dept in group["departments"]:
			schedule.append("departments", {"department": dept})
		for desig in group["designations"]:
			schedule.append("designations", {"designation": desig})
		for e in picked:
			schedule.append("employees", {
				"employee": e.name, "employee_name": e.employee_name,
				"department": e.department, "designation": e.designation,
			})
		schedule.flags.ignore_permissions = bool(self.flags.ignore_permissions)
		schedule.insert()
		result = schedule.create_test_requests() if picked else {"created": 0, "sent": 0, "failed": 0, "failures": []}

		rnd.status = "Scheduled"
		rnd.clinic_test_schedule = schedule.name
		rnd.scheduled_employees = len(picked)
		rnd.skipped_on_leave = len(on_leave)
		self.save()
		result.update({
			"round": rnd.round_no,
			"schedule": schedule.name,
			"scheduled": len(picked),
			"on_leave": [{"employee": e.name, "employee_name": e.employee_name} for e in on_leave],
		})
		return result
