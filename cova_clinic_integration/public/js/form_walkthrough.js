// Copyright (c) 2026, Upande Limited and contributors
// For license information, please see license.txt
//
// Keeps the Cova Clinic form walkthroughs usable.
//
// Two problems with driver.js as frappe drives it, both of which strand the
// user behind an overlay they cannot dismiss (frappe builds the driver with
// `allowClose: false`):
//
//   1. The highlight is positioned once, when the step is shown, and driver.js
//      never repositions it afterwards. Our tours make that certain to bite:
//      step 1 of the Job Offer walkthrough asks the user to set **Company**,
//      and doing so reveals the whole COVA Pre-Employment section — every field
//      below it shifts down, the highlight stays where it was, and the popover
//      ends up over empty space. The fix is to refresh the driver whenever the
//      form's layout actually changes size.
//
//   2. A tour is only ever auto-started from the onboarding panel, on a form
//      that is still settling. If it starts crooked there is no way to restart
//      it. The Walk Me Through button gives one, on a fully rendered form.

frappe.provide("cova_clinic");

cova_clinic.WALKTHROUGHS = {
	"Job Offer": "Cova Pre-Employment on Job Offer",
	"Cova Members": "Cova Member Walkthrough",
	"Clinic Test Request": "Clinic Test Request Walkthrough",
	"Clinic Test Result": "Clinic Test Result Walkthrough",
	"Clinic Visit Cost": "Clinic Visit Cost Walkthrough",
	"Clinic Checkin": "Clinic Checkin Walkthrough",
	"Medical Case": "Medical Case Walkthrough",
	"Health Monthly Report": "Health Monthly Report Walkthrough",
	"Cova Clinic Settings": "Cova Clinic Settings Walkthrough",
};

// Number every popover: "2/8 · Link the applicant", so it is clear how far in
// you are and how much is left.
//
// Done by wrapping build_steps rather than by editing the Form Tour titles,
// because frappe injects steps of its own (add a row, collapse a row, save) —
// the real total is only known once the steps are built. Scoped to this app's
// walkthroughs so other apps' tours are left alone.
cova_clinic.number_walkthrough_steps = function () {
	const FormTour = frappe.ui && frappe.ui.form && frappe.ui.form.FormTour;
	if (!FormTour || FormTour.prototype.__cova_numbered) {
		return;
	}
	FormTour.prototype.__cova_numbered = true;

	const build_steps = FormTour.prototype.build_steps;
	FormTour.prototype.build_steps = function () {
		build_steps.apply(this, arguments);

		const doctype = this.frm && this.frm.doctype;
		if (!doctype || !cova_clinic.WALKTHROUGHS[doctype]) {
			return;
		}

		cova_clinic.reanchor_hidden_steps(this);

		const total = this.driver_steps.length;
		this.driver_steps.forEach(function (step, i) {
			if (!step.popover || !step.popover.title) {
				return;
			}
			// Titles are already written as "2. Link the applicant"; drop that
			// hand-kept number so it is not printed twice, and so it cannot
			// disagree with the real position once frappe adds its own steps.
			const title = String(step.popover.title).replace(/^\s*\d+\.\s*/, "");
			step.popover.title = `${i + 1}/${total} · ${title}`;
		});
	};
};

