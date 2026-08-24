# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

"""Backfill ``cova_member`` on clinic records that predate the field.

New records get it on validate, but everything already on a site was written
before the field existed, so the member's Connections tab would start empty.
"""

import frappe

from cova_clinic_integration.member_link import LINKED_DOCTYPES, backfill_member_links


def execute():
	members = frappe.get_all(
		"Cova Members", fields=["name", "employee", "national_id"], limit_page_length=0
	)
	linked = 0
	for member in members:
		linked += backfill_member_links(member.name, member.employee, member.national_id)

	frappe.db.commit()
	frappe.logger().info(
		"cova_clinic_integration: linked %d record(s) across %s to %d member(s)"
		% (linked, ", ".join(sorted(LINKED_DOCTYPES)), len(members))
	)
