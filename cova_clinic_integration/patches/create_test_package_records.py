# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt
"""Back-fill Test Package records for the packages already on Clinic Test Request.

``Clinic Test Request.test_package`` was a Select and is now a Link to the new
Test Package doctype. The column is a varchar either way, so nothing is lost by
the schema change itself — but every existing row now points at a record that has
to exist, or the next save of that request fails link validation and the Test
Package column renders as a broken link.

Two kinds of value are out there:

  * the five the Select offered, which ``setup.ensure_test_packages()`` creates;
  * everything else. The Select never stopped a value being written by the API,
    an import or a direct db write, so live sites carry packages that were never
    in the list at all (kaitet-group has "X-Ray"). Those are minted here from the
    data rather than guessed at, and their ``description`` says where they came
    from so nobody mistakes one for a package somebody chose to define.

Near-misses are folded in rather than duplicated: " Annual Medical" and
"annual medical" become the one canonical record, and the requests holding them
are rewritten to match — two records differing only in case or a space read as
the same package everywhere they are shown, but filter and group as two.

Clinic Test Result also has a ``test_package``, deliberately left out of this: it
is a plain Data field holding whatever COVA sent back (the de-spaced code form,
"AnnualMedical"), not a reference to a package on this site.
"""

import frappe

from cova_clinic_integration.setup import ensure_test_packages


def execute():
	ensure_test_packages()

	# Canonical name by lower-case key, so an existing "Annual Medical" claims
	# every casing of itself that the requests happen to carry.
	canonical = {name.lower(): name for name in frappe.get_all("Test Package", pluck="name")}

	# Row by row rather than over a DISTINCT: the column's collation is
	# case-insensitive and pads spaces, so "annual medical " and "Annual Medical"
	# collapse into one DISTINCT value — and the row actually holding the odd
	# spelling would never be seen, let alone corrected. Comparing in Python and
	# updating by primary key keeps the collation out of it entirely.
	rows = frappe.db.sql(
		"""
		SELECT name, test_package
		FROM `tabClinic Test Request`
		WHERE test_package IS NOT NULL AND TRIM(test_package) != ''
		""",
		as_dict=True,
	)

	created = []
	rewritten = {}

	for row in rows:
		raw = row.get("test_package") or ""
		cleaned = raw.strip()
		if not cleaned:
			continue

		target = canonical.get(cleaned.lower())

		if not target:
			doc = frappe.get_doc(
				{
					"doctype": "Test Package",
					"package_name": cleaned,
					"description": "Created by migration from requests already carrying this package.",
				}
			).insert(ignore_permissions=True, ignore_if_duplicate=True)
			target = doc.name
			canonical[cleaned.lower()] = target
			created.append(target)

		if raw != target:
			# db.sql rather than a doc save: a Clinic Test Request validates (and
			# re-fetches) far more than this one column, and the value is not
			# changing in meaning here — only in spelling.
			frappe.db.sql(
				"UPDATE `tabClinic Test Request` SET test_package = %s WHERE name = %s",
				(target, row["name"]),
			)
			rewritten[(raw, target)] = rewritten.get((raw, target), 0) + 1

	if created:
		print("Test Package created from existing requests: " + ", ".join(created))
	for (raw, target), count in rewritten.items():
		print("Clinic Test Request: %s row(s) re-spelled %r -> %r" % (count, raw, target))