// A step whose field is not on screen.
//
// On Job Offer the whole COVA Pre-Employment section is gated:
//   cova_section.depends_on = "eval:doc.company && doc.company===frappe.boot.cova_clinic_company"
// The fields inside carry no depends_on of their own, so field-level meta looks
// clear — it is the section that hides them. Until a Company is picked, National
// ID, Phone Number, Date of Birth and Gender have wrappers in the DOM that
// render at 0x0. driver.js resolves such an anchor happily and then draws the
// stage over nothing with the popover positioned off it.
//
// So point the step at the field the person is already looking at — the last one
// on screen — and say the field is not showing yet. The explanation is the part
// worth having and it survives.
//
// Re-runnable on purpose: the original anchor is remembered, so once Company is
// set and the section appears, each step goes back to its real field.
cova_clinic.reanchor_hidden_steps = function (tour) {
	const form_node =
		(tour.frm && tour.frm.layout && tour.frm.layout.wrapper && tour.frm.layout.wrapper.get(0)) ||
		null;

	const rendered = function (node) {
		// offsetParent is null for display:none on the node or any ancestor, which
		// is how frappe hides the fields of a section whose depends_on is false.
		return !!(node && node.offsetParent !== null);
	};

	// Every floating step needs an anchor node of its OWN.
	//
	// driver.js skips the move entirely when the next step's node is the one
	// already highlighted (Overlay.highlight bails on Element.isSame). Point four
	// consecutive hidden fields at the same form wrapper and Next stops working
	// on the first of them — the popover never changes and the tour looks frozen.
	//
	// So each floating step gets its own invisible one-pixel marker, fixed to the
	// middle of the viewport. Distinct nodes mean the driver always sees a move,
	// and mid-center puts the card over the middle of the screen.
	const floating_anchor = function (step) {
		if (step.__cova_pad && step.__cova_pad.isConnected) {
			return step.__cova_pad;
		}
		const pad = document.createElement("div");
		pad.className = "cova-tour-anchor";
		pad.setAttribute("aria-hidden", "true");
		pad.style.cssText =
			"position:fixed;left:50%;top:45%;width:1px;height:1px;opacity:0;pointer-events:none;";
		document.body.appendChild(pad);
		step.__cova_pad = pad;
		return pad;
	};

	// Read-only counts as "nothing to do here" too: the tour should say what the
	// field means without pointing at a box the person cannot type in.
	const read_only = function (node) {
		if (!node || !node.getAttribute) {
			return false;
		}
		const fieldname = node.getAttribute("data-fieldname");
		const field = fieldname && tour.frm && tour.frm.get_field(fieldname);
		return !!(field && field.df && (field.df.read_only || field.disp_status === "Read"));
	};

	// Remember what each step was originally aimed at and how it read, once.
	tour.driver_steps.forEach(function (step) {
		if (step.__cova_anchor === undefined) {
			step.__cova_anchor = step.element;
			step.__cova_description = (step.popover && step.popover.description) || "";
			step.__cova_position = (step.popover && step.popover.position) || "bottom";
		}
	});

	const nodes = tour.driver_steps.map(function (step) {
		const src = step.__cova_anchor;
		return typeof src === "string" ? document.querySelector(src) : src;
	});

	let changed = false;

	tour.driver_steps.forEach(function (step, i) {
		const node = nodes[i];
		const hidden = !rendered(node);
		const locked = !hidden && read_only(node);
		const floating = hidden || locked;

		// A field with nothing to point at becomes a floating card: its own
		// invisible marker in the middle of the screen, with the white stage
		// turned transparent so no box is drawn over a field that is not there
		// (or that the person cannot edit anyway). The dimmed page behind it is
		// what makes it read as a modal.
		const anchor = floating ? floating_anchor(step) : node;

		if (step.element !== anchor) {
			step.element = anchor;
			changed = true;
		}

		const position = floating ? "mid-center" : step.__cova_position;
		const stage = floating ? "transparent" : "#ffffff";
		if (step.stageBackground !== stage) {
			step.stageBackground = stage;
			changed = true;
		}

		if (step.popover) {
			if (step.popover.position !== position) {
				step.popover.position = position;
				changed = true;
			}
			// Rebuilt from the pristine text each time, so a note appears while the
			// field is out of reach and disappears once it is editable.
			let note = "";
			if (hidden) {
				note =
					"<br><br><i>This field is not on the form yet — it appears once the " +
					"fields it depends on are filled in.</i>";
			} else if (locked) {
				note = "<br><br><i>Read-only — it fills itself in, there is nothing to type here.</i>";
			}
			step.popover.description = step.__cova_description + note;
		}
	});

	// A step with no anchor at all would be dropped by driver.js, and a dropped
	// step is what makes the counter disagree with what the popovers claim.
	const kept = tour.driver_steps.filter(function (step) {
		return !!step.element;
	});
	if (kept.length !== tour.driver_steps.length) {
		tour.driver_steps = kept;
		changed = true;
	}

	return changed;
};

