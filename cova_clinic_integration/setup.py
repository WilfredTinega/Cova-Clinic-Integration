# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt
"""Site wiring for the integration: the custom fields it installs on standard
doctypes, the settings it publishes to the client on boot, and its document
hooks. Everything hooks.py points at (other than the API endpoints in api.py)
lives here rather than in a file per hook.
"""

import frappe
from frappe import _

from cova_clinic_integration.cova_clinic_integration.doctype.cova_clinic_settings.cova_clinic_settings import (
	get_clinic_company,
)

# ─── custom fields ────────────────────────────────────────────────────────

# insert_after is used only for positioning; it is NOT a DocField attribute.
# Job Offer already ships a standard `company` Link, so the block gates on that
# rather than carrying its own copy. The company it is compared against is the
# one picked in Cova Clinic Settings, published to the client by the
# extend_bootinfo hook below.
_JO_DEPENDS = "eval:doc.company && doc.company===frappe.boot.cova_clinic_company"

CLINIC_FIELDS = {
	"Job Offer": [
		# COVA pre-employment biodata + tracking block. Job Offer is submittable
		# and registration happens after the offer goes out, so every field here
		# is allow_on_submit; the tracking fields are no_copy so an amended offer
		# starts clean.
		{
			"fieldname": "cova_section",
			"label": "COVA Pre-Employment",
			"fieldtype": "Section Break",
			"insert_after": "company",
			"depends_on": _JO_DEPENDS,
		},
		{
			"fieldname": "national_id",
			"label": "National ID",
			"fieldtype": "Data",
			"insert_after": "cova_section",
			"allow_on_submit": 1,
		},
		{
			"fieldname": "phone_number",
			"label": "Phone Number",
			"fieldtype": "Data",
			"options": "Phone",
			"insert_after": "national_id",
			"allow_on_submit": 1,
			# Job Applicant carries a (mandatory, on the csf_ke variant) phone
			# number, so the offer should not make HR retype it. fetch_if_empty
			# keeps the field editable and only fills a blank one — phone_number
			# is one of the two REQUIRED_FIELDS below, so it must stay writable
			# for an applicant who has none on file.
			"fetch_from": "job_applicant.phone_number",
			"fetch_if_empty": 1,
		},
		{
			"fieldname": "cova_column_break_1",
			"fieldtype": "Column Break",
			"insert_after": "phone_number",
		},
		{
			"fieldname": "date_of_birth",
			"label": "Date of Birth",
			"fieldtype": "Date",
			"insert_after": "cova_column_break_1",
			"allow_on_submit": 1,
		},
		{
			"fieldname": "gender",
			"label": "Gender",
			# Link to the Gender doctype, matching Employee.gender, rather than a
			# hardcoded Male/Female list.
			"fieldtype": "Link",
			"options": "Gender",
			"insert_after": "date_of_birth",
			"allow_on_submit": 1,
		},
		{
			"fieldname": "cova_column_break_2",
			"fieldtype": "Column Break",
			"insert_after": "gender",
		},
		{
			"fieldname": "cova_registered",
			"label": "Cova Registered?",
			"fieldtype": "Check",
			"insert_after": "cova_column_break_2",
			"allow_on_submit": 1,
			"no_copy": 1,
			"read_only": 1,
		},
		{
			"fieldname": "cova_column_break_3",
			"fieldtype": "Column Break",
			"insert_after": "cova_registered",
		},
		# Tested? leads the last column so it is never blank: sites with
		# hide_empty_read_only_fields on suppress the empty Link below it, and a
		# column holding only that field would render as dead space.
		{
			"fieldname": "cova_tested",
			"label": "Cova Tested?",
			"fieldtype": "Check",
			"insert_after": "cova_column_break_3",
			"allow_on_submit": 1,
			"no_copy": 1,
			"read_only": 1,
		},
		{
			"fieldname": "linked_test_result",
			"label": "Linked Test Result",
			"fieldtype": "Link",
			"options": "Clinic Test Result",
			"insert_after": "cova_tested",
			"allow_on_submit": 1,
			"no_copy": 1,
			"read_only": 1,
		},
	],
	"Employee": [
		# A fourth column in the Overview tab's first section, holding Status and
		# the two COVA fields.
		#
		# The anchor is `image` — the hidden Attach Image that sits between
		# `date_of_joining` and `status` — so the break lands immediately before
		# Status and takes it into the new column. Anchoring on `date_of_joining`
		# instead would put the break ahead of anything a site has hung off that
		# field, dragging those into the COVA column too.
		#
		# These anchors must name real Employee DocFields. The fields used to be
		# anchored on `national_id`, which is a Custom Field on Kenyan sites
		# (csf_ke) and no field at all on a vanilla one — _position() cannot see
		# either, so both COVA fields stayed where append() left them: the end of
		# the field list, which on Employee is *after* ERPNext's own
		# `connections_tab` break. That is why they rendered in the Connections
		# tab rather than anywhere near the identity fields.
		{
			"fieldname": "cova_overview_column",
			"fieldtype": "Column Break",
			"insert_after": "image",
		},
		{
			"fieldname": "cova_member_id",
			"label": "Cova Member ID",
			"fieldtype": "Data",
			"insert_after": "status",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "cova_deactivated",
			"label": "Cova Deactivated",
			"fieldtype": "Check",
			"insert_after": "cova_member_id",
			"default": "0",
			"no_copy": 1,
		},
		# First aid: kept on the employee themselves. Their phone and farm are
		# the employee's own (cell_number, and custom_farm or branch).
		{
			"fieldname": "is_first_aider",
			"label": "First Aider",
			"fieldtype": "Check",
			"insert_after": "cova_deactivated",
			"default": "0",
			"description": "Trained in first aid; listed on the Clinic dashboard with their phone and farm.",
		},
		{
			"fieldname": "first_aid_certified_until",
			"label": "First Aid Certificate Valid Until",
			"fieldtype": "Date",
			"insert_after": "is_first_aider",
			"depends_on": "eval:doc.is_first_aider",
		},
	],
}


