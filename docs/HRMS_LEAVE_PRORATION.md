# Part 13: incorrect pro-rated allocation of an event-based leave

**Reported:** a mid-year joiner received a pro-rated allocation for Maternity, Paternity or Marriage leave. These have a fixed entitlement and should be granted in full.

**Leave types:** Annual (pro-rated by joining date), Maternity, Paternity and Marriage (fixed).

## Root cause

HRMS v16.5.4, `hrms/hr/doctype/leave_policy_assignment/leave_policy_assignment.py`:

- **How the allocation is made.** `on_submit` → `grant_leave_alloc_for_employee()` loops over every row of the Leave Policy and calls `create_leave_allocation()` → `get_new_leaves()`.
- **Where proration happens.** `get_new_leaves()` has three branches:
  - compensatory → 0;
  - earned leave → accrual;
  - **everything else →** `calculate_pro_rated_leaves(annual_allocation, date_of_joining, effective_from, effective_to)`.

  The comment in the code says it literally: *"calculate pro-rated leaves for other leave types"*.
- **The calculation.** `calculate_pro_rated_leaves()` returns the full amount only if `date_of_joining <= effective_from`. Otherwise it multiplies by `(to − doj + 1) / (to − from + 1)` and rounds.
- **Worked example.** With a 2026 calendar leave period and a joining date of 1 July, Maternity 90 becomes `round(90 × 184/365) = 45`, and Annual 18 becomes 9.

So every non-earned leave type in the policy is pro-rated the same way, whether it's an accruing annual leave or an event-based entitlement.

- **No setting prevents it.** Leave Type in v16 has no field that disables proration (`is_earned_leave`, `is_compensatory`, `rounding`, `allocate_on_day` and so on are the only ones `get_leave_type_details()` reads), and HR Settings has no proration flag.
- **Reproduced by a test:** `tests/test_leave_proration.py::test_root_cause_unflagged_fixed_leave_is_pro_rated` asserts the 45.

## How to investigate (and rule out other causes)

| Check | Document or field | What it would mean |
|---|---|---|
| Ratio of allocation to annual ≈ (period end − DOJ + 1) / period length | Leave Allocation `new_leaves_allocated`; Leave Policy Assignment `effective_from/to`; Employee `date_of_joining` | Confirms this root cause |
| Leave Type is marked *Is Earned Leave* by mistake | Leave Type; Earned Leave Schedule rows on the allocation; non-integer values | A configuration error |
| Wrong or later-edited joining date | Employee Version history | A data error |
| `max_leaves_allowed` caps it, or carry-forward inflates it | Leave Type; Allocation `unused_leaves` | Not proration |
| Allocation skipped entirely (the pro-rated value rounded to 0) | Comment on the Leave Policy Assignment | A late joiner |
| Manual edit after submit | Leave Ledger Entry, Version | Not system logic |

## Options

1. **Configuration only.**
   - **Give event-based types their own policy, assigned from the joining date.** Not viable next to the annual policy: `validate_policy_assignment_overlap` rejects two overlapping submitted assignments for the same employee, whatever the policy.
   - **Take event-based types out of the policy** and allocate them through the Leave Control Panel or a manual Leave Allocation (no proration) when the event occurs. This works and needs no code, but it is a manual process that people forget.
2. **Customisation in the custom app, with no core change (implemented).**
   - A custom field on Leave Type, *Fixed Entitlement (No Proration)*, shipped as a fixture.
   - A class extension through `extend_doctype_class` on *Leave Policy Assignment* that overrides only `get_new_leaves`. For flagged types it returns the full policy allocation; for everything else it calls `super()`, so HRMS's behaviour is unchanged.
   - Frappe puts extension classes before the base class in the MRO (`frappe/model/base_document.py`), so this is a supported seam.
   - It also covers late joiners, whose pro-rated value would round to 0 and whose allocation HRMS would skip altogether.
3. **A doc_event on Leave Allocation `before_validate`.** It works for most cases, but misses the zero-skip case, and the assignment still shows the pro-rated number. Rejected.
4. **Editing HRMS.** Not justified, because a supported extension point exists.

**Configuration or customisation?** A manual workaround exists (1b). A per-leave-type "fixed entitlement" rule needs this small customisation: one custom field plus one method override, both in `reno_order`.

- Code: `reno_order/api/v1/leave_policy_assignment.py`; hooks `extend_doctype_class` and `doc_events["Leave Type"]`.
- A Leave Type `validate` rule rejects combining the flag with Earned or Compensatory leave.
- **Existing wrong allocations are not recalculated.** Correct them by amending, or by editing `new_leaves_allocated` after submit; HRMS writes the delta as a Leave Ledger Entry.

## Tests (`tests/test_leave_proration.py`)

The period is 2026-01-01 to 2026-12-31. The policy has Annual 18, Maternity 90 (flagged) and Marriage 90 (unflagged, the control).

| Test | Joiner | Expects |
|---|---|---|
| `test_root_cause_unflagged_fixed_leave_is_pro_rated` | 1 Jul | unflagged 90 → **45** (the bug) |
| `test_fixed_entitlement_is_full_for_mid_year_joiner` | 1 Jul | Maternity **90** |
| `test_other_leave_is_still_pro_rated` | 1 Jul | Annual **9** (HRMS unchanged) |
| `test_joiner_before_the_period_gets_everything` | 2025 | 18 / 90 / 90 |
| `test_very_late_joiner_is_not_skipped` | 28 Dec | Maternity 90, not skipped |
| `test_no_double_allocation` | 1 Jul | re-granting raises; one allocation |
| `test_flag_cannot_combine_with_earned_leave` | n/a | ValidationError |
