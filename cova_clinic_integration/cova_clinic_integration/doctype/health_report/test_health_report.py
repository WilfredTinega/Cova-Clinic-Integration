# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe

from cova_clinic_integration.testing import IntegrationTestCase


# The automatic link crawl reaches Company and trips the Fiscal Year overlap on
# a site that already has one. These fixtures build the Employee they need
# themselves (see cova_clinic_integration.testing.make_employee).
IGNORE_TEST_RECORD_DEPENDENCIES = ["Employee", "Company", "Leave Application"]

class TestHealthReport(IntegrationTestCase):
	"""``Health Report`` is the child table of Health Monthly Report — one row per
	condition, which is what ``receive_health_report`` appends to."""

	def tearDown(self):
		frappe.db.rollback()

	def test_is_a_child_table(self):
		self.assertTrue(frappe.get_meta("Health Report").istable)

	def test_condition_is_mandatory_and_links_to_medical_case(self):
		field = frappe.get_meta("Health Report").get_field("medical_case")
		self.assertEqual(field.options, "Medical Case")
		self.assertTrue(field.reqd)

	def test_rows_are_reachable_through_the_parent(self):
		if not frappe.db.exists("Medical Case", "Cova Child Condition"):
			frappe.get_doc({"doctype": "Medical Case", "cases": "Cova Child Condition"}).insert()
		parent = frappe.get_doc(
			{
				"doctype": "Health Monthly Report",
				"month": "SEP",
				"posting_date": "2026-09-30",
				"total_cases": 7,
				"medical_cases": [{"medical_case": "Cova Child Condition", "case_count": 7}],
			}
		).insert()
		rows = frappe.get_all(
			"Health Report", filters={"parent": parent.name}, fields=["medical_case", "case_count"]
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].case_count, 7)
