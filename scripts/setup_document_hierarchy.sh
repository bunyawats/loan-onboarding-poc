#!/usr/bin/env bash
#
# Builds this project's document indexing setup in Mayan EDMS via the REST
# API: Metadata Types -> Document Types -> three Index Templates (each with
# its own nested node tree).
#
# **Corrected from an earlier draft of this script**, which built one
# single "Loan Onboarding Archive" index rooted at `applicant_identifier`.
# Replaced (not merely renamed) with three separate indexes, each rooted at
# a different one of the three entity ids that a document can carry --
# giving staff three different entry points into the same document set,
# rather than one hierarchy trying to serve every navigation need at once:
#
#   Customer Index (customer_id)
#   └── <customer_id>
#          ├── <account_id>                 (branch 1: docs that also carry
#          │      └── <application_id>       account_id, i.e. every document
#          │             └── <category>      under an approved application,
#          │                                  plus Account Documents)
#          └── <application_id>              (branch 2: docs that carry
#                 └── <category>              application_id, whether or not
#                                              they've also gained account_id
#                                              yet -- a pre-approval upload
#                                              from a returning customer)
#
#   Account Index (account_id)
#   └── <account_id>
#          ├── <customer_id>
#          │      └── <category>
#          └── <application_id>
#                 └── <category>
#
#   Application Index (application_id)
#   └── <application_id>
#          ├── <customer_id>
#          │      └── <category>
#          └── <account_id>
#                 └── <category>
#
# **A document with more than one of these ids set shows up in more than
# one place -- deliberately, not a bug.** Confirmed with the user rather
# than assumed: Mayan evaluates every branch independently per document
# (the same "multi-leaf placement" behavior this project's original single
# index already relied on for its `id_photo` node -- see CLAUDE.md's
# "Document hierarchy"), so e.g. an approved application's Government ID
# document (application_id + account_id + customer_id, once approved)
# naturally appears under Customer Index's "account -> application" branch
# *and* its sibling "application" branch at once, not just one or the
# other. `applicant_identifier` plays no role in any of these three
# templates -- it's still attached to every document (see the metadata
# associations below) and still what `document.service.py`'s own queries
# filter on, just no longer an index-tree grouping key.
#
# All five gotchas from mayan-edms-customer-archive's
# docs/document-hierarchy-setup.md still apply -- read that file before
# touching this script. In particular, every LEAF condition below repeats
# its full ancestor requirement set (Gotcha #1: an empty parent expression
# does not stop Mayan from evaluating a descendant node against every
# document) -- the middle (non-leaf) grouping nodes deliberately do NOT
# repeat this guard, matching this project's own established convention: a
# middle node may spuriously group some wrong-document-type documents under
# an empty-string value (harmless UI clutter, nothing links to it), but the
# leaf is what actually gates which documents get shown, so only the leaf
# needs the full guard.
#
# Usage:
#   MAYAN_BASE_URL=http://localhost:8000 \
#   MAYAN_SERVICE_ACCOUNT_USERNAME=admin \
#   MAYAN_SERVICE_ACCOUNT_PASSWORD=... \
#   ./scripts/setup_document_hierarchy.sh
#
# Not idempotent (same as the reference project's script) -- re-running
# creates duplicate metadata types / document types / index templates. To
# re-run, delete the "Customer Index" / "Account Index" / "Application
# Index" index templates and the two document types first (System ->
# Setup, in Mayan's web UI), or reset the database.
#
# Requires: curl, python3

set -euo pipefail

MAYAN_BASE_URL="${MAYAN_BASE_URL:-http://localhost:8000}"
MAYAN_SERVICE_ACCOUNT_USERNAME="${MAYAN_SERVICE_ACCOUNT_USERNAME:-admin}"
MAYAN_SERVICE_ACCOUNT_PASSWORD="${MAYAN_SERVICE_ACCOUNT_PASSWORD:?Set MAYAN_SERVICE_ACCOUNT_PASSWORD}"
BASE="$MAYAN_BASE_URL/api/v4"

log() { echo "==> $*" >&2; }

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
log "Obtaining auth token for $MAYAN_SERVICE_ACCOUNT_USERNAME"
TOKEN=$(curl -sf -X POST "$BASE/auth/token/obtain/" \
  -H "Accept: application/json" -H "Content-Type: application/json" \
  -d "{\"username\":\"$MAYAN_SERVICE_ACCOUNT_USERNAME\",\"password\":\"$MAYAN_SERVICE_ACCOUNT_PASSWORD\"}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")

