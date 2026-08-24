# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document

from cova_clinic_integration.member_link import set_cova_member


class ClinicTestRequest(Document):

	def validate(self):
		# Keeps the member's Connections tab complete — see member_link.
		set_cova_member(self)
