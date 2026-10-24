// The company's sharing as Ory Keto namespaces, for wardowl's KetoAuthorizer.
//
// A document's `view` relation holds the people it is shared with and the members of groups. Group
// ids name their tenant ("northwind/hr"), so a group of one tenant never grants another tenant's
// documents; wardowl's TenantAuthorizer keeps each search inside the reader's tenant as well.
import { Namespace, SubjectSet } from "@ory/keto-namespace-types"

class User implements Namespace {}

class Group implements Namespace {
  related: {
    members: User[]
  }
}

class Document implements Namespace {
  related: {
    view: (User | SubjectSet<Group, "members">)[]
  }
}
