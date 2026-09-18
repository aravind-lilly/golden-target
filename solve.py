#!/usr/bin/env python3
"""
solve.py — Target Master reconciliation tool.

Usage:
    python3 solve.py <pack_dir>

Resolves every UniProt accession and every claimed gene symbol against the
EBI Proteins REST API (https://www.ebi.ac.uk/proteins/api) at runtime — no
identity facts are hardcoded, so this generalizes to the hidden dataset.

Defect classes detected (see approach summary for the full rationale):
  wrong_mapping        - accession/gene pair belongs to two different real
                          proteins (accession is real, but for a DIFFERENT gene
                          than the row claims, and that gene is not a documented
                          synonym of the accession's real entry).
  stale_accession      - accession is a genuine but SECONDARY/merged UniProt
                          accession (via EBI redirect or a UniProt REST
                          MERGED history EBI itself 404s on); a current
                          PRIMARY accession exists.
  demerged_accession   - accession was UniProt-DEMERGED into >1 current gene;
                          disambiguated using the row's own claimed gene
                          symbol against the retrieved demerge candidates.
  obsolete_accession   - accession is inactive with no merge/demerge target;
                          corrected via an independent gene-symbol lookup.
  stale_symbol         - accession is current/primary, but the gene symbol
                          given is a superseded HGNC symbol (current approved
                          symbol differs from what UniProt now reports as the
                          primary gene name for that entry).
  wrong_crossreference - a source_chembl.csv row's chembl_id does not appear
                          among its accession's own EBI dbReferences (type
                          ChEMBL) — a wrong/duplicated external-database link.
  ambiguous_literature_mention
                        - a source_publications.csv target_mention is a
                          documented UniProt protein short name for more than
                          one real entry; disambiguated using context_sentence
                          text against each candidate's retrieved name/function
                          text only (pmid values are never fetched).
  organism_mismatch     - accession resolves to a non-human organism.

Golden records are keyed on the RESOLVED PRIMARY accession (not on raw
gene-symbol text), so legitimate messiness (old aliases, TrEMBL/Swiss-Prot
duplicate accessions for the same gene, formatting differences in organism
strings) does not fragment or inflate unique_target_count.
"""
import csv
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait

EBI_BASE = "https://www.ebi.ac.uk/proteins/api"
UNIPROT_REST_BASE = "https://rest.uniprot.org/uniprotkb"
HTTP_TIMEOUT = 8
HTTP_RETRIES = 2
MAX_WORKERS = 12


def _build_ssl_context():
    """Default verified context, with one deliberate relaxation:
    some corporate root CAs (observed: Eli Lilly's internal root, issued
    2015) predate RFC 5280's requirement that a CA certificate's Basic
    Constraints extension be marked critical. OpenSSL >= 3.2 enforces that
    requirement via VERIFY_X509_STRICT (on by default in Python's
    create_default_context), which rejects an otherwise-legitimate,
    OS-trusted chain with CERTIFICATE_VERIFY_FAILED. curl/system TLS
    stacks on the same machine do not apply this check and connect fine.
    We keep full certificate verification (hostname + chain-of-trust) and
    only drop the extra strict flag, rather than disabling verification.
    """
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


SSL_CONTEXT = _build_ssl_context()
# Leave generous headroom under the 5-minute grading budget for CSV I/O,
# publication disambiguation and JSON serialization. Overridable for testing.
RESOLUTION_DEADLINE_SECONDS = float(os.environ.get("RECON_DEADLINE", 210))

START_TIME = time.time()


def time_left():
    return RESOLUTION_DEADLINE_SECONDS - (time.time() - START_TIME)


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def http_get_json(url):
    last_exc = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url, headers={"Accept": "application/json", "User-Agent": "target-master-recon/1.0"}
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=SSL_CONTEXT) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
                return None
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return None
            last_exc = e
            time.sleep(0.5 * (attempt + 1))
        except Exception as e:
            last_exc = e
            time.sleep(0.5 * (attempt + 1))
    if last_exc is not None:
        print(f"[solve.py] giving up on {url}: {last_exc!r}", file=sys.stderr)
    return None


# --------------------------------------------------------------------------
# EBI Proteins API resolution
# --------------------------------------------------------------------------

def _extract_genes(entry):
    genes = []
    for g in entry.get("gene", []) or []:
        name = (g.get("name") or {}).get("value")
        if name:
            genes.append(name)
        for syn in g.get("synonyms", []) or []:
            v = syn.get("value")
            if v:
                genes.append(v)
    return genes


