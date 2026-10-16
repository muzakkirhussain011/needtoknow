# Example policy for needtoknow's OPAAuthorizer.
#
# Chunks carry their permissions in metadata: `allowed_users` (user ids) and `allowed_groups`
# (group names, "*" for everyone). Replace `allowed` with your own rules; keep the shapes of
# `decisions` and `filter`.
#
#   opa run --server examples/opa
#   OPAAuthorizer("http://localhost:8181", filter_path="needtoknow/filter")

package needtoknow

import rego.v1

# One decision per resource, in the order the resources were sent.
decisions := [allowed(r) | some r in input.resources]

default allowed(_) := false

allowed(r) if input.subject.id in field_values(r, "allowed_users")

allowed(r) if {
	some group in field_values(r, "allowed_groups")
	group in {g | some g in input.subject.groups} | {"*"}
}

# A permission field may hold one value or a list of values; a missing field holds none.
field_values(r, name) := v if {
	v := object.get(r.metadata, name, [])
	is_array(v)
} else := [object.get(r.metadata, name, null)]

# The same rules as a filter, so the vector store can apply them inside the search.
filter := {"or": [
	{"in": {"field": "allowed_users", "values": [input.subject.id]}},
	{"in": {"field": "allowed_groups", "values": array.concat(input.subject.groups, ["*"])}},
]}
