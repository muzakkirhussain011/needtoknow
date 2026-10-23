// Ory Keto namespaces for needtoknow's tests and the KetoAuthorizer guide: a document's `view`
// relation holds users and the members of groups.
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