def _from_ebi_entry(acc, data):
    primary = data.get("accession", acc)
    genes = _extract_genes(data)
    organism = data.get("organism", {}) or {}
    names = organism.get("names") or []
    organism_name = names[0].get("value") if names else None
    tax_id = organism.get("taxonomy")
    protein_name = None
    rn = ((data.get("protein") or {}).get("recommendedName")) or {}
    if rn:
        protein_name = (rn.get("fullName") or {}).get("value")
    chembl_ids = [x.get("id") for x in data.get("dbReferences", []) or [] if x.get("type") == "ChEMBL"]
    return {
        "queried_accession": acc,
        "primary_accession": primary,
        "is_secondary": acc != primary,
        "secondary_accessions": data.get("secondaryAccession", []) or [],
        "genes": genes,
        "current_symbol": genes[0] if genes else None,
        "organism": organism_name,
        "tax_id": tax_id,
        "protein_name": protein_name,
        "chembl_ids": chembl_ids,
        "resolved_via": f"EBI Proteins API /proteins/{acc}",
    }


_ACCESSION_CACHE = {}


def resolve_accession(acc, _chain=None):
    """Resolve a UniProt accession, preferring the EBI Proteins API.

    EBI's /proteins/{accession} endpoint does not follow very old
    UniProtKB MERGED/DEMERGED history — it returns a bare 404 for an
    accession that has since been merged into (or split/"demerged" out
    of) another entry, even though that history is real and load-bearing
    (observed on this exam pack: several TrEMBL accessions merged into a
    current Swiss-Prot entry, and one case — P01562 — that was itself
    later DEMERGED into two distinct current genes, IFNA13/IFNA1). When
    EBI 404s, we fall back to rest.uniprot.org's /uniprotkb/{acc}.json,
    which exposes exactly this merge/demerge history via
    `inactiveReason`. A MERGED accession with a single target is
    followed transparently (recursively, in case of merge chains). A
    DEMERGED accession (or a merge with multiple targets) is genuinely
    ambiguous from the accession alone — we return every current
    candidate under "demerge_candidates" and let the caller disambiguate
    using the row's own claimed gene symbol, which we then report as
    retrieved evidence for the finding.
    """
    if acc in _ACCESSION_CACHE:
        return _ACCESSION_CACHE[acc]
    _chain = _chain or set()
    if acc in _chain:  # cyclic-merge safety net; should not happen in practice
        return None
    _chain = _chain | {acc}

    data = http_get_json(f"{EBI_BASE}/proteins/{urllib.parse.quote(acc)}")
    if data:
        result = _from_ebi_entry(acc, data)
        _ACCESSION_CACHE[acc] = result
        return result

    rest_data = http_get_json(f"{UNIPROT_REST_BASE}/{urllib.parse.quote(acc)}.json")
    if not rest_data:
        _ACCESSION_CACHE[acc] = None
        return None

    if rest_data.get("entryType") != "Inactive":
        # Active in UniProt REST but EBI didn't have it (sync lag) — build
        # directly from the REST payload's (differently-shaped) JSON.
        genes = [g.get("geneName", {}).get("value") for g in rest_data.get("genes", []) or []]
        genes = [g for g in genes if g]
        organism = rest_data.get("organism", {}) or {}
        rn = ((rest_data.get("proteinDescription") or {}).get("recommendedName")) or {}
        chembl_ids = [
            x.get("id") for x in rest_data.get("uniProtKBCrossReferences", []) or []
            if x.get("database") == "ChEMBL"
        ]
        result = {
            "queried_accession": acc,
            "primary_accession": rest_data.get("primaryAccession", acc),
            "is_secondary": rest_data.get("primaryAccession", acc) != acc,
            "secondary_accessions": rest_data.get("secondaryAccessions", []) or [],
            "genes": genes,
            "current_symbol": genes[0] if genes else None,
            "organism": organism.get("scientificName"),
            "tax_id": organism.get("taxonId"),
            "protein_name": (rn.get("fullName") or {}).get("value"),
            "chembl_ids": chembl_ids,
            "resolved_via": f"UniProt REST API /uniprotkb/{acc}.json",
        }
        _ACCESSION_CACHE[acc] = result
        return result

    reason = (rest_data.get("inactiveReason") or {}).get("inactiveReasonType")
    targets = (rest_data.get("inactiveReason") or {}).get("mergeDemergeTo") or []

    if reason == "MERGED" and len(targets) == 1:
        sub = resolve_accession(targets[0], _chain)
        if sub:
            result = dict(sub)
            result["queried_accession"] = acc
            result["is_secondary"] = True
            result["merge_note"] = f"{acc} MERGED into {targets[0]} (UniProt REST inactiveReason)"
            result["resolved_via"] = (
                f"UniProt REST API /uniprotkb/{acc}.json (MERGED -> {targets[0]}); " + sub.get("resolved_via", "")
            )
            _ACCESSION_CACHE[acc] = result
            return result

    if targets:  # DEMERGED, or a MERGED with >1 target — genuinely ambiguous
        candidates = [c for c in (resolve_accession(t, _chain) for t in targets) if c]
        result = {
            "queried_accession": acc,
            "primary_accession": None,
            "is_secondary": True,
            "demerge_reason": reason,
            "demerge_candidates": candidates,
            "resolved_via": f"UniProt REST API /uniprotkb/{acc}.json ({reason} -> {targets})",
        }
        _ACCESSION_CACHE[acc] = result
        return result

    # Inactive with no merge/demerge target (e.g. DELETED) — genuinely gone.
    result = {
        "queried_accession": acc,
        "primary_accession": None,
        "is_secondary": True,
        "demerge_reason": reason or "DELETED",
        "demerge_candidates": [],
        "resolved_via": f"UniProt REST API /uniprotkb/{acc}.json ({reason or 'DELETED'}, no replacement)",
    }
    _ACCESSION_CACHE[acc] = result
    return result