// Re-aim the steps at what is on screen right now, and hand the driver the new
// definitions. Called when the form's layout changes, which is exactly when a
// gated section appears or disappears under the tour.
cova_clinic.retarget_walkthrough = function (frm) {
	const tour = frm && frm.tour;
	const driver = tour && tour.driver;
	if (!driver || !driver.isActivated || !tour.driver_steps || !tour.driver_steps.length) {
		return;
	}

	if (!cova_clinic.reanchor_hidden_steps(tour)) {
		return; // nothing moved, leave the tour alone
	}

	const index = Math.min(driver.currentStep, tour.driver_steps.length - 1);
	// update_driver_steps() is frappe's own path and takes definitions, which is
	// the supported way to redefine — unlike feeding driver.steps back in.
	tour.update_driver_steps();
	driver.start(index);
};

// Has this person already been all the way through this form's tour?
//
// Kept in the browser rather than on the server. That makes it per browser: it
// survives reloads, and clearing site data brings the button back — which is
// how you get the tour again without an admin unsetting anything. The key
// carries the user, so two people sharing a machine do not inherit each other's.
//
// Only a tour walked to the end counts. Closing early leaves the button, or
// someone who bailed at step 2 could never find their way back to it.
cova_clinic.walkthrough_key = function (frm) {
	const user = (frappe.session && frappe.session.user) || "guest";
	return "cova_walkthrough_done::" + user + "::" + frm.doctype;
};

cova_clinic.walkthrough_seen = function (frm) {
	try {
		return window.localStorage.getItem(cova_clinic.walkthrough_key(frm)) === "1";
	} catch (e) {
		// Private windows and blocked site data throw on access. Showing the
		// button is the safe answer — it costs a click, hiding it wrongly costs
		// the walkthrough entirely.
		return false;
	}
};

cova_clinic.mark_walkthrough_seen = function (frm) {
	try {
		window.localStorage.setItem(cova_clinic.walkthrough_key(frm), "1");
	} catch (e) {
		console.warn("cova walkthrough: could not remember completion", e);
	}
	if (frm.__cova_overview_btn) {
		frm.__cova_overview_btn.remove();
		frm.__cova_overview_btn = null;
	}
};

// Bring every tour back on this browser, for when you want to walk them again.
// Run in the browser console: cova_clinic.reset_walkthroughs()
cova_clinic.reset_walkthroughs = function () {
	let cleared = 0;
	try {
		// Collected first, then removed: deleting while walking the index would
		// renumber the remaining keys underneath the loop.
		const store = window.localStorage;
		const doomed = [];
		for (let i = 0; i < store.length; i++) {
			const key = store.key(i);
			if (key && key.indexOf("cova_walkthrough_done::") === 0) {
				doomed.push(key);
			}
		}
		doomed.forEach(function (key) {
			store.removeItem(key);
			cleared++;
		});
	} catch (e) {
		console.warn("cova walkthrough: could not clear", e);
	}
	frappe.show_alert({
		message: __("Walkthroughs reset ({0}). Reload the page to see the i button again.", [cleared]),
		indicator: "green",
	});
	return cleared;
};


// A tour that has been walked to the end must stay closed.
//
// The onboarding panel starts a tour whenever it opens the form, so finishing
// one and landing back on the form started it over at 1/8. Remember that this
// form's tour is done, and let only an explicit request — the Clinic > Overview
// button — start it again.
cova_clinic.block_walkthrough_restart = function () {
	const FormTour = frappe.ui && frappe.ui.form && frappe.ui.form.FormTour;
	if (!FormTour || FormTour.prototype.__cova_no_restart) {
		return;
	}
	FormTour.prototype.__cova_no_restart = true;

	const init = FormTour.prototype.init;
	FormTour.prototype.init = function (opts) {
		const frm = this.frm;
		if (frm && cova_clinic.WALKTHROUGHS[frm.doctype]) {
			const theirs = opts && opts.on_finish;
			opts = Object.assign({}, opts || {}, {
				on_finish: function () {
					if (theirs) {
						theirs();
					}
					frm.__cova_walkthrough_done = true;
					cova_clinic.mark_walkthrough_seen(frm);
					cova_clinic.close_walkthrough(frm);
				},
			});
		}
		return init.call(this, opts);
	};

	const start = FormTour.prototype.start;
	FormTour.prototype.start = function () {
		const frm = this.frm;
		const ours = frm && cova_clinic.WALKTHROUGHS[frm.doctype];
		if (ours && frm.__cova_walkthrough_done && !frm.__cova_walkthrough_requested) {
			return;
		}
		if (frm) {
			frm.__cova_walkthrough_requested = false;
		}
		return start.apply(this, arguments);
	};
};

