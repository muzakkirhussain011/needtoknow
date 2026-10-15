# Haris authorisation policy.
#
# Input (built by haris/gateway.py):
#   input.action        "ask" | "describe" | "ingest" | "audit"
#   input.principal     {user_id, team, department, clearance, roles, disabled}
#   input.request       {model, collections}          # collections: [] means "everything I may read"
#   input.usage         {team_tokens_today}
#   input.signals       {injection_score, injection_signals, pii_kinds}
#
# Data (policies/data.json, loaded at data.haris_config):
#   collections: name -> {departments: [..] | ["*"], sensitivity}
#   teams:       name -> {models: [..], daily_token_budget}
#   default_model, injection_block_threshold
#
# Everything is denied unless a rule below allows it.

package haris

import rego.v1

levels := {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}

cfg := data.haris_config

principal := input.principal

team_cfg := cfg.teams[principal.team]

requested_collections := object.get(input.request, "collections", [])

requested_model := m if {
	m := input.request.model
	m != null
} else := cfg.default_model

clearance_level := levels[principal.clearance]

# Collections this principal may read: their department (or "*") owns it and the
# collection's sensitivity is at or below the principal's clearance.
readable contains name if {
	some name, col in cfg.collections
	department_allowed(col)
	levels[col.sensitivity] <= clearance_level
}

department_allowed(col) if "*" in col.departments

department_allowed(col) if principal.department in col.departments

# Least privilege: asking for nothing means "what I may read"; asking for specific collections
# narrows it. Asking for a collection you may not read is a denial, not a silent drop.
effective_collections := readable if count(requested_collections) == 0

effective_collections := {c | some c in requested_collections; c in readable} if count(requested_collections) > 0

forbidden_collections := {c | some c in requested_collections; not c in readable}

tokens_today := object.get(input, ["usage", "team_tokens_today"], 0)

budget_remaining := team_cfg.daily_token_budget - tokens_today

injection_score := object.get(input, ["signals", "injection_score"], 0)

default reveal_pii := false

reveal_pii if "pii_reader" in principal.roles

is_admin if "admin" in principal.roles

is_auditor if "auditor" in principal.roles

# ---------------------------------------------------------------------------------------------
# Deny reasons (each one is reported back and written to the audit log).

deny contains "account disabled" if principal.disabled

deny contains "unknown team" if not cfg.teams[principal.team]

deny contains sprintf("model %v is not approved for team %v", [requested_model, principal.team]) if {
	input.action == "ask"
	cfg.teams[principal.team]
	not requested_model in team_cfg.models
}

deny contains sprintf("daily token budget exhausted for team %v", [principal.team]) if {
	input.action == "ask"
	cfg.teams[principal.team]
	budget_remaining <= 0
}

deny contains sprintf("not authorised for collection %v", [c]) if {
	input.action == "ask"
	some c in forbidden_collections
}

deny contains "no readable collections" if {
	input.action == "ask"
	count(forbidden_collections) == 0
	count(effective_collections) == 0
}

deny contains "prompt injection suspected in the question" if {
	input.action == "ask"
	injection_score >= cfg.injection_block_threshold
}

deny contains "ingest requires the admin role" if {
	input.action == "ingest"
	not is_admin
}

deny contains "audit access requires the auditor or admin role" if {
	input.action == "audit"
	not is_auditor
	not is_admin
}

deny contains sprintf("unknown action %v", [input.action]) if {
	not input.action in {"ask", "describe", "ingest", "audit"}
}

# ---------------------------------------------------------------------------------------------
# The decision object returned to the gateway.

default decision := {"allow": false, "reasons": ["default deny"]}

decision := {
	"allow": count(deny) == 0,
	"reasons": sort([r | some r in deny]),
	"collections": sort([c | some c in effective_collections]),
	"max_sensitivity": clearance_level,
	"model": requested_model,
	"reveal_pii": reveal_pii,
	"budget_remaining": budget_remaining,
} if {
	cfg.teams[principal.team]
}

decision := {"allow": false, "reasons": sort([r | some r in deny])} if {
	not cfg.teams[principal.team]
}