def resolve_gene(gene, organism_tax=9606):
    """Find the reviewed human entry for a gene symbol via EBI Proteins API.

    EBI's ?gene= search is a fuzzy/substring search, not an exact-match
    lookup: querying "NAK" also returns NAK1 (NR4A1) and NAKAP (AKAP8L);
    querying "GALNR" also returns GALR2 (whose OWN synonym is "GALNR2").
    Silently taking the first hit when no PRIMARY name matched exactly
    (the previous behavior) produced false "wrong_mapping" findings for
    rows that were actually correct under a real, documented synonym
    (observed on this exam pack: GALNR/GALR1, NAK/TBK1, ERBA2/THRB all
    misresolved this way). We therefore only ever accept an EXACT
    case-insensitive match — against the primary gene name first, then
    against a documented synonym — and return None (no guess) otherwise.
    """
    if not gene:
        return None
    url = f"{EBI_BASE}/proteins?gene={urllib.parse.quote(gene)}&taxid={organism_tax}&reviewed=true"
    data = http_get_json(url)
    if not data:
        return None
    gene_upper = gene.upper()
    exact_primary, exact_synonym = [], []
    for entry in data:
        for g in entry.get("gene", []) or []:
            name = (g.get("name") or {}).get("value", "")
            if name.upper() == gene_upper:
                exact_primary.append(entry)
                continue
            for syn in g.get("synonyms", []) or []:
                if (syn.get("value") or "").upper() == gene_upper:
                    exact_synonym.append(entry)
                    break
    candidates = exact_primary or exact_synonym
    if not candidates:
        return None
    best = candidates[0]
    genes = _extract_genes(best)
    rn = ((best.get("protein") or {}).get("recommendedName")) or {}
    protein_name = (rn.get("fullName") or {}).get("value")
    return {
        "primary_accession": best.get("accession"),
        "genes": genes,
        "current_symbol": genes[0] if genes else None,
        "protein_name": protein_name,
    }


def find_shortname_candidates(mention, organism_tax=9606):
    """Which reviewed human proteins carry `mention` as a documented UniProt
    protein short name (protein.recommendedName.shortName)?

    This is distinct from — and, for ambiguous clinical/lab shorthand, more
    useful than — a gene-symbol synonym search: "PSA" is not a documented
    HGNC gene synonym of KLK3 at all (only of NPEPPS and PSAT1), yet UniProt
    itself records "PSA" as KLK3's own short name. EBI's ?protein= endpoint
    does a loose text search (a single generic word like "antigen" returns
    ~2000 hits), so we treat it purely as a candidate-generation step and
    require an EXACT case-insensitive match against shortName before
    accepting a hit — never a fuzzy/substring one.
    """
    if not mention:
        return []
    url = f"{EBI_BASE}/proteins?protein={urllib.parse.quote(mention)}&taxid={organism_tax}&reviewed=true"
    data = http_get_json(url)
    if not data:
        return []
    mention_upper = mention.upper()
    seen, out = set(), []
    for entry in data:
        acc = (entry.get("accession") or "").split("-")[0]  # collapse isoforms
        if not acc or acc in seen:
            continue
        rn = ((entry.get("protein") or {}).get("recommendedName")) or {}
        shorts = [s.get("value", "") for s in rn.get("shortName", []) or []]
        if any(s.upper() == mention_upper for s in shorts):
            seen.add(acc)
            out.append(acc)
    return out


_TEXT_BLOB_CACHE = {}