def _save_standard_doctype(dt):
	"""Save an edited standard (custom=0) DocType from anywhere, developer_mode
	on or off.

	Two flags do the work, and they are set for the duration of the save only:

	  * ``in_import`` permits the save without exporting the doctype back to
	    disk, so the owning app's JSON (erpnext/employee.json, hrms/job_offer.json)
	    is never rewritten by a site;
	  * ``in_patch`` is the exemption ``DocType.check_developer_mode()`` itself
	    honours. Without it a production site (developer_mode off, which is every
	    Frappe Cloud site) throws CannotCreateStandardDoctypeError, and this app
	    exists to install normal DocFields — not Custom Fields — so the fields
	    would simply never appear. It is the same context the app's own patches
	    already call these installers in.
	"""
	prev_in_import = frappe.flags.in_import
	prev_in_patch = frappe.flags.in_patch
	frappe.flags.in_import = True
	frappe.flags.in_patch = True
	try:
		dt.flags.ignore_permissions = True
		dt.save()
	finally:
		frappe.flags.in_import = prev_in_import
		frappe.flags.in_patch = prev_in_patch


def _position(dt, row, insert_after):
	"""Move an appended field row to sit right after `insert_after`.

	Only the doctype's own DocFields can be anchored against: a Custom Field on
	the parent (csf_ke's `national_id`, say) is merged into the form at render
	time and is not in `dt.fields`, so there is nothing here to measure from.

	When the anchor cannot be found the row is put before the doctype's
	Connections tab rather than left at the end of the list. The end of a tabbed
	standard doctype is *inside* its last tab — on Employee that is ERPNext's own
	`connections_tab` — so a missed anchor does not merely leave a field in a
	dull spot, it files it under Connections, which reads as a bug in the app
	rather than a missing anchor.
	"""
	if not insert_after:
		return

	names = [f.fieldname for f in dt.fields]

	if insert_after in names:
		dt.fields.remove(row)
		dt.fields.insert(names.index(insert_after) + 1, row)
		return

	if "connections_tab" in names:
		dt.fields.remove(row)
		dt.fields.insert(names.index("connections_tab"), row)


