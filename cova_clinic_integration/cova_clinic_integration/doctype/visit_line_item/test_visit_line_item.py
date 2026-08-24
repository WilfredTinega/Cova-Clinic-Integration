# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe

from cova_clinic_integration.testing import IntegrationTestCase, make_employee


# The automatic link crawl reaches Company and trips the Fiscal Year overlap on
# a site that already has one. These fixtures build the Employee they need
# themselves (see cova_clinic_integration.testing.make_employee).
IGNORE_TEST_RECORD_DEPENDENCIES = ["Employee", "Company", "Leave Application"]

class TestVisitLineItem(IntegrationTestCase):
	"""``Visit Line Item`` is the child table of Clinic Visit Cost — one row per
	thing COVA charged for on a visit."""

	def tearDown(self):
		frappe.db.rollback()

	def test_is_a_child_table(self):
		self.assertTrue(frappe.get_meta("Visit Line Item").istable)

	def test_purposes_match_the_benefit_categories(self):
		# receive_visit reads benefitBalanceAfter keyed by these same categories.
		options = frappe.get_meta("Visit Line Item").get_field("purpose").options.split("\n")
		for category in ("Consultation", "Pharmacy", "Laboratory", "Diagnostic", "Specialist"):
			self.assertIn(category, options)

	def test_rows_are_reachable_through_the_parent(self):
		parent = frappe.get_doc(
			{
				"doctype": "Clinic Visit Cost",
				"employee": make_employee("CV-TEST-9020", "Line Item"),
				"full_name": "Line Item",
				"visit_date": "2026-07-01",
				"visit_line_item": [{"purpose": "Laboratory", "cost": 300, "notes": "CBC"}],
			}
		).insert()
		rows = frappe.get_all(
			"Visit Line Item", filters={"parent": parent.name}, fields=["purpose", "cost"]
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].purpose, "Laboratory")
