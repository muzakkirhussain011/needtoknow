package haris_test

import rego.v1

import data.haris

hr_user := {
	"user_id": "aisha",
	"team": "people-ops",
	"department": "hr",
	"clearance": "confidential",
	"roles": ["employee", "pii_reader"],
	"disabled": false,
}

engineer := {
	"user_id": "omar",
	"team": "platform",
	"department": "engineering",
	"clearance": "internal",
	"roles": ["employee"],
	"disabled": false,
}

finance_lead := {
	"user_id": "fatima",
	"team": "treasury",
	"department": "finance",
	"clearance": "restricted",
	"roles": ["employee"],
	"disabled": false,
}

ask(p, req) := {"action": "ask", "principal": p, "request": req, "usage": {"team_tokens_today": 0}}

test_hr_reads_hr_and_shared_collections if {
	d := haris.decision with input as ask(hr_user, {"model": "gemma4:e2b", "collections": []})
	d.allow
	d.collections == ["company-handbook", "hr-policies", "vendor-faq"]
	d.reveal_pii
}

test_engineer_never_sees_hr_or_finance if {
	d := haris.decision with input as ask(engineer, {"model": "gemma4:e2b", "collections": []})
	d.allow
	not "hr-policies" in d.collections
	not "finance-procedures" in d.collections
	"it-runbooks" in d.collections
	not d.reveal_pii
}

test_explicit_request_for_forbidden_collection_is_denied if {
	d := haris.decision with input as ask(engineer, {"model": "gemma4:e2b", "collections": ["hr-policies"]})
	not d.allow
	"not authorised for collection hr-policies" in d.reasons
}

test_clearance_caps_sensitivity_even_inside_own_department if {
	low := object.union(finance_lead, {"clearance": "confidential"})
	d := haris.decision with input as ask(low, {"model": "gemma4:e2b", "collections": []})
	not "finance-procedures" in d.collections
}

test_finance_lead_reads_restricted_finance if {
	d := haris.decision with input as ask(finance_lead, {"model": "gemma4:e2b", "collections": ["finance-procedures"]})
	d.allow
	d.collections == ["finance-procedures"]
	d.max_sensitivity == 3
}

test_unapproved_model_is_denied if {
	d := haris.decision with input as ask(finance_lead, {"model": "gemma3:1b", "collections": []})
	not d.allow
	"model gemma3:1b is not approved for team treasury" in d.reasons
}

test_missing_model_uses_default if {
	d := haris.decision with input as ask(engineer, {"collections": []})
	d.allow
	d.model == "gemma4:e2b"
}

test_budget_exhausted_is_denied if {
	inp := object.union(ask(engineer, {"model": "gemma4:e2b"}), {"usage": {"team_tokens_today": 300000}})
	d := haris.decision with input as inp
	not d.allow
	"daily token budget exhausted for team platform" in d.reasons
}

test_high_injection_score_is_denied if {
	inp := object.union(ask(engineer, {"model": "gemma4:e2b"}), {"signals": {"injection_score": 0.9}})
	d := haris.decision with input as inp
	not d.allow
	"prompt injection suspected in the question" in d.reasons
}

test_disabled_account_is_denied if {
	p := object.union(engineer, {"disabled": true})
	d := haris.decision with input as ask(p, {"model": "gemma4:e2b"})
	not d.allow
	"account disabled" in d.reasons
}

test_unknown_team_is_denied if {
	p := object.union(engineer, {"team": "ghost-team"})
	d := haris.decision with input as ask(p, {"model": "gemma4:e2b"})
	not d.allow
	"unknown team" in d.reasons
}

test_ingest_requires_admin if {
	d := haris.decision with input as {"action": "ingest", "principal": engineer, "request": {}}
	not d.allow
}

test_audit_allowed_for_auditor if {
	auditor := object.union(engineer, {"roles": ["auditor"], "team": "security"})
	d := haris.decision with input as {"action": "audit", "principal": auditor, "request": {}}
	d.allow
}

test_unknown_action_is_denied if {
	d := haris.decision with input as {"action": "delete_everything", "principal": engineer, "request": {}}
	not d.allow
}
