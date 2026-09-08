# Research note: Keycloak

Like `research-temporal-io.md`/`research-mayan-edms.md`, not
speculative — Keycloak is already this project's entire back-office
identity layer (`bff_backoffice/keycloak_auth.py`/`keycloak_session.py`/
`session_store.py`, `keycloak/import/loanrealm-realm.json`), running as
its own `keycloak` container (`quay.io/keycloak/keycloak:26.0`,
`start-dev --import-realm`) plus a dedicated `backoffice-redis`. This
note is a grounding reference, condensed from `keycloak.org`/
`www.keycloak.org/docs`, closing with where each concept already shows
up in this codebase. See the `keycloak-admin` skill for the deep,
hands-on version (realm-import JSON shapes, Admin REST API auditing,
the conditional-authentication-flow recipe) — this note is the
concept-level companion to it.

## What it is

Keycloak is an open-source Identity and Access Management (IAM)
platform: "add authentication to applications and secure services with
minimum effort." Standard protocols only — OpenID Connect, OAuth 2.0,
SAML 2.0 — so an application delegates login, session, and (optionally)
fine-grained permission decisions to Keycloak instead of building any
of that itself.

## Core pieces

- **Realm** — the top-level isolation boundary: a realm owns its own
  users, credentials, roles, groups, and clients, completely separate
  from any other realm on the same Keycloak deployment. Everything else
  below lives inside exactly one realm.
- **Client** — an application registered to use the realm for
  authentication. *Confidential* clients (a real backend, can keep a
  secret) authenticate as themselves via `client_id`+`client_secret`;
  *public* clients (SPAs, native apps) can't, and use PKCE instead.
- **Realm roles vs. client roles** — a realm role applies across every
  application in the realm; a client role is namespaced to one specific
  client. Roles can be composite (one role bundling several others).
- **Groups** — collections of users with shared role mappings and
  attributes, inherited on membership.
- **Identity Providers** — Keycloak can broker to an external IdP
  (Google, another SAML/OIDC provider) instead of, or alongside,
  managing credentials itself; irrelevant here (this project's realm
  manages its own demo users directly).
- **Authorization Services** (the fine-grained layer, on top of plain
  roles): **Resources** (a protected thing — a page, an endpoint, an
  entity type or a specific instance), **Scopes** (an action on a
  resource — view, approve, reject), **Policies** (a reusable, generic
  condition — "has role Underwriter," composable into "policy of
  policies"), and **Permissions**, which tie a Resource/Scope to the
  Policies that govern it: "X can do Y on resource Z."

## Authorization Code flow

A confidential client's standard login: the browser is redirected to
Keycloak, the user authenticates there (not on the app), Keycloak
redirects back with a short-lived Authorization Code, and the app's
*backend* exchanges that code (plus its own `client_id`+`client_secret`)
for an **Access Token** (what's sent to prove identity/authorization),
a **Refresh Token** (used to get a new Access Token without the user
logging in again), and an **ID Token** (OpenID Connect's user-identity
claims). The user's browser never sees the client secret or, ideally,
the raw tokens — the tradeoff that motivates this project's own
server-side session design below.

## The UMA ticket exchange (fine-grained permission checks)

Checking "can this user actually do this one specific thing" — not
just "do they hold a role" — goes through Authorization Services'
UMA (User-Managed Access) flow: the resource server requests a
**permission ticket** naming the Resource/Scope in question, exchanges
it (plus the user's own Access Token) at Keycloak's token endpoint for
an **RPT** (Requesting Party Token) — Keycloak evaluates every Policy
attached to that Permission live, at request time, and the RPT's
contents *are* the authorization decision. There's no local caching of
"what can this user do" by design — every check is a live round trip.

## How this project uses it

- **One realm** (`keycloak/import/loanrealm-realm.json`, loaded via
  `start-dev --import-realm`): two realm roles (`Underwriter`,
  `Manager`), one confidential client (`loan-onboarding-backoffice`),
  demo users `underwriter1`/`underwriter2`/`manager1`/`manager2`
  (password `password`).
- **One Resource, `LoanApplication`, five Scopes** —
  `UnderwriterApprove`/`UnderwriterReject`/`UnderwriterRequestMoreInfo`/
  `ManagerApprove`/`ManagerReject` — deliberately five distinct scopes
  rather than one shared `Approve`, because (unlike the reference
  project this was adapted from) *two* roles here both approve, at
  different pipeline stages; a shared scope name would hand every
  Underwriter a permission that also satisfies the Manager-stage check.
  Bound via two Policies (`Underwriter Policy`/`Manager Policy`) and
  five scope-type Permissions.
- **Role gates screens, permission gates actions — no exceptions**:
  `keycloak_session.require_session_role(...)` decides whether a staff
  member can see the Underwriter or Manager queue at all;
  `keycloak_session.check_permission(...)`/`require_permission(...)`
  (a real UMA ticket exchange every time, no local cache) decides
  whether a specific decision on a specific application is actually
  allowed. A deliberate, audited correction in this codebase's history:
  an earlier version also pre-gated the Manager decision route by role,
  producing two different 403 reasons for the same denied action — now
  only the permission check gates it.
- **Real access+refresh tokens never reach the browser** — a signed
  token pair for this setup runs ~4.5KB, over the ~4KB ceiling real
  browsers enforce per cookie (measured directly building this). The
  browser cookie holds only an opaque server-side session id
  (`session_store.new_session_id()`); the real tokens live in Redis
  (`backoffice-redis`), looked up by that id on every request.
- **A real gotcha this project hit**: a Keycloak issuer mismatch
  between this app's internal server-to-server calls (talking to
  Keycloak's Docker-internal hostname) and the browser-facing/token
  `iss` claim (which needs Keycloak's externally-reachable hostname) —
  fixed with a dedicated `KEYCLOAK_PUBLIC_ISSUER` env var, separate from
  `KEYCLOAK_ISSUER`, plus pinning Keycloak's own `KC_HOSTNAME`. See
  `CLAUDE.md`'s Phase 12 note and the `keycloak-admin` skill.
- **Deliberately not built**: Keycloak protection for the Temporal Web
  UI (a different, `TemporalAdmin`-role concern the reference project
  handles, out of scope here), and any Keycloak involvement on the
  customer side — the customer BFF's identity model (email OTP, no
  password) is a completely separate, purpose-built mechanism with no
  Keycloak dependency at all.

## Links

- Product site: https://www.keycloak.org/
- Server administration guide (realms, clients, roles, groups):
  https://www.keycloak.org/docs/latest/server_admin/index.html
- Authorization Services (Resources/Scopes/Policies/Permissions, UMA):
  https://www.keycloak.org/docs/latest/authorization_services/index.html
- Securing apps / OIDC flows: https://www.keycloak.org/securing-apps/oidc-layers
- Token exchange: https://www.keycloak.org/securing-apps/token-exchange