cova_clinic.number_walkthrough_steps();
cova_clinic.block_walkthrough_restart();

// Tear the overlay off the page by hand. driver.reset() is the polite way out,
// but it only works while the driver's own state is intact — if the tour has
// already broken, its nodes are still in the DOM with nothing left to remove
// them, and the page sits dimmed and unclickable forever.
cova_clinic.clear_walkthrough_overlay = function () {
	["#driver-page-overlay", "#driver-highlighted-element-stage", "#driver-popover-item"].forEach(
		function (selector) {
			$(selector).remove();
		}
	);
	$(".driver-fix-stacking").removeClass("driver-fix-stacking");
	$(".driver-highlighted-element").removeClass("driver-highlighted-element");
	// The invisible markers the floating steps were anchored to.
	$(".cova-tour-anchor").remove();
};

// Always leave a way out. frappe builds the driver with `allowClose: false`, so
// without this there is no gesture at all that dismisses a tour — which is only
// tolerable while the tour still works.
cova_clinic.close_walkthrough = function (frm) {
	const driver = frm && frm.tour && frm.tour.driver;
	if (driver) {
		try {
			driver.reset(true);
		} catch (e) {
			// Broken driver state — the DOM sweep below is the fallback.
			console.warn("cova walkthrough reset failed", e);
		}
	}
	cova_clinic.clear_walkthrough_overlay();
};

cova_clinic.make_walkthrough_escapable = function (frm) {
	if (frm.__cova_walkthrough_escape) {
		return;
	}
	frm.__cova_walkthrough_escape = true;

	// Namespaced so the handlers go with the form when it is torn down.
	const ns = ".cova_walkthrough_" + frm.docname;

	// Escape is the way out. Deliberately the ONLY one.
	//
	// Do not also close on a backdrop click: frappe builds the driver with
	// `overlayClickNext: true`, so the dimmed area already means "next". Binding
	// close to it made a single click do both — the driver advanced and redrew
	// the stage while this handler tore the popover node out of the DOM. The
	// driver's Popover object kept its reference to that detached node, so every
	// step after it rendered a stage with no popover and the tour was stuck.
	$(document).on("keydown" + ns, function (e) {
		if (e.key === "Escape") {
			cova_clinic.close_walkthrough(frm);
		}
	});

	$(frm.page.wrapper).on("remove", function () {
		$(document).off(ns);
	});
};

// A tour the user can no longer act on is just a dimmed page, so take it down.
// Two ways that happens:
//
//   * the driver has no steps left — nothing to walk, advance or finish;
//   * a stage is on screen with no popover beside it. driver.js draws the stage
//     unconditionally but skips a null popover, so this state offers no Next,
//     no Close and no text — the tour is unusable even though it looks live.
//
// The popover check is confirmed a second time before acting: it is briefly
// true during a normal step transition, and closing then would cut a working
// tour short.
cova_clinic.dismiss_if_dead = function (frm) {
	const driver = frm && frm.tour && frm.tour.driver;
	if (!driver || !driver.isActivated) {
		return false;
	}

	if (!driver.steps || !driver.steps.length) {
		console.warn("cova walkthrough lost its steps — closing the overlay");
		cova_clinic.close_walkthrough(frm);
		return true;
	}

	const stranded = function () {
		return (
			document.getElementById("driver-highlighted-element-stage") &&
			!document.getElementById("driver-popover-item")
		);
	};

	if (stranded() && !frm.__cova_walkthrough_stranded_check) {
		frm.__cova_walkthrough_stranded_check = setTimeout(function () {
			frm.__cova_walkthrough_stranded_check = null;
			const live = frm.tour && frm.tour.driver;
			if (live && live.isActivated && stranded()) {
				cova_clinic.report_walkthrough_state(frm, "stage on screen with no popover");
				cova_clinic.close_walkthrough(frm);
			}
		}, 900);
	}

	return false;
};