def install_clinic_fields():
	"""Create the integration's fields as normal DocFields (idempotent).

	If a field already exists as a Custom Field record, it is converted: the
	Custom Field record is deleted and the same fieldname is re-added as a
	normal DocField. The physical column is left in place across the swap, so
	existing data is preserved.

	Runs on any site: the save goes through `_save_standard_doctype`, which
	carries its own exemption from the developer_mode wall."""
	for doctype, specs in CLINIC_FIELDS.items():
		fieldnames = [s["fieldname"] for s in specs]

		# Convert any existing Custom Field versions to normal fields: drop the
		# Custom Field record first (the DB column stays, keeping the data).
		for cf in frappe.get_all(
			"Custom Field",
			filters={"dt": doctype, "fieldname": ["in", fieldnames]},
			pluck="name",
		):
			frappe.delete_doc("Custom Field", cf, ignore_permissions=True, force=True)
		frappe.clear_cache(doctype=doctype)

		dt = frappe.get_doc("DocType", doctype)
		existing = {f.fieldname: f for f in dt.fields}
		changed = False

		# Fields already present are re-synced to the spec rather than left as
		# they are, so edits here (a new depends_on, say) reach sites that were
		# installed against an older version.
		for spec in specs:
			row = existing.get(spec["fieldname"])
			if row is None:
				continue
			for key, value in spec.items():
				if key == "insert_after":
					continue
				if row.get(key) != value:
					row.set(key, value)
					changed = True

		for spec in specs:
			if spec["fieldname"] in existing:
				continue
			field = {k: v for k, v in spec.items() if k != "insert_after"}
			row = dt.append("fields", field)
			_position(dt, row, spec.get("insert_after"))
			changed = True

		# Re-seat every field the app owns against its current insert_after, so
		# re-ordering the spec (moving a column break, say) reaches sites where
		# the fields already exist. Done in spec order after the additions, so
		# each anchor is already in its final place when the next row moves.
		for spec in specs:
			row = next((f for f in dt.fields if f.fieldname == spec["fieldname"]), None)
			if row is None:
				continue
			before = list(dt.fields)
			_position(dt, row, spec.get("insert_after"))
			if dt.fields != before:
				changed = True

		if not changed:
			continue

		for i, f in enumerate(dt.fields):
			f.idx = i + 1

		_save_standard_doctype(dt)
		frappe.clear_cache(doctype=doctype)

	frappe.db.commit()


# ─── Connections on standard doctypes ─────────────────────────────────────
# The app's own doctypes carry their connections in their JSON. Employee is
# standard, so its DocType Link rows are installed the same way its fields are.
#
# Clinic Checkin appears once. It used to appear twice, one entry per employee
# field, back when punches and sick-offs named their person in different
# columns. Both use `employee` now, so a second entry is simply a duplicate row
# in the Connections tab — DocType Link has no label override, so two entries
# for the same doctype and field are indistinguishable.
CLINIC_CONNECTIONS = {
	"Employee": [
		{"group": "Cova Clinic", "link_doctype": "Cova Members", "link_fieldname": "employee"},
		{"group": "Cova Clinic", "link_doctype": "Clinic Visit Cost", "link_fieldname": "employee"},
		{"group": "Cova Clinic", "link_doctype": "Clinic Test Request", "link_fieldname": "employee"},
		{"group": "Cova Clinic", "link_doctype": "Clinic Test Result", "link_fieldname": "employee"},
		{"group": "Clinic Attendance", "link_doctype": "Clinic Checkin", "link_fieldname": "employee"},
		{"group": "Clinic Attendance", "link_doctype": "Clinic Ticket", "link_fieldname": "employee"},
		{"group": "Workplace Safety", "link_doctype": "Work Accident", "link_fieldname": "employee"},
		{"group": "Workplace Safety", "link_doctype": "Work Accident", "link_fieldname": "first_aider"},
	],
}


def _connection_key(row):
	return (row.get("link_doctype"), row.get("link_fieldname"), row.get("table_fieldname") or "")


