# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe

from cova_clinic_integration.cova_clinic_integration.doctype.test_package.test_package import (
	cova_package_code,
)
from cova_clinic_integration.testing import IntegrationTestCase


class TestTestPackage(IntegrationTestCase):
	"""The packages a Clinic Test Request can carry are records now, not a Select
	list, so what used to be enforced by the field's options is enforced here."""

	def tearDown(self):
		frappe.db.rollback()

	def test_standard_packages_are_installed(self):
		# Laid down by setup.ensure_test_packages(), which after_install and
		# after_migrate both run.
		for package in (
			"Pre Employment Wellness",
			"Cholinesterase",
			"Food Handler",
			"Annual Medical",
			"Exit Medical",
		):
			self.assertTrue(frappe.db.exists("Test Package", package), package)

	def test_name_is_the_package_name(self):
		doc = frappe.get_doc({"doctype": "Test Package", "package_name": "  Spirometry  "}).insert()
		self.assertEqual(doc.name, "Spirometry")

	def test_cova_code_defaults_to_the_de_spaced_name(self):
		# The rule every request used before this doctype existed.
		self.assertEqual(cova_package_code("Annual Medical"), "AnnualMedical")

	def test_cova_code_overrides_the_name(self):
		# A name the site cannot already be using: "X-Ray" is a real package on
		# kaitet-group, and the back-fill patch mints it wherever the requests
		# carry it, so a test that claims the name fails on exactly the sites
		# this doctype was built for.
		frappe.get_doc(
			{"doctype": "Test Package", "package_name": "Zz Cova Code Fixture", "cova_code": "XRAY"}
		).insert()
		self.assertEqual(cova_package_code("Zz Cova Code Fixture"), "XRAY")

	def test_unknown_package_falls_back_to_the_raw_value(self):
		# A request carrying a package nobody created yet still reaches COVA with
		# something rather than an empty testPackage.
		self.assertEqual(cova_package_code("Not A Package"), "NotAPackage")
