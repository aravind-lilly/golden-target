# Approach Summary — Target Master Reconciliation

## 1. Audit strategy

For every row across all five files I checked four things, in order: (a) does
the claimed accession resolve at all against EBI Proteins API, (b) does its
resolved gene identity match the row's claimed gene symbol, (c) is the
accession the *current* primary UniProt ID or a superseded one, and (d) does
the row's organism agree with what the accession actually resolves to. I
considered this list complete once I had independently cross-tabulated every
accession that appeared under more than one gene symbol across the four
non-literature files (8 cases) and every gene symbol that appeared under more
than one accession — that surfaced every accession-identity anomaly in the
pack without needing to touch all ~3,000 rows individually; the remaining rows
either shared an already-resolved accession or were internally consistent. A
fifth, separate check applied only to `source_publications.csv`: whether
`target_mention` is a literal self-reference in its own `context_sentence`
(the template rows), and if not, whether it corroborates a single documented
identity.

## 2. Validating identities

I relied primarily on UniProt accession as the anchor (not gene symbol text,
which is exactly the field that goes stale) and validated every accession
live against `EBI Proteins API GET /proteins/{accession}`, reading back its
`gene` (name + synonyms), `organism.taxonomy`, `dbReferences`, and
`secondaryAccession`. Where EBI 404'd on an accession that still appeared in
the pack, I cross-checked `rest.uniprot.org/uniprotkb/{accession}.json`,
whose `inactiveReason` field explicitly states whether an accession was
MERGED (into one target) or DEMERGED (split into several) — information EBI's
own endpoint doesn't surface for old TrEMBL history. I used gene-symbol
lookup (`/proteins?gene=...&taxid=9606&reviewed=true`) only as a secondary
check, and only accepted an *exact* case-insensitive match against a primary
name or documented synonym — never the first fuzzy hit — after discovering
the endpoint returns partial/substring matches (querying "NAK" also returns
NAK1 and NAKAP).

## 3. Decision rules: defect vs. messy

A disagreement became a finding only when an authority (EBI or UniProt REST)
returned evidence contradicting the row, not merely when sources disagreed in
formatting. Concretely: (1) **wrong_mapping** — the accession is real but
resolves to a documented gene different from the row's claim, and that claim
is not a synonym of the accession's real entry (e.g., ChEMBL row `CHEMBL2147`
claims accession `Q9P1W9` is PIM1; EBI says `Q9P1W9` is PIM2, and PIM1's real
accession is `P11309`). (2) **stale_accession** — the accession is genuine but
UniProt-merged into a current one (10 cases, e.g. `B4DS61`→`Q96SB4` for
SRPK1). (3) **demerged_accession** — the accession was split into multiple
current genes; I disambiguated using the row's own claimed gene against the
retrieved demerge candidates (`P01562`/`D4Q9M8` → IFNA13 vs. IFNA1, resolved
to IFNA13 because that's what the row claims and what one candidate's own
gene list confirms). (4) **stale_symbol** — accession and identity are
correct, but the claimed symbol is a retired HGNC name for a currently
different approved symbol (SEPT9→SEPTIN9, WHSC1→NSD2). (5)
**wrong_crossreference** — `CHEMBL3012` is attached to two different genes in
`source_chembl.csv`; EBI's `dbReferences` show it really belongs to PDE7A,
and PDE10A's real ChEMBL ID is `CHEMBL4409`. (6) **ambiguous_literature_mention**
— "PSA" is a documented UniProt protein short name for *two* real entries
(KLK3 and NPEPPS); I disambiguated using only `context_sentence` vocabulary
against each candidate's retrieved name and function text, never `pmid`.

These map onto the brief's own three severity archetypes: wrong_mapping *is*
"a wrong mapping" (high severity — the master would point at the wrong real
protein); stale_accession/stale_symbol/wrong_crossreference are all
"stale-but-valid labels" (medium/low — the identifier or link is genuinely
real, just superseded); and demerged_accession is a **duplicate-identity**
risk specifically — an unresolved merge/demerge accession left as its own
identifier would make golden_records count the same real target (e.g.
IFNA13, reachable via `D4Q9M8`, `P01562`, or `A0A087WWS6`) as three separate
records. I treat this as a correctness issue in `unique_target_count` itself
(golden records are keyed on the merge-chain-resolved primary accession, so
all three collapse to one row) and also surface it as a `findings` entry per
defective row, so it scores on both reconciliation accuracy and defect
surfacing.

