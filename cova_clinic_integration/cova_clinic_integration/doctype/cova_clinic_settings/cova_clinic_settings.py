# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class CovaClinicSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		access_token: DF.Text | None
		api_key: DF.Data | None
		base_url: DF.Data | None
		company: DF.Link | None
		deactivation_endpoint: DF.Data | None
		pre_employement_endpoint: DF.Data | None
		register_endpoint: DF.Data | None
		test_request_endpoint: DF.Data | None
	# end: auto-generated types

	pass


def get_clinic_company() -> str | None:
	"""The Company the COVA integration is configured for, or None when one has
	not been picked yet. Read with get_single_value so callers do not need read
	permission on the settings."""
	return frappe.db.get_single_value("Cova Clinic Settings", "company") or None
