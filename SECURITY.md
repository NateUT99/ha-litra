# Security

## Reporting a vulnerability

Report security problems privately through GitHub: **Security → Report a vulnerability**
on this repository. Please don't open a public issue for them.

This is a hobby project with no response-time commitment (see *Project status* in the
README), but security reports get looked at before anything else.

## Scope

In scope:

- `litra-agent`: authentication and token handling, TLS and certificate pinning, the API's
  input handling, and anything that lets a network client do more than control the lights
- The `litra` Home Assistant integration: credential storage, certificate pinning, and
  the pairing and re-pairing flows

Out of scope: anything that requires an account on the Mac running the agent. That
user can already control the light directly; see *Security model* in the README.