def install_clinic_links():
	"""Add the integration's Connections to the standard doctypes (idempotent).

	Saved the same way as install_clinic_fields, through
	`_save_standard_doctype`, so it too works with developer_mode off and leaves
	the owning app's JSON on disk alone."""
	for doctype, specs in CLINIC_CONNECTIONS.items():
		dt = frappe.get_doc("DocType", doctype)
		existing = {_connection_key(row.as_dict()): row for row in dt.links}
		changed = False

		for spec in specs:
			row = existing.get(_connection_key(spec))
			if row is None:
				dt.append("links", spec)
				changed = True
				continue
			# Re-sync, so a changed group reaches sites installed against an
			# older version.
			for key, value in spec.items():
				if row.get(key) != value:
					row.set(key, value)
					changed = True

		if not changed:
			continue

		for i, row in enumerate(dt.links):
			row.idx = i + 1

		_save_standard_doctype(dt)
		frappe.clear_cache(doctype=doctype)

	frappe.db.commit()


def uninstall_clinic_links():
	"""Drop the integration's Connections again on uninstall."""
	for doctype, specs in CLINIC_CONNECTIONS.items():
		wanted = {_connection_key(spec) for spec in specs}
		dt = frappe.get_doc("DocType", doctype)
		kept = [row for row in dt.links if _connection_key(row.as_dict()) not in wanted]
		if len(kept) != len(dt.links):
			dt.set("links", kept)
			for i, row in enumerate(dt.links):
				row.idx = i + 1
			_save_standard_doctype(dt)
			frappe.clear_cache(doctype=doctype)

	frappe.db.commit()


# ─── install / uninstall entry points ─────────────────────────────────────


def repair_naming_series():
	"""Pull each ``PREFIX.-.####`` counter up to the highest name already issued.

	A counter that has fallen behind is fatal, not cosmetic: getseries() starts a
	missing row at 1, the generated name collides with an existing record, the
	insert dies with a duplicate-key error — and because that rolls the failed
	transaction back, the counter row goes with it and the next attempt starts at
	1 all over again. Every insert for that doctype fails, permanently, until
	somebody notices. It happens whenever rows arrive without their counter: a
	partial restore, a table copied between sites, a hand-deleted Series row.

	Only ever moves a counter forward, so it can prevent a collision but never
	cause one.
	"""
	repaired = []
	for doctype, prefix in (
		("Clinic Test Request", "TR-"),
		("Clinic Test Result", "CTR-"),
		("Health Monthly Report", "HMR-"),
	):
		if not frappe.db.table_exists(doctype):
			continue

		highest = 0
		for (name,) in frappe.db.sql(f"SELECT name FROM `tab{doctype}` WHERE name LIKE %s", (prefix + "%",)):
			tail = str(name)[len(prefix) :]
			if tail.isdigit():
				highest = max(highest, int(tail))

		if not highest:
			continue

		current = frappe.db.sql("SELECT `current` FROM tabSeries WHERE name = %s", prefix)
		current = current[0][0] if current else None
		if current is not None and int(current) >= highest:
			continue

		frappe.db.sql(
			"INSERT INTO tabSeries (name, current) VALUES (%s, %s) "
			"ON DUPLICATE KEY UPDATE current = GREATEST(current, %s)",
			(prefix, highest, highest),
		)
		repaired.append(f"{prefix} {current} -> {highest}")

	if repaired:
		frappe.db.commit()
		frappe.log_error(
			title="COVA naming series repaired",
			message="Counters were behind the names already issued: " + ", ".join(repaired),
		)


# ─── master data ──────────────────────────────────────────────────────────

# The packages the integration itself raises. Clinic Test Request.test_package
# used to be a Select carrying exactly this list; it is a Link to Test Package
# now, so the list has to exist as records or the requests api.py creates —
# "Exit Medical" on deactivation, "Pre Employment Wellness" on a Job Offer,
# "Annual Medical" on the statutory sweep — fail link validation.
#
# Anything beyond these five is site data: added in the desk, or minted from
# what was already on the requests by the create_test_package_records patch.
STANDARD_TEST_PACKAGES = (
	"Pre Employment Wellness",
	"Cholinesterase",
	"Food Handler",
	"Annual Medical",
	"Exit Medical",
)


def ensure_test_packages():
	"""Create the packages api.py names in code. Idempotent — after_migrate runs
	it on every deploy, and an existing package is left exactly as the site has
	it (a cova_code or a Disabled tick set in the desk is never overwritten)."""
	for package in STANDARD_TEST_PACKAGES:
		if frappe.db.exists("Test Package", package):
			continue
		frappe.get_doc({"doctype": "Test Package", "package_name": package}).insert(
			ignore_permissions=True, ignore_if_duplicate=True
		)


