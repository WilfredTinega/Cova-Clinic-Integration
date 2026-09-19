# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

"""Tests for the append-only ``cova_raw`` trace.

Clinic Visit Cost is the fixture doctype simply because it already carries the
``cova_raw`` field; nothing here is specific to a visit.
"""

import re

import frappe

from cova_clinic_integration.cova_trace import MAX_CHARS, MAX_ENTRIES, record_cova_message
from cova_clinic_integration.testing import IntegrationTestCase, make_employee

# No IGNORE_TEST_RECORD_DEPENDENCIES here: frappe only honours it inside a
# doctype folder and raises NotImplementedError for a module under tests/.
# Nothing is needed anyway — make_employee builds the Employee these fixtures
# use, so the link crawl never runs.

HEADER = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ", re.MULTILINE)


class TestCovaTrace(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		employee = make_employee("CV-TRACE-9301", "Trace Tester")
		self.visit = frappe.get_doc(
			{
				"doctype": "Clinic Visit Cost",
				"employee": employee,
				"full_name": "Trace Tester",
				"payroll_number": "CV-TRACE-9301",
				"visit_date": "2026-09-01",
			}
		).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.db.rollback()

	def _raw(self):
		return frappe.db.get_value("Clinic Visit Cost", self.visit.name, "cova_raw") or ""

	def _set_raw(self, text):
		frappe.db.set_value("Clinic Visit Cost", self.visit.name, "cova_raw", text, update_modified=False)

	def _record(self, action, direction, payload, status=None):
		record_cova_message("Clinic Visit Cost", self.visit.name, action, direction, payload, status)

	# ── first write ───────────────────────────────────────────────────
	def test_first_write_creates_the_entry(self):
		self._record("receive_visit", "received", {"status": "success", "amount": 1700}, "success")

		raw = self._raw()
		lines = raw.splitlines()
		self.assertRegex(
			lines[0], r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] received · receive_visit · success$"
		)
		# the body is pretty-printed JSON, not a one-line dump
		self.assertEqual(lines[1], "{")
		self.assertIn('"amount": 1700', raw)

	def test_status_is_optional(self):
		self._record("submit_test_request", "sent", {"payrollNumber": "X"})
		self.assertRegex(self._raw().splitlines()[0], r"\] sent · submit_test_request$")

	def test_undumpable_payload_does_not_raise(self):
		# default=str covers datetimes/Decimals; a cyclic dict still has to survive
		cyclic = {}
		cyclic["self"] = cyclic
		self._record("receive_visit", "received", cyclic)
		self.assertIn("receive_visit", self._raw())

	# ── ordering ──────────────────────────────────────────────────────
	def test_second_write_goes_above_the_first(self):
		self._record("submit_test_request", "sent", {"seq": "older"})
		self._record("receive_test_result", "received", {"seq": "newer"}, "success")

		raw = self._raw()
		self.assertLess(raw.index("newer"), raw.index("older"))
		self.assertEqual(len(HEADER.findall(raw)), 2)
		# entries are separated by a blank line
		self.assertIn("\n\n[", raw)

	# ── legacy content ────────────────────────────────────────────────
	def test_legacy_blob_is_kept_as_the_oldest_entry(self):
		legacy = '{"legacyKey": "legacyValue"}'
		self._set_raw(legacy)

		self._record("receive_visit", "received", {"seq": "new"}, "success")
		raw = self._raw()
		self.assertIn("legacyValue", raw)
		self.assertLess(raw.index('"seq"'), raw.index("legacyKey"))

		# and it stays at the bottom as further entries land on top
		self._record("receive_visit", "received", {"seq": "newest"})
		raw = self._raw()
		self.assertLess(raw.index("newest"), raw.index("legacyKey"))
		self.assertTrue(raw.rstrip().endswith(legacy))

	# ── caps ──────────────────────────────────────────────────────────
	def test_entry_cap_drops_the_oldest(self):
		seeded = [
			'[2026-09-01 08:%02d:00] sent · submit_test_request\n{\n  "seq": %d\n}' % (i, i)
			for i in range(MAX_ENTRIES)
		]
		self._set_raw("\n\n".join(seeded))

		self._record("receive_visit", "received", {"seq": "new"}, "success")
		raw = self._raw()
		self.assertEqual(len(HEADER.findall(raw)), MAX_ENTRIES)
		self.assertIn('"seq": "new"', raw)
		self.assertNotIn('"seq": %d' % (MAX_ENTRIES - 1), raw)  # the oldest seeded entry is gone

	def test_character_cap_trims_the_history(self):
		big = "x" * 60_000
		self._record("submit_test_request", "sent", {"blob": big, "seq": "older"})
		self._record("receive_test_result", "received", {"blob": big, "seq": "newer"})

		raw = self._raw()
		self.assertLessEqual(len(raw), MAX_CHARS)
		self.assertIn("newer", raw)
		self.assertNotIn("older", raw)

	# ── no-ops ────────────────────────────────────────────────────────
	def test_unknown_doctype_is_a_silent_no_op(self):
		record_cova_message("No Such Doctype", "whatever", "receive_visit", "received", {})

	def test_missing_document_is_a_silent_no_op(self):
		record_cova_message("Clinic Visit Cost", "CV-does-not-exist", "receive_visit", "received", {})

	def test_doctype_without_the_field_is_a_silent_no_op(self):
		self.assertFalse(frappe.get_meta("Employee").has_field("cova_raw"))
		record_cova_message("Employee", self.visit.employee, "receive_visit", "received", {})

	def test_blank_target_is_a_silent_no_op(self):
		record_cova_message(None, None, "receive_visit", "received", {})
		record_cova_message("Clinic Visit Cost", None, "receive_visit", "received", {})
