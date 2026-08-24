# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe

from cova_clinic_integration.testing import IntegrationTestCase


# The automatic link crawl reaches Company and trips the Fiscal Year overlap on
# a site that already has one. These fixtures build the Employee they need
# themselves (see cova_clinic_integration.testing.make_employee).
IGNORE_TEST_RECORD_DEPENDENCIES = ["Employee", "Company", "Leave Application"]

class TestCovaClinicSettings(IntegrationTestCase):
	"""Single doctype holding the COVA base URL and the endpoint paths every
	outbound action concatenates onto it."""

	def tearDown(self):
		frappe.db.rollback()

	def test_is_single(self):
		self.assertTrue(frappe.get_meta("Cova Clinic Settings").issingle)

	def test_endpoint_paths_round_trip(self):
		doc = frappe.get_single("Cova Clinic Settings")
		doc.base_url = "https://nodered-dev.upande.com"
		doc.register_endpoint = "/members/register"
		doc.deactivation_endpoint = "/members/deactivate"
		doc.test_request_endpoint = "/tests/request"
		doc.save(ignore_permissions=True)

		self.assertEqual(
			frappe.db.get_single_value("Cova Clinic Settings", "base_url"),
			"https://nodered-dev.upande.com",
		)
		# base_url + endpoint is exactly how api.py builds the URL
		self.assertEqual(
			frappe.db.get_single_value("Cova Clinic Settings", "base_url")
			+ frappe.db.get_single_value("Cova Clinic Settings", "register_endpoint"),
			"https://nodered-dev.upande.com/members/register",
		)
