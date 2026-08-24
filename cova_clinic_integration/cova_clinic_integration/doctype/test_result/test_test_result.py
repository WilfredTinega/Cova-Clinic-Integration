# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe

from cova_clinic_integration.testing import IntegrationTestCase


# The automatic link crawl reaches Company and trips the Fiscal Year overlap on
# a site that already has one. These fixtures build the Employee they need
# themselves (see cova_clinic_integration.testing.make_employee).
IGNORE_TEST_RECORD_DEPENDENCIES = ["Employee", "Company", "Leave Application"]

class TestTestResult(IntegrationTestCase):
	"""``Test Result`` is the child table of Clinic Test Result that holds one
	risk grade per condition. Child tables only exist inside a parent."""

	def tearDown(self):
		frappe.db.rollback()

	def test_is_a_child_table(self):
		self.assertTrue(frappe.get_meta("Test Result").istable)

	def test_risk_options_match_the_outcome_map(self):
		# api.py maps COVA's clinicalOutcome onto exactly these four grades.
		options = frappe.get_meta("Test Result").get_field("select_tezd").options.split("\n")
		for grade in ("High Risk", "Medium Risk", "Low Risk", "No Risk"):
			self.assertIn(grade, options)

	def test_condition_links_to_medical_case(self):
		self.assertEqual(frappe.get_meta("Test Result").get_field("test").options, "Medical Case")