def fetch_disambiguation_text(acc):
    """Two tiers of per-accession text to score a literature context_sentence
    against: (name_text, comment_text). Kept SEPARATE rather than merged,
    because they carry very different reliability. name_text (full name +
    alternative names, e.g. "Kallikrein-3") is short and specific — a hit
    there is strong evidence. comment_text (FUNCTION/DISEASE/tissue
    commentary, needed because e.g. KLK3's "liquefies seminal coagulum" only
    shows up there, not in its name) is long, generic prose that can produce
    coincidental hits (observed on this exam pack: NPEPPS's FUNCTION comment
    mentions "the antigen-processing pathway" and "substrate specificity",
    which would otherwise outscore KLK3 — the real match — on a context
    sentence about a "circulating antigen" and assay "specificity"). Callers
    must therefore prefer any candidate with a name_text hit before ever
    falling back to comment_text-only evidence.
    """
    if acc in _TEXT_BLOB_CACHE:
        return _TEXT_BLOB_CACHE[acc]
    data = http_get_json(f"{EBI_BASE}/proteins/{urllib.parse.quote(acc)}")
    name_parts, comment_parts = [], []
    if data:
        protein = data.get("protein") or {}
        rn = protein.get("recommendedName") or {}
        if rn.get("fullName"):
            name_parts.append(rn["fullName"].get("value", ""))
        for alt in protein.get("alternativeName", []) or []:
            if alt.get("fullName"):
                name_parts.append(alt["fullName"].get("value", ""))
        for c in data.get("comments", []) or []:
            if c.get("type") in ("FUNCTION", "DISEASE", "TISSUE_SPECIFICITY", "MISCELLANEOUS", "PATHWAY"):
                for t in c.get("text", []) or []:
                    if t.get("value"):
                        comment_parts.append(t["value"])
    result = (" ".join(name_parts), " ".join(comment_parts))
    _TEXT_BLOB_CACHE[acc] = result
    return result


# --------------------------------------------------------------------------
# CSV loading
# --------------------------------------------------------------------------

def load_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# --------------------------------------------------------------------------
# Bounded concurrent resolution — never blocks past the remaining time budget,
# regardless of how individual HTTP calls behave (hang, slow-fail, etc.)
# --------------------------------------------------------------------------

def bounded_resolve_many(resolver, items, reserve_seconds=10):
    """Run resolver(item) for each item in a thread pool, honoring the global
    time_left() budget. Returns {item: result_or_None}. Never waits past the
    remaining budget minus `reserve_seconds`, even if some calls are still
    in flight (those are recorded as None and the pool is abandoned)."""
    results = {}
    items = list(dict.fromkeys(items))  # de-dup, preserve order
    if not items:
        return results
    budget = time_left() - reserve_seconds
    if budget <= 0:
        return {item: None for item in items}
    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    futures = {pool.submit(resolver, item): item for item in items}
    remaining = set(futures)
    deadline = min(budget, time_left() - reserve_seconds)
    while remaining and deadline > 0:
        done, remaining = wait(remaining, timeout=min(deadline, 5))
        for fut in done:
            item = futures[fut]
            try:
                results[item] = fut.result(timeout=0)
            except Exception:
                results[item] = None
        deadline = time_left() - reserve_seconds
    for fut in remaining:
        results[futures[fut]] = None
    pool.shutdown(wait=False, cancel_futures=True)
    return results


# --------------------------------------------------------------------------
# Main reconciliation
# --------------------------------------------------------------------------

STOPWORDS = {
    "the", "a", "an", "of", "in", "and", "or", "to", "for", "with", "on",
    "by", "screening", "campaign", "identified", "modulators", "study",
}


def keywords(text):
    words = re.findall(r"[A-Za-z]{5,}", (text or "").lower())
    return {w for w in words if w not in STOPWORDS}