api() {
  # api METHOD PATH [JSON_BODY]
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -sf -X "$method" "$BASE$path" \
      -H "Authorization: Token $TOKEN" -H "Accept: application/json" -H "Content-Type: application/json" \
      -d "$body"
  else
    curl -sf -X "$method" "$BASE$path" \
      -H "Authorization: Token $TOKEN" -H "Accept: application/json"
  fi
}

json_get() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }

# ---------------------------------------------------------------------------
# 1. Metadata types
# ---------------------------------------------------------------------------
log "Creating metadata types"
declare -A METADATA_TYPE_ID
for entry in \
  "applicant_identifier:Applicant Identifier" \
  "application_id:Application ID" \
  "account_id:Account ID" \
  "customer_id:Customer ID" \
  "category:Category"
do
  name="${entry%%:*}"
  label="${entry#*:}"
  id=$(api POST "/metadata_types/" "{\"name\":\"$name\",\"label\":\"$label\"}" | json_get "['id']")
  METADATA_TYPE_ID["$name"]="$id"
  log "  $name -> id=$id"
done

# ---------------------------------------------------------------------------
# 2. Document types + required metadata associations
# ---------------------------------------------------------------------------
# Two document types, deliberately not three -- an "Application Document"
# carries applicant_identifier/application_id/category always, and
# optionally customer_id (attached later, only on the one Government ID
# document promoted to id_photo on approval -- not required at upload time).
# An "Account Document" carries applicant_identifier/account_id/category;
# it never has application_id at all, which is what lets the leaf
# conditions below stay simple (CLAUDE.md's document hierarchy note).
log "Creating document types"
declare -A DOCUMENT_TYPE_ID
for label in "Application Document" "Account Document"; do
  id=$(api POST "/document_types/" "{\"label\":\"$label\"}" | json_get "['id']")
  DOCUMENT_TYPE_ID["$label"]="$id"
  log "  $label -> id=$id"
done

attach_metadata() {
  local doc_type_id="$1" metadata_name="$2" required="$3"
  local metadata_type_id="${METADATA_TYPE_ID[$metadata_name]}"
  api POST "/document_types/$doc_type_id/metadata_types/" \
    "{\"metadata_type_id\":$metadata_type_id,\"required\":$required}" > /dev/null
}

log "Attaching metadata to document types"
for m in applicant_identifier application_id category; do
  attach_metadata "${DOCUMENT_TYPE_ID['Application Document']}" "$m" "true"
done
# customer_id is attached later, per-document, only on promotion -- not
# required at upload time (most Application Documents never get it).
attach_metadata "${DOCUMENT_TYPE_ID['Application Document']}" "customer_id" "false"
# account_id likewise -- attached to every document under an application
# only on approval (document.service.tag_application_documents, CLAUDE.md's
# "Document metadata assignment lifecycle"), never at upload time. Found
# live in P16-4: without this association, Mayan rejects the attach with
# a 400 (a document type can only carry metadata types it's been
# associated with) -- an Application Document's account_id association
# was missing entirely until this fix, since account_id previously only
# ever lived on Account Document type documents (Welcome Letter/Consent).
attach_metadata "${DOCUMENT_TYPE_ID['Application Document']}" "account_id" "false"

for m in applicant_identifier account_id category; do
  attach_metadata "${DOCUMENT_TYPE_ID['Account Document']}" "$m" "true"
done
# customer_id too, same reasoning as the Application Document association
# above -- generate_welcome_letter (document.service.py) attaches it to
# every Welcome Letter/Consent document as of P16-1, and Mayan rejects
# an attach for a metadata type the document type was never associated
# with. Found live in the same P16-4 pass as the Application Document
# gap.
attach_metadata "${DOCUMENT_TYPE_ID['Account Document']}" "customer_id" "false"