// Printed whenever a tour is closed for being unusable. Without this the only
// evidence is a screenshot, and a screenshot cannot tell you which step the
// driver thinks it is on or whether the step ever had a popover to show.
cova_clinic.report_walkthrough_state = function (frm, why) {
	const driver = frm && frm.tour && frm.tour.driver;
	const step = driver && driver.steps && driver.steps[driver.currentStep];
	const defs = (frm.tour && frm.tour.driver_steps) || [];

	console.warn(
		"[cova walkthrough] closing — " + why + "\n" +
			JSON.stringify(
				{
					doctype: frm.doctype,
					tour: cova_clinic.WALKTHROUGHS[frm.doctype],
					step_index: driver ? driver.currentStep : null,
					steps_built: driver && driver.steps ? driver.steps.length : null,
					steps_defined: defs.length,
					// A definition whose anchor did not resolve is dropped by
					// driver.js, so these two numbers disagreeing is the tell.
					dropped: defs.length - ((driver && driver.steps && driver.steps.length) || 0),
					current_has_popover: !!(step && step.popover),
					current_node: step && step.node ? step.node.getAttribute("data-fieldname") : null,
					titles: defs.map(function (d) {
						return d.popover && d.popover.title;
					}),
				},
				null,
				2
			)
	);
};

// Re-align the highlight when the form grows or shrinks under it.
cova_clinic.keep_walkthrough_aligned = function (frm) {
	if (frm.__cova_walkthrough_watch || typeof ResizeObserver === "undefined") {
		return;
	}
	const layout = frm.layout && frm.layout.wrapper && frm.layout.wrapper.get(0);
	if (!layout) {
		return;
	}
	frm.__cova_walkthrough_watch = true;

	let pending = null;
	const observer = new ResizeObserver(function () {
		const driver = frm.tour && frm.tour.driver;
		// Only while a tour is actually on screen — refreshing an inactive
		// driver would re-draw an overlay nobody asked for.
		if (!driver || !driver.isActivated) {
			return;
		}
		// Debounced: a section reveal fires several resizes as it settles, and
		// repositioning against a mid-animation height puts the highlight in
		// the wrong place all over again.
		clearTimeout(pending);
		pending = setTimeout(function () {
			if (cova_clinic.dismiss_if_dead(frm)) {
				return;
			}
			try {
				// A gated section appearing or disappearing is a layout change, so
				// this is exactly the moment to re-aim the steps at what is on
				// screen. Only redefines when an anchor actually moved.
				cova_clinic.retarget_walkthrough(frm);

				// refresh() re-measures the highlighted node and moves the overlay
				// and popover onto it, which is the whole job here.
				//
				// Do NOT also call defineSteps(driver.steps) to "re-query" the
				// anchors: defineSteps takes step *definitions*, while
				// driver.steps holds prepared Element instances. Feeding those
				// back in throws "Element is required in step 0" — after
				// defineSteps has already emptied the list — which leaves the
				// tour with no steps at all and kills it mid-walk.
				driver.refresh();
			} catch (e) {
				// A highlight that cannot be repositioned is not worth trapping
				// the page for: take the overlay down rather than leave it stuck.
				console.warn("cova walkthrough refresh failed", e);
				cova_clinic.close_walkthrough(frm);
			}
		}, 150);
	});
	observer.observe(layout);

	frm.__cova_walkthrough_observer = observer;

	// The health check above only runs when something resizes. A tour that
	// strands itself without changing the layout would never be checked at all,
	// so poll for as long as one is on screen. Cheap: two getElementById calls.
	if (!frm.__cova_walkthrough_poll) {
		let orphaned_for = 0;
		frm.__cova_walkthrough_poll = setInterval(function () {
			const live = frm.tour && frm.tour.driver;
			if (live && live.isActivated) {
				orphaned_for = 0;
				cova_clinic.dismiss_if_dead(frm);
				return;
			}

			// A popover on screen means a tour IS running, whatever this form's
			// tour object currently says. frm.tour is rebuilt when the form
			// re-renders, so the driver reference above goes stale under a live
			// tour — and sweeping on that alone tore down the tour the user was
			// reading, a second after they opened it.
			if (document.getElementById("driver-popover-item")) {
				orphaned_for = 0;
				return;
			}

			const debris =
				document.getElementById("driver-page-overlay") ||
				document.getElementById("driver-highlighted-element-stage") ||
				document.querySelector(".cova-tour-anchor");

			if (!debris) {
				orphaned_for = 0;
				return;
			}

			// Overlay with no popover and no active driver: either genuinely
			// abandoned, or a step transition caught mid-flight. Only the first
			// survives a second look.
			orphaned_for += 1;
			if (orphaned_for >= 2) {
				orphaned_for = 0;
				cova_clinic.clear_walkthrough_overlay();
			}
		}, 1000);
		$(frm.page.wrapper).on("remove", function () {
			clearInterval(frm.__cova_walkthrough_poll);
			frm.__cova_walkthrough_poll = null;
		});
	}
};