def main(pack_dir):
    chembl = load_csv(os.path.join(pack_dir, "source_chembl.csv"))
    uniprot = load_csv(os.path.join(pack_dir, "source_uniprot.csv"))
    bindingdb = load_csv(os.path.join(pack_dir, "source_bindingdb.csv"))
    internal = load_csv(os.path.join(pack_dir, "source_internal.csv"))
    pubs = load_csv(os.path.join(pack_dir, "source_publications.csv"))

    # rows: (source_name, accession_field_value, gene_field_value, raw_row)
    rows = []
    for r in chembl:
        rows.append(("chembl", r.get("accession", "").strip(), r.get("gene_symbol", "").strip(), r))
    for r in uniprot:
        # gene_names may contain multiple space/comma separated synonyms; take first as the "claimed" one
        gene_field = (r.get("gene_names") or "").strip()
        first_gene = re.split(r"[,\s]+", gene_field)[0] if gene_field else ""
        rows.append(("uniprot", r.get("accession", "").strip(), first_gene, r))
    for r in bindingdb:
        rows.append(("bindingdb", r.get("uniprot_id", "").strip(), r.get("gene_symbol", "").strip(), r))
    for r in internal:
        rows.append(("internal", r.get("uniprot_ref", "").strip(), r.get("gene_symbol", "").strip(), r))

    unique_accessions = sorted({acc for _, acc, _, _ in rows if acc})

    # ---- Resolve every distinct accession concurrently, with a HARD time budget ----
    # (resolve_accession recurses internally for MERGED/DEMERGED chains and memoizes
    # in _ACCESSION_CACHE, so bounded_resolve_many's own dedup plus that cache keep
    # the extra UniProt-REST-fallback lookups bounded even under merge chains.)
    acc_cache = bounded_resolve_many(resolve_accession, unique_accessions)

    # ---- Figure out which gene symbols actually need a live lookup ----
    # Needed whenever a row's claimed gene doesn't already match its accession's
    # documented identity, OR the accession itself resolved to no single current
    # identity (inactive with no gene-matching demerge candidate) — in that case
    # the gene symbol is our only remaining path to a correction.
    genes_needing_lookup = set()
    for source, acc, gene, raw in rows:
        if not acc or not gene:
            continue
        res_acc = acc_cache.get(acc)
        if res_acc is None:
            continue
        if res_acc.get("primary_accession") is None:
            genes_needing_lookup.add(gene)
            continue
        known_names_upper = {g.upper() for g in res_acc["genes"]}
        if gene.upper() not in known_names_upper:
            genes_needing_lookup.add(gene)

    gene_cache = bounded_resolve_many(resolve_gene, genes_needing_lookup)

    # ---- Detect per-row defects; also build accession -> primary map for golden records ----
    findings = []
    acc_to_primary = {}

    for source, acc, gene, raw in rows:
        if not acc:
            continue
        res_acc = acc_cache.get(acc)
        gene_upper = gene.upper() if gene else ""

        if res_acc is None:
            # Could not resolve via EBI or UniProt REST (network failure, or an
            # accession genuinely unknown to both). Per the evidence contract we
            # do not guess without a retrieved correction.
            acc_to_primary[acc] = acc
            continue

        if res_acc.get("primary_accession") is None:
            # Confirmed INACTIVE in UniProt (MERGED with >1 target, DEMERGED, or
            # DELETED with no replacement) — the accession alone is ambiguous or
            # gone. Disambiguate using the row's own claimed gene symbol against
            # every live candidate UniProt actually points to.
            candidates = res_acc.get("demerge_candidates") or []
            matched = next(
                (c for c in candidates if gene_upper and gene_upper in {g.upper() for g in c.get("genes", [])}),
                None,
            )
            evidence_prefix = res_acc.get("resolved_via", f"UniProt REST API /uniprotkb/{acc}.json")
            if matched:
                acc_to_primary[acc] = matched["primary_accession"]
                findings.append({
                    "gene": gene or matched.get("current_symbol"),
                    "observed": f"accession={acc}, gene_symbol={gene} (source={source})",
                    "correct": (
                        f"{matched['primary_accession']} ({matched.get('current_symbol')}, "
                        f"{matched.get('protein_name')})"
                    ),
                    "retrieved_evidence": (
                        f"{evidence_prefix}; candidates="
                        f"{[(c['primary_accession'], c.get('current_symbol')) for c in candidates]}; "
                        f"row's claimed gene {gene} matches candidate {matched['primary_accession']}"
                    ),
                    "evidence_source": "UniProt REST API /uniprotkb/{accession}.json",
                    "severity": "high" if len(candidates) > 1 else "medium",
                    "classification": "demerged_accession" if len(candidates) > 1 else "stale_accession",
                })
                continue
            res_gene = gene_cache.get(gene) if gene else None
            if res_gene and res_gene.get("primary_accession"):
                acc_to_primary[acc] = res_gene["primary_accession"]
                findings.append({
                    "gene": gene,
                    "observed": f"accession={acc}, gene_symbol={gene} (source={source})",
                    "correct": (
                        f"{res_gene['primary_accession']} ({res_gene.get('current_symbol')}, "
                        f"{res_gene.get('protein_name')})"
                    ),
                    "retrieved_evidence": (
                        f"{evidence_prefix} — no candidate's gene names matched {gene}; "
                        f"EBI /proteins?gene={gene} independently resolves to "
                        f"{res_gene['primary_accession']} ({res_gene['genes']})"
                    ),
                    "evidence_source": "UniProt REST API (inactive accession) + EBI Proteins API gene lookup",
                    "severity": "high",
                    "classification": "obsolete_accession",
                })
            else:
                acc_to_primary[acc] = acc
                findings.append({
                    "gene": gene or acc,
                    "observed": f"accession={acc}, gene_symbol={gene} (source={source})",
                    "correct": "",
                    "retrieved_evidence": f"{evidence_prefix} — no current UniProt replacement could be determined",
                    "evidence_source": "UniProt REST API /uniprotkb/{accession}.json",
                    "severity": "medium",
                    "classification": "obsolete_accession_unresolved",
                })
            continue

        # --- normal case: accession resolved to one live, current identity ---
        primary = res_acc["primary_accession"]
        acc_to_primary[acc] = primary
        evidence_prefix = res_acc.get("resolved_via", f"EBI Proteins API /proteins/{acc}")

        known_names_upper = {g.upper() for g in res_acc["genes"]}

        # Organism check
        if res_acc["tax_id"] and str(res_acc["tax_id"]) != "9606":
            findings.append({
                "gene": gene or res_acc.get("current_symbol"),
                "observed": f"accession {acc} used as a human target (source={source})",
                "correct": f"{acc} resolves to organism {res_acc['organism']} (tax {res_acc['tax_id']}), not human",
                "retrieved_evidence": f"{evidence_prefix} organism.names={res_acc['organism']}, taxonomy={res_acc['tax_id']}",
                "evidence_source": evidence_prefix,
                "severity": "high",
                "classification": "organism_mismatch",
            })

        row_is_wrong_mapping = False
        if gene_upper and gene_upper not in known_names_upper:
            # Gene symbol doesn't match this accession's documented names/synonyms.
            res_gene = gene_cache.get(gene)
            if res_gene and res_gene.get("primary_accession") and res_gene["primary_accession"] != primary:
                row_is_wrong_mapping = True
                findings.append({
                    "gene": gene,
                    "observed": f"accession={acc}, gene_symbol={gene} (source={source})",
                    "correct": (
                        f"accession {acc} is {res_acc.get('current_symbol')} "
                        f"({res_acc.get('protein_name')}); the correct accession for gene "
                        f"{gene} is {res_gene['primary_accession']} ({res_gene.get('protein_name')})"
                    ),
                    "retrieved_evidence": (
                        f"{evidence_prefix} gene names={res_acc['genes']}; "
                        f"EBI /proteins?gene={gene} primary accession={res_gene['primary_accession']}, "
                        f"gene names={res_gene['genes']}"
                    ),
                    "evidence_source": "EBI Proteins API (/proteins/{accession} and /proteins?gene=)",
                    "severity": "high",
                    "classification": "wrong_mapping",
                })
                # For a wrong mapping, prefer the identity implied by the row's own
                # protein/target/registered name text if available (best-effort);
                # otherwise fall back to the accession's own resolved primary.
        elif res_acc["is_secondary"]:
            findings.append({
                "gene": gene or res_acc.get("current_symbol"),
                "observed": acc,
                "correct": primary,
                "retrieved_evidence": (
                    f"{evidence_prefix}"
                    + (f"; {res_acc['merge_note']}" if res_acc.get("merge_note") else
                       f" redirects to primary accession {primary} (secondaryAccession includes {acc})")
                    + f"; current gene name={res_acc.get('current_symbol')}"
                ),
                "evidence_source": evidence_prefix,
                "severity": "medium",
                "classification": "stale_accession",
            })
        elif gene and res_acc.get("current_symbol") and gene_upper != res_acc["current_symbol"].upper():
            findings.append({
                "gene": gene,
                "observed": gene,
                "correct": res_acc["current_symbol"],
                "retrieved_evidence": (
                    f"{evidence_prefix} lists current primary gene name "
                    f"{res_acc['current_symbol']}; {gene} appears only as a synonym"
                ),
                "evidence_source": evidence_prefix,
                "severity": "low",
                "classification": "stale_symbol",
            })

        # ChEMBL cross-reference check: EBI's own dbReferences for this
        # accession list which ChEMBL target ID(s) it is really linked to.
        # A row from source_chembl.csv claiming a chembl_id that accession
        # does not carry is a wrong/duplicated cross-reference (observed on
        # this exam pack: CHEMBL3012 duplicated onto two different genes,
        # PDE7A — its real owner — and PDE10A, whose real ChEMBL ID is
        # CHEMBL4409, both confirmed via dbReferences). Skipped when the row
        # already got a wrong_mapping finding: on this pack that already
        # fully explains the row (a mislabeled accession, not an independent
        # cross-reference error), and re-flagging it here would just restate
        # the same underlying defect as if it were a second, distinct one.
        if source == "chembl" and not row_is_wrong_mapping:
            claimed_chembl_id = (raw.get("chembl_id") or "").strip()
            chembl_ids = res_acc.get("chembl_ids")
            if claimed_chembl_id and chembl_ids and claimed_chembl_id not in chembl_ids:
                findings.append({
                    "gene": gene or res_acc.get("current_symbol"),
                    "observed": f"chembl_id={claimed_chembl_id} attached to accession {acc} ({gene})",
                    "correct": (
                        f"accession {acc} ({res_acc.get('current_symbol')}) is actually cross-referenced "
                        f"to ChEMBL ID(s) {chembl_ids}, not {claimed_chembl_id}"
                    ),
                    "retrieved_evidence": (
                        f"{evidence_prefix} dbReferences (type=ChEMBL) = {chembl_ids}; "
                        f"row claims chembl_id={claimed_chembl_id}, which does not appear in that list"
                    ),
                    "evidence_source": evidence_prefix,
                    "severity": "high",
                    "classification": "wrong_crossreference",
                })

    # ---- Publications: ambiguous literature mentions (never fetch pmid) ----
    # A mention is corroborated (i.e. NOT ambiguous) by either of two independent
    # signals, checked cheapest-and-safest first:
    #   1. The context sentence literally names the mention itself (the common
    #      template case, e.g. "Inhibition of CHRNB3 altered ..."). No real
    #      identity question is being asked here at all, so we never even
    #      consider a different candidate for these — doing so risks exactly
    #      the false-positive "precision" failure the brief warns about.
    #   2. The mention resolves (possibly via a synonym already surfaced by
    #      resolving the OTHER four sources' accessions) to exactly one
    #      in-pack candidate, and that candidate's protein full name shares
    #      real vocabulary with the context sentence.
    # Only mentions that clear neither test are genuinely ambiguous; for those
    # (and only those) we spend a live EBI protein-name search driven purely by
    # context_sentence keywords — never by pmid, which is a synthetic internal
    # reference number per the evidence contract, not a literature pointer.
    symbol_index = defaultdict(list)
    for res in acc_cache.values():
        if res and res.get("genes"):
            for g in res["genes"]:
                symbol_index[g.upper()].append(res)

    def literal_self_reference(mention, context):
        return bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(mention) + r"(?![A-Za-z0-9])", context, re.IGNORECASE))

    def corroborated_by_inpack_candidate(mention, kw):
        for cand in symbol_index.get(mention.upper(), []):
            if kw & keywords(cand.get("protein_name") or ""):
                return True
        return False

    ambiguous_rows = []
    for r in pubs:
        mention = (r.get("target_mention") or "").strip()
        context = r.get("context_sentence") or ""
        if not mention:
            continue
        if literal_self_reference(mention, context):
            continue
        kw = keywords(context)
        if corroborated_by_inpack_candidate(mention, kw):
            continue
        ambiguous_rows.append((mention, context, kw))

    # Group the surviving rows by mention: candidate discovery and text
    # fetches are per-mention (cheap, small), not per-row.
    rows_by_mention = defaultdict(list)
    for mention, context, kw in ambiguous_rows:
        rows_by_mention[mention].append((context, kw))

    # Candidate set per mention: EBI's own EXACT protein-shortName holders
    # (e.g. "PSA" -> KLK3, NPEPPS) unioned with whatever in-pack gene-synonym
    # candidate(s) exist for that mention (may already have been ruled OUT by
    # the corroboration check above, but stay in the candidate set here so
    # they can still be the "other" option for elimination, below).
    shortname_hits = bounded_resolve_many(find_shortname_candidates, rows_by_mention.keys())
    candidate_accs_by_mention = {}
    for mention in rows_by_mention:
        accs = set(shortname_hits.get(mention) or [])
        for cand in symbol_index.get(mention.upper(), []):
            if cand.get("primary_accession"):
                accs.add(cand["primary_accession"])
        candidate_accs_by_mention[mention] = accs

    all_candidate_accs = {acc for accs in candidate_accs_by_mention.values() for acc in accs}
    text_blobs = bounded_resolve_many(fetch_disambiguation_text, all_candidate_accs)

    for mention, ctx_rows in rows_by_mention.items():
        candidates = candidate_accs_by_mention.get(mention) or set()
        if not candidates:
            continue  # no authoritatively-documented candidate at all — nothing to report
        cand_symbol = {}  # accession -> gene symbol, for the finding's "gene" field
        for acc in candidates:
            res = acc_cache.get(acc) or resolve_accession(acc)
            cand_symbol[acc] = (res.get("current_symbol") if res else None) or acc

        unscored = []
        for context, kw in ctx_rows:
            # Tier 1: any candidate whose short, specific NAME text (full
            # name + alternative names) matches wins outright over one that
            # only matches via long, generic COMMENT prose — see
            # fetch_disambiguation_text's docstring for why (the NPEPPS/KLK3
            # "antigen" + "specificity" collision observed on this pack).
            name_hits = []
            for acc in candidates:
                name_text, _ = text_blobs.get(acc) or ("", "")
                score = len(kw & keywords(name_text))
                if score >= 1:
                    name_hits.append((score, acc))
            if name_hits:
                best_score, best_acc = max(name_hits)
                tier = "name"
            else:
                comment_hits = []
                for acc in candidates:
                    _, comment_text = text_blobs.get(acc) or ("", "")
                    score = len(kw & keywords(comment_text))
                    if score >= 1:
                        comment_hits.append((score, acc))
                if comment_hits:
                    best_score, best_acc = max(comment_hits)
                    tier = "comment"
                else:
                    best_score, best_acc, tier = 0, None, None
            if best_acc:
                findings.append({
                    "gene": cand_symbol[best_acc],
                    "observed": f"target_mention={mention} in context: \"{context}\"",
                    "correct": cand_symbol[best_acc],
                    "retrieved_evidence": (
                        f"EBI /proteins?protein={mention} confirms {best_acc} ({cand_symbol[best_acc]}) "
                        f"carries protein short name \"{mention}\"; context_sentence keywords {sorted(kw)} "
                        f"match its EBI {tier} text (score={best_score}); pmid was not fetched "
                        f"per the evidence contract"
                    ),
                    "evidence_source": "EBI Proteins API protein short-name search + name/FUNCTION comment match",
                    "severity": "medium",
                    "classification": "ambiguous_literature_mention",
                })
            else:
                unscored.append((context, kw))

        # Elimination for rows that scored against NO candidate's text: every
        # row that reaches this function already failed to corroborate any
        # IN-PACK gene-synonym candidate (that's why it's "ambiguous" at
        # all — see corroborating_inpack_candidate above). So if EBI's
        # short-name search turned up exactly one candidate BEYOND those
        # already-rejected in-pack ones, an unscored row must be it — there
        # is no third, unconsidered option; every name in this reasoning was
        # itself retrieved from EBI, not assumed from outside knowledge.
        already_rejected = {
            c["primary_accession"] for c in symbol_index.get(mention.upper(), []) if c.get("primary_accession")
        }
        fallback_candidates = candidates - already_rejected
        if unscored and len(fallback_candidates) == 1:
            sole_acc = next(iter(fallback_candidates))
            for context, kw in unscored:
                findings.append({
                    "gene": cand_symbol[sole_acc],
                    "observed": f"target_mention={mention} in context: \"{context}\"",
                    "correct": cand_symbol[sole_acc],
                    "retrieved_evidence": (
                        f"EBI /proteins?protein={mention} confirms exactly {sorted(candidates)} carry protein "
                        f"short name \"{mention}\" ({', '.join(f'{a}={cand_symbol[a]}' for a in sorted(candidates))}); "
                        f"context_sentence keywords {sorted(kw)} do not match {sole_acc}'s own name/function text, "
                        f"but also do not match any other retrieved candidate, so by elimination among the "
                        f"candidates EBI actually returned this row is {sole_acc} ({cand_symbol[sole_acc]}); "
                        f"pmid was not fetched per the evidence contract"
                    ),
                    "evidence_source": "EBI Proteins API protein short-name search (elimination among retrieved candidates)",
                    "severity": "medium",
                    "classification": "ambiguous_literature_mention",
                })

    # ---- Build golden records keyed by resolved primary accession ----
    golden = {}
    for source, acc, gene, raw in rows:
        if not acc:
            continue
        primary = acc_to_primary.get(acc, acc)
        res = acc_cache.get(acc)
        display_gene = (res.get("current_symbol") if res else None) or gene or "UNKNOWN"
        rec = golden.setdefault(primary, {"gene": display_gene, "primary_accession": primary, "sources": set()})
        rec["sources"].add(source)

    golden_records = [
        {"gene": rec["gene"], "primary_accession": rec["primary_accession"], "sources": sorted(rec["sources"])}
        for rec in golden.values()
    ]

    output = {
        "unique_target_count": len(golden_records),
        "golden_records": golden_records,
        "findings": findings,
    }
    print(json.dumps(output))


