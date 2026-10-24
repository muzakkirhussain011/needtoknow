// The company's reading rule in Cedar, for wardowl's CedarAuthorizer.
//
// The resource is the chunk, with its metadata as attributes (tenant, allowed_users,
// allowed_groups); the principal is the subject, with its id, its groups and its tenant.

// Shared with the reader by name.
permit (principal, action == Action::"read", resource)
when { resource has allowed_users && resource.allowed_users.contains(principal.id) };

// Shared with one of the reader's groups.
permit (principal, action == Action::"read", resource)
when { resource has allowed_groups && resource.allowed_groups.containsAny(principal.groups) };

// Never across tenants, whatever the sharing says. A chunk or a reader without a tenant is refused.
forbid (principal, action, resource)
unless { principal has tenant && resource has tenant && principal.tenant == resource.tenant };