cova_clinic.start_walkthrough = function (frm) {
	const tour_name = cova_clinic.WALKTHROUGHS[frm.doctype];
	if (!tour_name || !frm.tour) {
		return;
	}
	cova_clinic.keep_walkthrough_aligned(frm);
	cova_clinic.make_walkthrough_escapable(frm);

	// Walking off the end must leave a clean form. driver.js resets itself when
	// Next runs out of steps, but a tour with save_on_complete gets a Save step
	// appended whose popover has no buttons — it waits for a real save, so
	// abandoning there would leave the page dimmed indefinitely.
	const finish = function () {
		frm.__cova_walkthrough_done = true;
		cova_clinic.close_walkthrough(frm);
		// Once the fields have been explained, point at the action that uses
		// them — that is the next thing the person actually has to press.
		setTimeout(function () {
			cova_clinic.highlight_clinic_button(frm);
		}, 300);
	};

	frm.tour
		.init({ tour_name: tour_name, on_finish: finish })
		.then(function () {
			const driver = frm.tour && frm.tour.driver;
			if (driver) {
				// Covers the other way out: driver resetting itself on the last
				// Next, which does not run on_finish.
				const original_on_destroy = driver.onDestroy;
				driver.onDestroy = function () {
					if (original_on_destroy) {
						original_on_destroy.call(this);
					}
					frm.__cova_walkthrough_done = true;
					setTimeout(function () {
						cova_clinic.clear_walkthrough_overlay();
						cova_clinic.highlight_clinic_button(frm);
					}, 300);
				};
			}
			frm.__cova_walkthrough_requested = true;
			frm.tour.start();
		})
		.catch(function (e) {
			frappe.msgprint({
				title: __("Walkthrough unavailable"),
				message: __("Could not load {0}.", [tour_name]),
				indicator: "orange",
			});
			console.error(e);
		});
};

Object.keys(cova_clinic.WALKTHROUGHS).forEach(function (doctype) {
	frappe.ui.form.on(doctype, {
		refresh: function (frm) {
			// Armed even when no tour is running: the onboarding panel starts one
			// on load, before this handler could have set the observer up — and
			// that is exactly the tour nobody can dismiss if it goes wrong.
			cova_clinic.keep_walkthrough_aligned(frm);
			cova_clinic.make_walkthrough_escapable(frm);

			// A tour started by the onboarding panel can die before any resize
			// fires, leaving the form dimmed from the moment it loads.
			setTimeout(function () {
				cova_clinic.dismiss_if_dead(frm);
			}, 1500);

			// A standalone "i" button, not a dropdown and not inside the Clinic
			// menu: the Clinic menu is for doing things to the record, this only
			// explains the form. Gone for good once the tour has been completed.
			if (!cova_clinic.walkthrough_seen(frm)) {
				frm.__cova_overview_btn = frm
					.add_custom_button("i", function () {
						cova_clinic.start_walkthrough(frm);
					})
					.attr("title", __("Overview — walk through this page"))
					.addClass("cova-overview-btn");
			}
		},
	});
});

cova_clinic.highlight_clinic_button = function (frm) {
	if (!frm || frm.is_new()) {
		return;
	}
	// Find the Clinic button group and highlight the first button in it.
	const toolbar = frm.page.$page.find(".form-toolbar");
	if (!toolbar || !toolbar.length) {
		return;
	}
	// Look for a button with text matching Clinic actions
	const clinic_btns = toolbar.find("button").filter(function () {
		const text = $(this).text().toLowerCase();
		return text.includes("register") || text.includes("request") || text.includes("deactivate") || text.includes("wellness");
	});
	if (clinic_btns && clinic_btns.length > 0) {
		const first_btn = clinic_btns.first();
		first_btn.css({
			outline: "3px solid #ffb84d",
			outlineOffset: "2px",
			boxShadow: "0 0 0 4px rgba(255, 184, 77, 0.25)",
			transition: "all 0.3s ease",
		});
		// Pulse animation
		first_btn.animate({ boxShadow: "0 0 0 8px rgba(255, 184, 77, 0)" }, 1000);
	}
};
