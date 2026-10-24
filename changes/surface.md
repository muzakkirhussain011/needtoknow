### Security

- Example gateway (`examples/gateway`): a request body over `RAMPART_MAX_BODY_BYTES` (1 MiB by
  default) is refused with 413 before it is parsed or the caller is authenticated, by its
  `Content-Length` and by the bytes received. Neither the app nor uvicorn set a limit, so a 1 GB
  POST to `/v1/ask` was read and parsed as JSON before the API key was checked. A principal marked
  `disabled` is refused with 403 where its key is checked, before its question is masked and
  scored; only the policy refused it before, after that work. The phone pattern and the "send data
  out" injection rule, which run before the policy decides, now take linear time (a run of 4,000
  zeros took most of a second, and `send.to.` repeated over 4,000 characters about a second): a
  phone number starts only where a number starts, so digits glued to a preceding digit or `+` are
  no longer read as one, and the rule's address starts only where a run of address characters
  starts, with at most 64 characters before the `@` and 63 in the domain label. Compose pins
  `ollama/ollama` to 0.40.6 and its multi-arch index digest instead of `latest`.
