# Copyright (c) 2026, Upande Limited and contributors
# For license information, please see license.txt

"""Append-only message trace for the COVA exchange, kept on the record itself.

``cova_raw`` used to hold a single blob — whatever COVA last sent — so a record
that went out, came back with an error and was re-sent showed only the final
state, and the support question "what did we actually send them?" had no answer
on the document. Every exchange is appended here instead, newest first, because
the field is read at a glance on the Data tab and the latest exchange is what
the reader is nearly always after.

The history is capped (``MAX_ENTRIES`` / ``MAX_CHARS``, oldest dropped first):
``cova_raw`` is a Long Text on a row that is loaded in full on every form view
and pulled into every list/report query that names it, so an uncapped trace on a
chatty record grows the row until the form is slow to open and a backup is
needlessly large. The cap keeps the useful recent history and nothing else.

Known limitation: the write joins the caller's transaction, so a request that
rolls back (a validation throw further down the handler) loses the entry it
appended. The trace is a convenience view on the document, not the system of
record — the ``frappe.log_error`` audit in ``api.py`` is written outside the
rolled-back work and is what survives that.
"""

import json
import re

import frappe

FIELD = "cova_raw"

MAX_ENTRIES = 50
MAX_CHARS = 100_000

# An entry starts at column 0 with its bracketed timestamp; anything ahead of the
# first such line is the legacy single blob this field used to hold.
_HEADER_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ", re.MULTILINE)


def record_cova_message(doctype, name, action, direction, payload, status=None):
	"""Append one COVA exchange to ``doctype``/``name``'s ``cova_raw``, newest first.

	``direction`` is "sent" or "received", ``action`` is the COVA action name,
	``payload`` is the body (any JSON-serialisable value) and ``status`` is an
	optional short word for the header line.

	Never raises: tracing must not be able to fail a COVA call, so an unknown
	doctype, a field that is not deployed yet and a missing document are all
	silent no-ops.
	"""
	try:
		if not doctype or not name:
			return

		try:
			meta = frappe.get_meta(doctype)
		except Exception:
			return  # doctype not on this site — nothing to trace onto

		if not meta.has_field(FIELD) or not frappe.db.exists(doctype, name):
			return

		existing = frappe.db.get_value(doctype, name, FIELD) or ""
		text = _compose(existing, _format_entry(action, direction, payload, status))

		# An audit trail on an existing row: it must not touch ``modified`` (which
		# would fake an edit and trip other users' "document was modified" checks)
		# and must not re-run the controller that is mid-flight above us.
		frappe.db.set_value(doctype, name, FIELD, text, update_modified=False)
	except Exception:
		frappe.log_error(title="COVA record_cova_message", message=frappe.get_traceback())


def _format_entry(action, direction, payload, status):
	# now() carries microseconds; the header is a line people scan, and trimming
	# keeps it the fixed width _HEADER_RE reads back.
	stamp = frappe.utils.now()[:19]
	parts = [str(direction or "?"), str(action or "?")]
	if status:
		parts.append(str(status))
	try:
		body = json.dumps(payload, indent=2, default=str)
	except Exception:
		# default=str covers datetimes and Decimals, not a cyclic or exotic object;
		# a repr still tells the reader what went over the wire.
		body = repr(payload)
	return "[%s] %s\n%s" % (stamp, " · ".join(parts), body)


def _split_entries(text):
	"""Existing entries newest-first, with any legacy blob last as the oldest."""
	if not text or not text.strip():
		return []

	starts = [m.start() for m in _HEADER_RE.finditer(text)]
	if not starts:
		return [text.strip()]

	legacy = text[: starts[0]].strip()
	bounds = [*starts, len(text)]
	entries = [text[bounds[i] : bounds[i + 1]].strip() for i in range(len(starts))]
	if legacy:
		entries.append(legacy)
	return [e for e in entries if e]


def _compose(existing, entry):
	entries = [entry, *_split_entries(existing)][:MAX_ENTRIES]

	text = "\n\n".join(entries)
	while len(text) > MAX_CHARS and len(entries) > 1:
		entries.pop()
		text = "\n\n".join(entries)
	# One oversized entry cannot be dropped without losing the newest exchange, so
	# it is cut instead.
	return text if len(text) <= MAX_CHARS else text[:MAX_CHARS]