def ensure_test_groups():
	"""Seed Cova Clinic Settings → Test Scheduling with the groups the
	statutory run used to hardcode. Only while the table is empty, so anything
	HR has set up is never touched, and only designations this site has."""
	from cova_clinic_integration.cova_clinic_integration.doctype.cova_clinic_settings.cova_clinic_settings import (
		DEFAULT_TEST_GROUPS,
	)

	settings = frappe.get_doc("Cova Clinic Settings")
	if settings.get("test_groups"):
		return
	for group, spec in DEFAULT_TEST_GROUPS.items():
		if not frappe.db.exists("Test Package", spec["test_package"]):
			continue
		for designation in spec["designations"]:
			if frappe.db.exists("Designation", designation):
				settings.append(
					"test_groups",
					{"group_name": group, "test_package": spec["test_package"], "designation": designation},
				)
	if settings.get("test_groups"):
		settings.flags.ignore_mandatory = True
		settings.save(ignore_permissions=True)


def install_gate_pass_link():
	"""Tie clinic tickets to upande_ta's Gate Pass on sites that have it.

	cova_clinic_integration does not require upande_ta, so neither the
	``gate_pass`` link on Clinic Ticket nor the sidebar entry can ship in the
	standard JSON — a Link to a doctype the site lacks will not sync. Both are
	added here instead, and only where Gate Pass exists."""
	if not frappe.db.exists("DocType", "Gate Pass"):
		return

	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(
		{
			"Clinic Ticket": [
				{
					"fieldname": "gate_pass",
					"fieldtype": "Link",
					"label": "Gate Pass",
					"options": "Gate Pass",
					"insert_after": "issued_by",
					"no_copy": 1,
					"read_only": 1,
				}
			],
			# Which ticket a pass was raised from — one ticket, one live pass.
			"Gate Pass": [
				{
					"fieldname": "clinic_ticket",
					"fieldtype": "Link",
					"label": "Clinic Ticket",
					"options": "Clinic Ticket",
					"insert_after": "pass_type",
					"depends_on": "eval:doc.clinic_ticket",
					"read_only": 1,
					"no_copy": 1,
					"search_index": 1,
				}
			],
		},
		update=True,
	)

	if not frappe.db.exists("Workspace Sidebar", "Cova Clinic"):
		return
	sidebar = frappe.get_doc("Workspace Sidebar", "Cova Clinic")
	if any(i.link_to == "Gate Pass" for i in sidebar.items):
		return
	labels = [i.label for i in sidebar.items]
	at = labels.index("Clinic Ticket") + 1 if "Clinic Ticket" in labels else len(labels)
	row = sidebar.append(
		"items",
		{
			"type": "Link",
			"label": "Gate Pass",
			"link_type": "DocType",
			"link_to": "Gate Pass",
			"icon": "unlock",
			"collapsible": 1,
			"open_in_new_tab": 1,
		},
	)
	sidebar.items.remove(row)
	sidebar.items.insert(at, row)
	for i, row in enumerate(sidebar.items):
		row.idx = i + 1
	sidebar.flags.ignore_permissions = True
	sidebar.save()


def after_install():
	"""Single entry point so hooks name one thing per lifecycle event."""
	install_clinic_fields()
	install_clinic_links()
	repair_naming_series()
	ensure_test_packages()
	ensure_test_groups()
	install_gate_pass_link()


def before_uninstall():
	uninstall_clinic_links()
	uninstall_clinic_fields()


def uninstall_clinic_fields():
	"""Remove the integration's fields (and their columns) on uninstall."""
	for doctype, specs in CLINIC_FIELDS.items():
		fieldnames = {s["fieldname"] for s in specs}
		dt = frappe.get_doc("DocType", doctype)
		kept = [f for f in dt.fields if f.fieldname not in fieldnames]
		if len(kept) != len(dt.fields):
			dt.set("fields", kept)
			for i, f in enumerate(dt.fields):
				f.idx = i + 1
			_save_standard_doctype(dt)
			frappe.clear_cache(doctype=doctype)

		# Drop the physical columns for a clean uninstall.
		table_cols = set(frappe.db.get_table_columns(doctype))
		for fn in fieldnames:
			if fn in table_cols:
				try:
					frappe.db.sql_ddl("ALTER TABLE `tab{0}` DROP COLUMN `{1}`".format(doctype, fn))
				except Exception:
					frappe.log_error(
						title="COVA uninstall drop column",
						message=frappe.get_traceback(),
					)

	frappe.db.commit()


