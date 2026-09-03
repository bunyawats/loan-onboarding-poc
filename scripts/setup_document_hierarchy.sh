#!/usr/bin/env bash
#
# Builds the "Loan Onboarding Archive" document hierarchy in Mayan EDMS via
# the REST API: Metadata Types -> Document Types -> Index Template (with a
# nested node tree).
#
# This is this project's own hierarchy, NOT a literal copy of
# mayan-edms-customer-archive's script -- see CLAUDE.md's "Document
# hierarchy" for why the shape differs (two levels on the application
# branch, not that project's three, because neither a customer nor an
# account is guaranteed to exist at upload time here):
#
#   Loan Onboarding Archive
#   └── <applicant_identifier>
#          ├── <application_id>              (created at submission, always)
#          │      └── <category>             (Government ID, Proof of Income, ...)
#          ├── id_photo                       (same document as an approved
#          │                                   application's Government ID,
#          │                                   re-tagged with customer_id --
#          │                                   present only post-approval)
#          └── <account_id>                   (present only post-approval)
#                 └── <category>              (Welcome Letter, Consent)
#
# All five gotchas from mayan-edms-customer-archive's
# docs/document-hierarchy-setup.md apply -- read that file before touching
# this script. In particular, every LEAF condition below repeats its full
# ancestor requirement set (Gotcha #1: an empty parent expression does not
# stop Mayan from evaluating a descendant node against every document).
#
# Usage:
#   MAYAN_BASE_URL=http://localhost:8000 \
#   MAYAN_SERVICE_ACCOUNT_USERNAME=admin \
#   MAYAN_SERVICE_ACCOUNT_PASSWORD=... \
#   ./scripts/setup_document_hierarchy.sh
#
# Not idempotent (same as the reference project's script) -- re-running
# creates duplicate metadata types / document types / index templates. To
# re-run, delete the "Loan Onboarding Archive" index template and the two
# document types first (System -> Setup, in Mayan's web UI), or reset the
# database.
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

for m in applicant_identifier account_id category; do
  attach_metadata "${DOCUMENT_TYPE_ID['Account Document']}" "$m" "true"
done

# ---------------------------------------------------------------------------
# 3. Index template
# ---------------------------------------------------------------------------
log "Creating index template 'Loan Onboarding Archive'"
INDEX_RESPONSE=$(api POST "/index_templates/" '{"label":"Loan Onboarding Archive","slug":"loan-onboarding-archive","enabled":true}')
INDEX_ID=$(echo "$INDEX_RESPONSE" | json_get "['id']")
ROOT_NODE_ID=$(echo "$INDEX_RESPONSE" | json_get "['index_template_root_node_id']")
log "  index id=$INDEX_ID root_node_id=$ROOT_NODE_ID"

log "Attaching document types to the index"
for label in "Application Document" "Account Document"; do
  api POST "/index_templates/$INDEX_ID/document_types/add/" \
    "{\"document_type\":${DOCUMENT_TYPE_ID[$label]}}" > /dev/null
done

post_node() {
  # post_node PARENT_ID EXPRESSION LINK_DOCUMENTS(true|false)
  local parent="$1" expr="$2" link_docs="$3"
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
  api POST "/index_templates/$INDEX_ID/nodes/" "$payload" | json_get "['id']"
}

log "Building node hierarchy"

# Level 1: group by applicant_identifier. The one identity value guaranteed
# to exist at upload time, regardless of whether the applicant is a
# recognized customer yet (CLAUDE.md's "Document hierarchy").
NODE_APPLICANT=$(post_node "$ROOT_NODE_ID" \
  '{{ document.metadata_value_of.applicant_identifier }}' \
  "false")
log "  applicant_identifier node -> id=$NODE_APPLICANT"

# Branch A: group by application_id (submission-gate documents).
NODE_APPLICATION=$(post_node "$NODE_APPLICANT" \
  '{{ document.metadata_value_of.application_id }}' \
  "false")
log "  application_id node -> id=$NODE_APPLICATION"

# Branch A leaf: category, under application_id. Condition repeats
# "application_id present" rather than trusting the parent's emptiness to
# gate it -- Gotcha #1.
NODE_APPLICATION_CATEGORY=$(post_node "$NODE_APPLICATION" \
  '{% if document.metadata_value_of.application_id %}{{ document.metadata_value_of.category }}{% endif %}' \
  "true")
log "  category node (application docs, leaf) -> id=$NODE_APPLICATION_CATEGORY"

# Branch B (leaf, direct child of applicant_identifier): id_photo. Fires
# only once customer_id metadata has been attached (on approval) -- the
# same Government ID document also satisfies Branch A's leaf at the same
# time (multi-membership; see CLAUDE.md's flagged, source-confirmed-but-
# not-yet-instance-verified note -- P5-2's own DoD verifies this for real).
NODE_ID_PHOTO=$(post_node "$NODE_APPLICANT" \
  '{% if document.metadata_value_of.customer_id %}id_photo{% endif %}' \
  "true")
log "  id_photo node (leaf) -> id=$NODE_ID_PHOTO"

# Branch C: group by account_id (present only post-approval).
NODE_ACCOUNT=$(post_node "$NODE_APPLICANT" \
  '{{ document.metadata_value_of.account_id }}' \
  "false")
log "  account_id node -> id=$NODE_ACCOUNT"

# Branch C leaf: category, under account_id. Condition repeats "account_id
# present" -- Gotcha #1, same reasoning as Branch A's leaf.
NODE_ACCOUNT_CATEGORY=$(post_node "$NODE_ACCOUNT" \
  '{% if document.metadata_value_of.account_id %}{{ document.metadata_value_of.category }}{% endif %}' \
  "true")
log "  category node (account docs, leaf) -> id=$NODE_ACCOUNT_CATEGORY"

log "Rebuilding index"
api POST "/index_templates/$INDEX_ID/rebuild/" > /dev/null

log "Done. Index template id=$INDEX_ID, slug=loan-onboarding-archive"

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

NODE_YEAR=$(post_node "$CREATION_DATE_ROOT_NODE_ID" \
  '{{ document.datetime_created|date:"Y" }}' \
  "false")
log "  year node -> id=$NODE_YEAR"

NODE_MONTH=$(post_node "$NODE_YEAR" \
  '{{ document.datetime_created|date:"m" }}' \
  "true")
log "  month node (leaf) -> id=$NODE_MONTH"

log "Rebuilding Creation date index"
api POST "/index_templates/$INDEX_ID/rebuild/" > /dev/null

log "Done. Index template id=$INDEX_ID, slug=creation_date"