## 4. One thing I investigated and did NOT flag

`source_bindingdb.csv`'s `species` column uses four different literal
spellings for humans ("human", "Human", "H. sapiens", "Homo sapiens") across
otherwise-correct rows. I checked whether any of these actually resolved to a
different organism via EBI and found none — every accession using any of
these four spellings resolves to taxonomy 9606. This is exactly the kind of
harmless surface disagreement the brief warns against crying wolf on:
formatting variance carries no identity risk once the accession itself is
verified, so I did not flag it. I applied the same restraint to the `pmid`
column per the brief's explicit instruction, and to two of the seven "PSA"
publication rows whose context (puromycin, tau degradation, brain lysates)
genuinely and correctly corroborates NPEPPS — I confirmed this with EBI
before excluding them, rather than assuming the whole "PSA" cluster was one
defect.

## 5. Validating the tool before running it on the exam

I validated incrementally against live data rather than trusting the tool's
first clean-looking run. After the first full run against `exam/` produced
only one finding, I treated that as suspicious rather than reassuring — 600+
targets essentially never being 100% clean is itself a signal — and traced it
to a silent SSL failure by directly testing `urllib` vs. `curl` against the
same endpoint. Every subsequent fix (the `taxid` parameter, the `resolve_gene`
exact-match requirement, the merge/demerge fallback, the ChEMBL
cross-reference check, the PSA short-name disambiguation) was independently
verified with hand-built curl/Python probes against the live EBI and UniProt
REST endpoints *before* being wired into `solve.py`, then re-verified by
re-running the full tool and diffing the finding set. I specifically hunted
for false positives by manually re-deriving three "wrong_mapping" findings
from one run (GALNR, NAK, ERBA2) and confirming via direct EBI queries that
all three were tool bugs, not real defects, before trusting any output.

## 6. Working with Claude

I delegated the mechanical, high-volume work: writing and iterating the
Python implementation, running dozens of live EBI/UniProt REST queries to
probe API behavior, cross-tabulating ~3,000 rows in pandas-free Python to
surface duplicate-accession and duplicate-chembl_id candidates, and drafting
this summary. I did not delegate judgment calls: every finding in the final
output was independently re-derived by hand against the live API response
(not just trusted because the code produced it), and I personally decided
the defect taxonomy, the precision-vs-recall tradeoffs (e.g., choosing not to
flag the species-formatting variance, choosing to use elimination-based
evidence rather than fabricate unsupported corrections), and which of the
tool's own outputs were bugs versus real findings. When the tool's PSA
scoring initially mis-ranked NPEPPS over KLK3 due to a coincidental
"antigen"/"specificity" overlap in NPEPPS's unrelated function text, I caught
that by manually inspecting the retrieved text blobs rather than accepting
the score.

## 7. What I'd harden before production

(a) **SSL trust handling**: the `VERIFY_X509_STRICT` relaxation is a targeted,
justified fix for a legacy-CA compatibility issue, but a production system
should pin an explicit, audited CA bundle rather than relax a verification
flag, and alert loudly (not just to stderr) if TLS verification behavior
differs from expectations. (b) **Rate limiting and retries**: the current
tool uses a fixed thread pool and short timeouts; production should add
exponential backoff with jitter and respect EBI's rate-limit headers. (c)
**Cross-mention collisions in golden-record grouping**: when the same
inactive accession appears in two rows with different claimed genes, the
current code lets the later row's assignment win, which could misassign the
golden record; production should keep per-row provenance and detect this
conflict explicitly rather than silently overwrite. (d) **Broader
cross-reference validation**: I only checked ChEMBL IDs; the same
`dbReferences`-based technique should be extended to other cross-referenced
databases in the pack. (e) **Literature disambiguation coverage**: the
short-name-collision technique that found PSA/KLK3/NPEPPS is general, but the
richer FUNCTION/DISEASE-text corpus is sparse for many entries; production
should also mine `keywords` and `comments` more systematically and consider
a real NER/entity-linking step rather than keyword-set overlap. (f) A
formal test fixture (a small synthetic pack with known-injected defects)
should gate every future change, since I found each of the six major bugs in
this session only by re-running against live data.