# ─── tests ────────────────────────────────────────────────────────────────

TEST_COMPANY = "_Test Company"


def _make_test_company():
	"""Put a Company on the site so the Employee fixtures can be built.

	Installing ERPNext does not create one — the setup wizard does, and CI never
	runs it. This is the same route hrms takes in its own CI.
	"""
	from frappe.utils import now_datetime

	year = now_datetime().year
	args = {
		"currency": "KES",
		"full_name": "Test User",
		"company_name": TEST_COMPANY,
		"timezone": "Africa/Nairobi",
		"company_abbr": "_TC",
		"country": "Kenya",
		"fy_start_date": "{0}-01-01".format(year),
		"fy_end_date": "{0}-12-31".format(year),
		"language": "english",
		"company_tagline": "Testing",
		"email": "test@example.com",
		"password": "test",
		"chart_of_accounts": "Standard",
	}

	try:
		from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

		setup_complete(args)
	except Exception:
		frappe.log_error(title="COVA before_tests setup wizard", message=frappe.get_traceback())
		frappe.db.rollback()

	if frappe.db.exists("Company", TEST_COMPANY):
		return

	# The wizard returns early on a site already flagged as set up, and swallows
	# its own stage failures. ERPNext's programmatic entry point does neither: it
	# installs the presets and the company directly, and raises on failure — so a
	# site that cannot be prepared fails the run here rather than in every test.
	from erpnext.setup.setup_wizard.setup_wizard import setup_complete as erpnext_setup_complete

	erpnext_setup_complete(args)


def _ensure_genders():
	"""The Employee fixtures set ``gender``, a Link to Gender. Those records are
	part of the setup wizard's fixtures, so a site that never ran it has none."""
	for gender in ("Male", "Female", "Other"):
		if not frappe.db.exists("Gender", gender):
			frappe.get_doc({"doctype": "Gender", "gender": gender}).insert(
				ignore_permissions=True, ignore_if_duplicate=True
			)


def before_tests():
	"""Prepare a freshly reinstalled site for this app's test suite.

	Run once by ``bench run-tests --app cova_clinic_integration``. Two things the
	suite needs are absent on a bare CI site:

	  * a Company and the Gender records — every Employee fixture needs both,
	    and both are laid down by the setup wizard, not by installing ERPNext;
	  * this app's fields and Connections on Employee/Job Offer, which no bare
	    site carries until ``after_install`` writes them.

	``after_install`` is idempotent, so re-running it here just asserts the
	wiring is present rather than installing it a second time.
	"""
	frappe.clear_cache()

	if not frappe.db.count("Company"):
		_make_test_company()

	_ensure_genders()

	after_install()

	frappe.db.commit()  # nosemgrep


# ─── boot ─────────────────────────────────────────────────────────────────


def extend_bootinfo(bootinfo):
	"""Publish the configured company so form/list scripts can gate on it
	without hardcoding a company name."""
	bootinfo.cova_clinic_company = get_clinic_company()


# ─── Job Offer ────────────────────────────────────────────────────────────

REQUIRED_FIELDS = ("national_id", "phone_number")


def job_offer_validate(doc, method=None):
	"""Make the COVA biodata mandatory when the offer is for the company picked
	in Cova Clinic Settings. Offers for any other company are untouched."""
	company = get_clinic_company()
	if not company or doc.get("company") != company:
		return

	meta = frappe.get_meta("Job Offer")
	missing = [
		meta.get_label(fieldname)
		for fieldname in REQUIRED_FIELDS
		if meta.has_field(fieldname) and not (doc.get(fieldname) or "").strip()
	]

	if missing:
		frappe.throw(
			_("{0} is required for {1} job offers.").format(
				frappe.bold(", ".join(missing)), frappe.bold(company)
			),
			frappe.MandatoryError,
			title=_("Missing Values Required"),
		)
