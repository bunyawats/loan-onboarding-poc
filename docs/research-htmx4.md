# Research note: htmx v4

Like the other `research-*.md` notes, not speculative — this project
pins **htmx v4.0.0-beta6** already
(`loan_onboarding/bff_backoffice/templates/base.html` and
`bff_customer/templates/base.html`, both via
`https://unpkg.com/htmx.org@4.0.0-beta6`). **The global `htmx4` skill
is the authoritative source for anything load-bearing** — it verifies
claims against the real downloaded bundle rather than trusting
`four.htmx.org`'s docs prose alone, which the skill explicitly flags as
unreliable for at least one load-bearing behavior (inline `<script>`
execution) and notes the homepage mixes real info with joke/gag
content. This note is the concept-level companion, condensed from that
skill plus a docs-site pass — anything below not attributed to
"bundle-verified" should be treated as docs prose, not independently
checked here.

## What htmx is

A small (~11kb), dependency-free JavaScript library that adds HTML
attributes for issuing HTTP requests from any element (not just forms
and links) and swapping the HTML response into the DOM — the
"hypermedia-driven" alternative to a client-side JS framework: the
server keeps owning rendering, the browser just gets more places to
trigger a request and more ways to place the response.

## Core attributes (unchanged in spirit since v1)

- **`hx-get`/`hx-post`/`hx-put`/`hx-patch`/`hx-delete`** — any element
  can issue any HTTP method.
- **`hx-trigger`** — what fires the request (`click`, `change`,
  `submit` are the per-element defaults; modifiers like `delay`,
  `throttle`, `once`, `every Ns` for polling).
- **`hx-target`** — where the response HTML lands (a CSS selector, or
  `closest`/`find`/`next`/`previous` relative to the triggering
  element).
- **`hx-swap`** — how it lands (`innerHTML`, `outerHTML`,
  `beforeend`/`afterend`, etc.).
- **`hx-vals`** — extra values sent alongside the request, including a
  `js:` prefix for a runtime-computed value.
- **Out-of-band swaps** (`hx-swap-oob="true"`) — update a second,
  unrelated part of the page (matched by id) in the same response that
  updates the primary target.

## v4: status and why it's different in kind, not just in degree

htmx v4 is a **`fetch()`-based rewrite** (v1–v2 used `XMLHttpRequest`),
still pre-release as of this writing — re-check `npm view htmx.org
dist-tags` before trusting any specific beta number, including this
project's own pin, as current.

**Bundle-verified breaking changes** (per the `htmx4` skill, checked
against the real minified bundle, not just docs):

- Every non-2xx response (4xx/5xx) is **swapped into the DOM by
  default now** — v2 needed `htmx.config.noSwap` to opt into this.
  An app that relied on error responses being silently dropped needs
  an explicit guard now.
- **Attribute inheritance is explicit, not implicit** — a child no
  longer picks up an ancestor's `hx-target`/`hx-swap`/etc. unless
  opted in with an `:inherited` modifier.
- **Event names are colon-namespaced and the payload shape changed** —
  `htmx:afterSwap` → `htmx:after:swap`, and `event.detail` is now
  `{ ctx }` rather than a flat object (`event.detail.ctx.response.status`,
  not `event.detail.successful`). Hand-written JS using v2-era event
  names silently never fires — no error, just a dead listener.
- **History navigation re-requests from the server** instead of
  restoring a client-side DOM snapshot — routes reachable via
  back/forward must be safe to replay.
- `htmx.config.allowScriptTags` is gone as a *setting*, but the
  behavior is now **unconditional** — inline `<script>` tags in
  swapped-in fragments still execute, with no way to disable it.
- Default request timeout is now 60000ms (was unlimited).

Two more mentioned in docs prose, not confirmed here against the
bundle: a `<hx-partial>` tag for multi-target responses (positioned as
an alternative to OOB swaps) and `innerMorph`/`outerMorph` swap
strategies. Verify against the pinned version before relying on either.

## How this project uses it

- **Both BFFs' `base.html` pin the identical beta** — one version
  across the whole app, no per-page override.
- **`hx-vals` is the bulk-selection mechanism** in
  `bff_backoffice/`'s staff queues (`_bulk_toolbar.html`,
  `_staff_row.html`, `_staff_list.html`, `_staff_rows_body.html`) —
  see the `list-pagination-bulk-actions` skill for the specific
  array-to-CSV coercion gotcha `hx-vals`' `js:` prefix has when the
  computed value is a JS array, which this exact pattern is exposed to.
- **Not currently exposed to two of the biggest v4 gotchas above**: a
  repo-wide check found no server-side `HX-Request` header branching
  and no hand-written `htmx:`-prefixed JS event listeners anywhere in
  either BFF — so the colon-namespaced-event-rename and
  `event.detail.ctx` reshaping (the skill's own "bitten by this once"
  war story) aren't live risks in this codebase today. Worth
  re-checking this note if either pattern gets added later.
- **No custom error-fragment handling built yet** for the "4xx/5xx now
  swaps by default" change — every route here returns 2xx on the
  HTMX-visible path today (blocking errors render as a normal
  in-fragment message, e.g. `bff_backoffice/routes.py`'s
  `ctx["error"]` pattern, rather than a non-2xx status code), so this
  behavior change hasn't had a chance to bite yet either.

## Links

- v4 docs/marketing (treat homepage copy with suspicion per the
  `htmx4` skill): https://four.htmx.org/
- v4 docs: https://four.htmx.org/docs/
- Package/dist-tags: `npm view htmx.org dist-tags`
