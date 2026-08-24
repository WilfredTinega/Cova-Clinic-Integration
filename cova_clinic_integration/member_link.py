# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

"""Linking clinic records back to their Cova Members row.

Frappe's Connections tab is reverse-only: it lists doctypes that hold a Link
field pointing *at* the record you are looking at. Cova Members had none — its
``visit_reference`` / ``test_request_reference`` / ``test_result_reference`` all
point outwards — so a member's history was not reachable from the member.

Each per-member doctype therefore carries a ``cova_member`` link, filled in on
validate. Two doctypes deliberately do not: Health Monthly Report is an
aggregate across everyone, and Medical Case is a catalogue of conditions.
"""

import frappe


def resolve_cova_member(employee: str | None = None, national_id: str | None = None) -> str | None:
	"""The Cova Members row for an employee, or for a pre-employment national id."""
	if employee:
		found = frappe.db.get_value("Cova Members", {"employee": employee}, "name")
		if found:
			return found
	if national_id:
		found = frappe.db.get_value("Cova Members", {"national_id": national_id}, "name")
		if found:
			return found
	return None


def set_cova_member(doc) -> None:
	"""Fill ``cova_member`` if it is empty, leaving any manual value alone.

	The keys differ per doctype: a check-in may identify its employee through
	``b_employee`` (biometric punches) rather than ``employee``, and a
	pre-employment request has a national id and no Employee at all.
	"""
	if doc.get("cova_member"):
		return

	employee = doc.get("employee") or doc.get("b_employee")
	national_id = doc.get("nationa_id")  # spelling matches the doctype

	# A result carries neither on a pre-employment candidate; its request does.
	if not employee and not national_id and doc.get("request_id"):
		national_id = frappe.db.get_value("Clinic Test Request", doc.request_id, "nationa_id")

	member = resolve_cova_member(employee, national_id)
	if member:
		doc.cova_member = member


# Doctypes that carry the link, and the column used to match them to a member.
LINKED_DOCTYPES = {
	"Clinic Visit Cost": "employee",
	"Clinic Test Request": "employee",
	"Clinic Test Result": "employee",
	"Clinic Checkin": "employee",
}


def backfill_member_links(member_name: str, employee: str | None = None, national_id: str | None = None) -> int:
	"""Attach records that were created before their member existed.

	``register_preemployment_candidate`` inserts the Clinic Test Request first and
	only then creates the Cova Members row, so validate had nothing to resolve
	against. Called at the end of create_or_update_cova_member to close that gap.
	"""
	if not member_name or not (employee or national_id):
		return 0

	linked = 0
	for doctype, employee_field in LINKED_DOCTYPES.items():
		or_filters = {}
		if employee:
			or_filters[employee_field] = employee
			if doctype == "Clinic Checkin":
				or_filters["b_employee"] = employee
		if national_id and frappe.get_meta(doctype).get_field("nationa_id"):
			or_filters["nationa_id"] = national_id
		if not or_filters:
			continue

		for name in frappe.get_all(
			doctype,
			filters={"cova_member": ["in", ["", None]]},
			or_filters=or_filters,
			pluck="name",
		):
			frappe.db.set_value(doctype, name, "cova_member", member_name, update_modified=False)
			linked += 1

	return linked
