# The company's reading rule for the knowledge assistant, in Rego, for wardowl's OPAAuthorizer.
#
# Who a document is shared with lives in OPA's data, pushed there by the document system, one entry
# per document id:
#
#   {"northwind/hr/salary-bands-1": {"tenant": "northwind", "users": ["carol"], "groups": ["hr"]}}
#
# A person may read a document of their own tenant that is shared with them by name or with one of
# their groups. The tenant and the groups come from the subject wardowl sends (from authentication);
# the chunk only says which document it belongs to, in its `doc_id` metadata.
#
# The scenario uploads this file with PUT /v1/policies/<id> and the sharing with PUT /v1/data/...,
# after giving the package and the data root a name unique to the run.

package acme_s02.documents

import rego.v1

sharing := data.acme_s02_data.sharing

# One decision per chunk, in the order wardowl sent them (OPAAuthorizer's decisions_path).
decisions := [allowed(r) | some r in input.resources]

default allowed(_) := false

allowed(r) if {
	doc := r.metadata.doc_id
	is_string(doc)
	readable(doc)
}

readable(doc) if {
	entry := sharing[doc]
	entry.tenant == input.subject.attributes.tenant
	shared_with_subject(entry)
}

shared_with_subject(entry) if input.subject.id in entry.users

shared_with_subject(entry) if {
	some group in entry.groups
	group in input.subject.groups
}

# The same rule as a filter for pre-filtering (OPAAuthorizer's filter_path): every document the
# subject may read, by id, so the vector store searches only those.
filter := {"in": {"field": "doc_id", "values": [doc | some doc, _ in sharing; readable(doc)]}}