# ---------------------------------------------------------------------------
# 3. Three index templates: Customer Index, Account Index, Application
#    Index -- see the module docstring above for the full tree shapes and
#    why three separate indexes replaced the original single one.
# ---------------------------------------------------------------------------
post_node() {
  # post_node INDEX_ID PARENT_ID EXPRESSION LINK_DOCUMENTS(true|false)
  local index_id="$1" parent="$2" expr="$3" link_docs="$4"
  local payload
  payload=$(python3 -c "
import json, sys
print(json.dumps({
    'parent': int(sys.argv[1]),
    'expression': sys.argv[2],
    'link_documents': sys.argv[3] == 'true',
    'enabled': True,
}))
" "$parent" "$expr" "$link_docs")
  api POST "/index_templates/$index_id/nodes/" "$payload" | json_get "['id']"
}

create_index() {
  # create_index LABEL SLUG -- creates the index template, attaches both
  # document types, and prints "INDEX_ID ROOT_NODE_ID" for the caller to
  # capture via `read`.
  local label="$1" slug="$2"
  local response index_id root_id
  response=$(api POST "/index_templates/" "{\"label\":\"$label\",\"slug\":\"$slug\",\"enabled\":true}")
  index_id=$(echo "$response" | json_get "['id']")
  root_id=$(echo "$response" | json_get "['index_template_root_node_id']")
  log "  index '$label' -> id=$index_id root_node_id=$root_id"
  for doc_type_label in "Application Document" "Account Document"; do
    api POST "/index_templates/$index_id/document_types/add/" \
      "{\"document_type\":${DOCUMENT_TYPE_ID[$doc_type_label]}}" > /dev/null
  done
  echo "$index_id $root_id"
}

log "Creating 'Customer Index'"
read -r INDEX_ID ROOT_NODE_ID <<< "$(create_index "Customer Index" "customer-index")"

NODE_CUSTOMER=$(post_node "$INDEX_ID" "$ROOT_NODE_ID" \
  '{{ document.metadata_value_of.customer_id }}' \
  "false")
log "  customer_id node -> id=$NODE_CUSTOMER"

# Branch 1: account -> application -> category.
NODE_C_ACCOUNT=$(post_node "$INDEX_ID" "$NODE_CUSTOMER" \
  '{{ document.metadata_value_of.account_id }}' \
  "false")
NODE_C_ACCOUNT_APPLICATION=$(post_node "$INDEX_ID" "$NODE_C_ACCOUNT" \
  '{{ document.metadata_value_of.application_id }}' \
  "false")
post_node "$INDEX_ID" "$NODE_C_ACCOUNT_APPLICATION" \
  '{% if document.metadata_value_of.customer_id and document.metadata_value_of.account_id and document.metadata_value_of.application_id %}{{ document.metadata_value_of.category }}{% endif %}' \
  "true" > /dev/null
log "  branch 1 (account -> application -> category) built"

# Branch 2 (sibling of branch 1, under customer): application -> category.
# Deliberately not restricted to "account_id is empty" -- a document that
# has gained account_id (post-approval) still also appears here, per the
# multi-placement decision in the module docstring above.
NODE_C_APPLICATION=$(post_node "$INDEX_ID" "$NODE_CUSTOMER" \
  '{{ document.metadata_value_of.application_id }}' \
  "false")
post_node "$INDEX_ID" "$NODE_C_APPLICATION" \
  '{% if document.metadata_value_of.customer_id and document.metadata_value_of.application_id %}{{ document.metadata_value_of.category }}{% endif %}' \
  "true" > /dev/null
log "  branch 2 (application -> category) built"

api POST "/index_templates/$INDEX_ID/rebuild/" > /dev/null
log "Done. 'Customer Index' id=$INDEX_ID, slug=customer-index"

log "Creating 'Account Index'"
read -r INDEX_ID ROOT_NODE_ID <<< "$(create_index "Account Index" "account-index")"

NODE_ACCOUNT=$(post_node "$INDEX_ID" "$ROOT_NODE_ID" \
  '{{ document.metadata_value_of.account_id }}' \
  "false")
log "  account_id node -> id=$NODE_ACCOUNT"

# Branch 1: customer -> category.
NODE_A_CUSTOMER=$(post_node "$INDEX_ID" "$NODE_ACCOUNT" \
  '{{ document.metadata_value_of.customer_id }}' \
  "false")
post_node "$INDEX_ID" "$NODE_A_CUSTOMER" \
  '{% if document.metadata_value_of.account_id and document.metadata_value_of.customer_id %}{{ document.metadata_value_of.category }}{% endif %}' \
  "true" > /dev/null
log "  branch 1 (customer -> category) built"

# Branch 2 (sibling): application -> category.
NODE_A_APPLICATION=$(post_node "$INDEX_ID" "$NODE_ACCOUNT" \
  '{{ document.metadata_value_of.application_id }}' \
  "false")
post_node "$INDEX_ID" "$NODE_A_APPLICATION" \
  '{% if document.metadata_value_of.account_id and document.metadata_value_of.application_id %}{{ document.metadata_value_of.category }}{% endif %}' \
  "true" > /dev/null
log "  branch 2 (application -> category) built"

api POST "/index_templates/$INDEX_ID/rebuild/" > /dev/null
log "Done. 'Account Index' id=$INDEX_ID, slug=account-index"

log "Creating 'Application Index'"
read -r INDEX_ID ROOT_NODE_ID <<< "$(create_index "Application Index" "application-index")"

NODE_APPLICATION=$(post_node "$INDEX_ID" "$ROOT_NODE_ID" \
  '{{ document.metadata_value_of.application_id }}' \
  "false")
log "  application_id node -> id=$NODE_APPLICATION"

# Branch 1: customer -> category.
NODE_AP_CUSTOMER=$(post_node "$INDEX_ID" "$NODE_APPLICATION" \
  '{{ document.metadata_value_of.customer_id }}' \
  "false")
post_node "$INDEX_ID" "$NODE_AP_CUSTOMER" \
  '{% if document.metadata_value_of.application_id and document.metadata_value_of.customer_id %}{{ document.metadata_value_of.category }}{% endif %}' \
  "true" > /dev/null
log "  branch 1 (customer -> category) built"

# Branch 2 (sibling): account -> category.
NODE_AP_ACCOUNT=$(post_node "$INDEX_ID" "$NODE_APPLICATION" \
  '{{ document.metadata_value_of.account_id }}' \
  "false")
post_node "$INDEX_ID" "$NODE_AP_ACCOUNT" \
  '{% if document.metadata_value_of.application_id and document.metadata_value_of.account_id %}{{ document.metadata_value_of.category }}{% endif %}' \
  "true" > /dev/null
log "  branch 2 (account -> category) built"

api POST "/index_templates/$INDEX_ID/rebuild/" > /dev/null
log "Done. 'Application Index' id=$INDEX_ID, slug=application-index"

# ---------------------------------------------------------------------------
# 4. Second index template: "Creation date" -- groups documents by year then
#    month using Mayan's own native `document.datetime_created` field (no
#    custom metadata field involved -- a separate custom `creation_date`
#    metadata field was tried first and deliberately removed in favor of
#    this, since Mayan's built-in timestamp already covers it). Found
#    broken when first hand-built directly against a live instance: it was
#    attached only to Mayan's default "Default" document type, which none
#    of this project's real documents use (they're always "Application
#    Document" or "Account Document") -- so every rebuild silently
#    produced zero results no matter how many times it was run. Attaching
#    the two real document types here from the start is what actually
#    fixed it.
# ---------------------------------------------------------------------------
log "Creating index template 'Creation date'"
CREATION_DATE_INDEX_RESPONSE=$(api POST "/index_templates/" '{"label":"Creation date","slug":"creation_date","enabled":true}')
INDEX_ID=$(echo "$CREATION_DATE_INDEX_RESPONSE" | json_get "['id']")
CREATION_DATE_ROOT_NODE_ID=$(echo "$CREATION_DATE_INDEX_RESPONSE" | json_get "['index_template_root_node_id']")
log "  index id=$INDEX_ID root_node_id=$CREATION_DATE_ROOT_NODE_ID"

log "Attaching document types to the Creation date index"
for label in "Application Document" "Account Document"; do
  api POST "/index_templates/$INDEX_ID/document_types/add/" \
    "{\"document_type\":${DOCUMENT_TYPE_ID[$label]}}" > /dev/null
done

NODE_YEAR=$(post_node "$INDEX_ID" "$CREATION_DATE_ROOT_NODE_ID" \
  '{{ document.datetime_created|date:"Y" }}' \
  "false")
log "  year node -> id=$NODE_YEAR"

NODE_MONTH=$(post_node "$INDEX_ID" "$NODE_YEAR" \
  '{{ document.datetime_created|date:"m" }}' \
  "true")
log "  month node (leaf) -> id=$NODE_MONTH"

log "Rebuilding Creation date index"
api POST "/index_templates/$INDEX_ID/rebuild/" > /dev/null

log "Done. Index template id=$INDEX_ID, slug=creation_date"