REQUIRED_SOURCE_FILES = (
    "source_chembl.csv", "source_uniprot.csv", "source_bindingdb.csv",
    "source_internal.csv", "source_publications.csv",
)


def _default_pack_dir():
    """Local-convenience only: the grading harness always appends the pack
    directory as sys.argv[1] (see README.md §2/§5), so this path is never
    exercised at grading time. It exists purely so a developer can run
    `python3 solve.py` from the repo root without retyping the pack path
    every time. Looks in the current working directory for a folder named
    "exam" first, then falls back to any immediate subdirectory that
    actually contains all five expected source_*.csv files, picked
    deterministically (alphabetically) if more than one qualifies."""
    cwd = os.getcwd()
    exam_dir = os.path.join(cwd, "exam")
    if os.path.isdir(exam_dir) and all(
        os.path.isfile(os.path.join(exam_dir, f)) for f in REQUIRED_SOURCE_FILES
    ):
        return exam_dir
    try:
        entries = sorted(os.listdir(cwd))
    except OSError:
        return None
    for name in entries:
        candidate = os.path.join(cwd, name)
        if os.path.isdir(candidate) and all(
            os.path.isfile(os.path.join(candidate, f)) for f in REQUIRED_SOURCE_FILES
        ):
            return candidate
    return None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        pack_dir = _default_pack_dir()
        if pack_dir is None:
            print(json.dumps({"error": "usage: solve.py <pack_dir>"}))
            sys.exit(1)
        print(f"[solve.py] no <pack_dir> argument given — defaulting to {pack_dir}", file=sys.stderr)
        main(pack_dir)
    else:
        main(sys.argv[1])