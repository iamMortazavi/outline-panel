# Invariant register → the tests that hold it

Every row of §1.2 in `MODERNIZATION.md`, mapped to the test that would fail if
the guarantee were removed. `test_invariant_map.py` reads this file and asserts
each named test still exists, so deleting a guarantee is visible as a broken
map rather than as one fewer green dot.

Format: `ID | test_function_name | file`. Comment lines and blanks are ignored.

## Money

M1 | test_charge_refuses_what_cannot_be_afforded | tests/test_credit.py
M2 | test_ledger_never_drifts_from_the_balance | tests/test_credit.py
M3 | test_credit_cannot_be_set_directly_through_the_editor | tests/test_subadmins.py
M4 | test_a_repeated_purchase_charges_once_and_creates_one_key | tests/test_idempotency.py
M5 | test_a_failed_sale_gives_the_credit_back | tests/test_subadmins.py
M6 | test_history_survives_deleting_the_package | tests/test_credit.py
M7 | test_credit_admin_cannot_top_up_a_key_for_free | tests/test_hardening.py
M8 | test_a_credit_admin_may_put_a_customer_on_several_servers | tests/test_hardening.py

## Access

A-1 | test_an_empty_server_list_grants_nothing | tests/test_hardening.py
A-2 | test_a_sub_admin_sees_only_their_own_users | tests/test_subadmins.py
A-3 | test_a_mirrored_sub_belongs_to_whoever_owns_the_primary | tests/test_subadmins.py
A-4 | test_bot_refuses_a_key_on_an_out_of_scope_server | tests/test_hardening.py
A-5 | test_owner_only_surfaces_are_closed_to_sub_admins | tests/test_subadmins.py
A-6 | test_a_stranger_cannot_add_a_server_to_someone_elses_subscription | tests/test_sub_ownership.py
A-7 | test_editing_an_admin_takes_effect_immediately | tests/test_subadmins.py
A-8 | test_a_rejected_admin_leaves_no_row_behind | tests/test_hardening.py

## Time & quota

T1 | test_duration_starts_on_first_connection_by_default | tests/test_features.py
T3 | test_reset_usage | tests/test_features.py
T4 | test_monthly_quota_reaches_outline | tests/test_features.py
T6 | test_a_plan_that_has_not_started_does_not_read_as_never_expiring | tests/test_hardening.py
T7 | test_rotation_carries_the_used_bytes | tests/test_profile.py

## Integrity

I3 | test_a_half_applied_step_replays_cleanly | tests/test_migrations.py
I4 | test_two_schedulers_do_not_both_do_the_work | tests/test_multiworker.py
I2 | test_restore_rejects_garbage | tests/test_features.py
I5 | test_an_adopted_key_also_gets_a_link | tests/test_profile.py

## Public surface

P1 | test_the_profile_host_serves_only_the_profile | tests/test_profile.py
P2 | test_the_subscription_route_is_rate_limited_and_cached | tests/test_multiworker.py
P3 | test_rate_limit_not_bypassed_by_rotating_xff | tests/test_webapp.py
P4 | test_api_responses_are_not_cacheable | tests/test_webapp.py
O1 | test_rotation_is_audited | tests/test_profile.py

## Wire contract (the golden master itself)

W1 | test_golden_owner_reads | tests/test_golden.py
W2 | test_golden_reseller_reads | tests/test_golden.py
W3 | test_golden_public_subscription | tests/test_golden.py
W4 | test_golden_errors | tests/test_golden.py
W5 | test_golden_writes | tests/test_golden.py

## The subscription aggregate (A1 — fixed in step 3)

A1-1 | test_A1_1_suspending_a_customer_suspends_every_server | tests/test_invariant_a1_subscription.py
A1-2 | test_A1_2_resuming_a_customer_resumes_every_server | tests/test_invariant_a1_subscription.py
A1-3 | test_A1_3_renewing_moves_every_members_clock | tests/test_invariant_a1_subscription.py
A1-4 | test_A1_4_deleting_a_customer_removes_every_key | tests/test_invariant_a1_subscription.py
A1-5 | test_A1_5_allowance_changes_reach_every_server | tests/test_invariant_a1_subscription.py
A1-6 | test_the_monthly_quota_reaches_the_mirror | tests/test_invariant_a1_subscription.py

## Convergence (the outbox)

C1 | test_one_unreachable_server_does_not_block_the_others | tests/test_invariant_a1_subscription.py
C2 | test_nothing_is_written_when_nothing_reaches_a_server | tests/test_invariant_a1_subscription.py
C3 | test_bookkeeping_alone_is_not_a_total_failure | tests/test_invariant_a1_subscription.py
C4 | test_a_deferred_suspension_lands_when_the_server_comes_back | tests/test_convergence.py
C5 | test_a_still_failing_effect_backs_off_rather_than_spinning | tests/test_convergence.py
C6 | test_an_effect_for_a_removed_server_is_dropped | tests/test_convergence.py
C7 | test_a_key_made_in_outline_manager_is_never_touched | tests/test_convergence.py
C8 | test_drift_reports_a_key_deleted_upstream_without_recreating_it | tests/test_convergence.py
C9 | test_the_report_is_owner_only | tests/test_convergence.py

## Live stream

L1 | test_a_reseller_only_sees_their_own_customers | tests/test_stream.py
L2 | test_revoking_an_admin_closes_their_stream | tests/test_stream.py
L3 | test_the_stream_needs_a_session | tests/test_stream.py
L4 | test_the_stream_sends_what_the_rest_route_sends | tests/test_stream.py
L5 | test_an_unreachable_server_arrives_as_an_error_not_a_gap | tests/test_stream.py
L6 | test_one_sample_serves_every_tab | tests/test_stream.py
L7 | test_the_sampler_stops_when_the_last_tab_closes | tests/test_stream.py

## Interface (browser-verified; run locally, not in CI — see tests/test_ui.py)

U1 | test_the_layout_follows_the_container_not_a_javascript_measurement | tests/test_ui.py
U2 | test_nothing_ever_scrolls_sideways | tests/test_ui.py
U3 | test_the_desktop_only_controls_are_actually_hidden_on_a_phone | tests/test_ui.py
U4 | test_reduced_motion_is_honoured | tests/test_ui.py
U5 | test_both_colour_schemes_are_real | tests/test_ui.py
U6 | test_type_scales_with_the_viewport | tests/test_ui.py
U7 | test_the_frontend_decides_layout_in_css_not_javascript | tests/test_architecture.py

## Structure

S1 | test_core_never_imports_the_web_or_the_bot | tests/test_architecture.py
S2 | test_the_rules_have_exactly_one_definition | tests/test_architecture.py
S3 | test_only_the_composition_root_opens_the_database | tests/test_architecture.py
S4 | test_the_api_does_not_describe_itself_to_the_internet | tests/test_architecture.py
