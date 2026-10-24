### Added

- Acceptance scenarios (`acceptance/`): the applications a company builds with wardowl, run from
  the documentation against real servers, a real identity provider (Keycloak), real embedding and
  reranking models, and a local LLM, with every result checked against an oracle of who may read
  what that does not use wardowl. They cover 14 stores, seven permission systems, LangChain,
  LlamaIndex, Haystack and MCP, the HTTP service, day-2 operations with servers stopped mid-traffic,
  redaction and reranking, a team's own store and policy, and the example gateway: 3,912 checks,
  run weekly against the release on PyPI. A Kaggle notebook runs the assistant at scale on GPUs.
