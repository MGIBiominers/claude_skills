---
name: verify-references
description: >
  Verify scientific identifiers (UniProt accessions, Pfam IDs, DOIs, PMIDs,
  NCBI accessions) in a project against authoritative sources. Use when
  generating reference data (positive controls, seed lists, citations),
  before committing identifier lists, or auditing an existing project for
  hallucinated accessions. Trigger when the user says "verify references",
  "check accessions", "audit my IDs", or asks whether protein/paper IDs are
  correct.
version: 1.0.0
user-invocable: true
argument-hint: "[path-or-file]"
---

You are auditing scientific identifiers in a project for correctness against authoritative sources. The goal is to catch LLM-hallucinated identifiers (right format, wrong specific value) before they corrupt downstream work.

## Setup

The companion Python module `verifier.py` lives next to this SKILL.md at `~/.claude/skills/verify-references/verifier.py`. It is stdlib-only (urllib, re, json) and works in any environment with Python 3.10+.

Resolve `$ARGUMENTS`:
- Empty: scan the current working directory
- A file path: scan only that file
- A directory path: scan all files under it (respecting .gitignore-ish defaults: skip `.git`, `__pycache__`, `node_modules`, parquet/binary blobs)
- A YAML/JSON manifest with explicit `references:` entries (see "Structured verification" below): verify those specifically

## How to run

```bash
# Audit a project for hallucinated identifiers
python3 ~/.claude/skills/verify-references/verifier.py scan <target>

# Verbose progress to stderr
python3 ~/.claude/skills/verify-references/verifier.py -v scan <target>

# JSON output for CI integration (machine-readable, exit code 1 on MISMATCH/NOT_FOUND)
python3 ~/.claude/skills/verify-references/verifier.py --format json scan <target>

# Limit which identifier types are scanned
python3 ~/.claude/skills/verify-references/verifier.py --types uniprot,doi scan <target>

# Verify a structured manifest with explicit expectations
python3 ~/.claude/skills/verify-references/verifier.py verify references.yaml
```

Per-identifier outcomes:
- VERIFIED: identifier exists AND expected fields (from context) match the fetched record
- MISMATCH: identifier exists but expected fields do NOT match — the smoking gun
- NOT_FOUND: identifier does not exist in the authoritative source
- EXISTS: identifier exists, no structural context anchored an expectation (loose mention in prose, log file, etc.) — reported but not flagged
- FETCH_FAIL: network error; retry

The scanner is conservative by design. It only generates a MISMATCH when one of these **structural context patterns** anchors an expected protein/organism name tightly to the identifier:

1. **Dict key with embedded accession** — `"ManducaApoLipIII_P80668":` in Python/JSON dicts. The camelcase label is mined for known protein-name keywords (apolipophorin, chaperonin, luciferase, ...) and common genera (Manduca, Galleria, Drosophila, ...).
2. **FASTA header** — `>POSITIVE_CONTROL|ManducaApoLipIII_P80668|description` or similar pipe-delimited formats. Both label and description are mined.
3. **Parenthetical naming** — `MBP (P0AEX9), NusA (P0A5Y6)` style. Each identifier gets its own immediate label as the expected name (or a vocab-matched keyword if the label is a known abbreviation).
4. **Trailing comment** — `"EcNusA_P0A5Y6":  "P0A5Y6",   # E. coli NusA`. Comment text after the identifier is scanned for organism binomials and protein keywords.
5. **Structured record (multi-line)** — a dataclass/dict/object record where the identity and the accession are sibling fields on different lines, e.g. `Anchor(name="aerobactin_receptor_IutA", protein_id="WP_012375988.1", description="Ferric aerobactin receptor IutA")`. The scanner extracts the enclosing `(...)`/`{...}` block by bracket matching, mines the identity-bearing fields (`name`, `product`, `gene`, `description`, `label`, `title`, `annotation`, `note` — excluding the field that holds the accession) for salient words, and verifies them by **word overlap** against the fetched record's name. Zero overlap of salient words (generic terms like "protein", "family", "domain", "hypothetical" are stripped from both sides) is a MISMATCH. Unlike patterns 1-4, this does not depend on a curated keyword vocabulary — the expectation is derived from the record's own descriptive text, so it works for any domain.

If none of these patterns match, the identifier is reported as EXISTS — it's not an error to mention an accession in a log file or prose without claiming what it is. This dramatically reduces false positives: tested on a real project (~37 unique IDs across 27 files), the scanner produced 22 VERIFIED, 9 EXISTS, and 6 MISMATCH — and all 6 MISMATCH were real accession errors. Pattern 5 was added after a project hardcoded 16 capability-scoring anchors as `Anchor(name=..., protein_id=..., description=...)` records: all 16 accessions were confabulated (resolved to unrelated proteins), but patterns 1-4 saw only the bare `protein_id=` line and reported EXISTS. Pattern 5 flags all 16.

Network calls go to UniProt REST (uniprot.org), InterPro REST (ebi.ac.uk), CrossRef (crossref.org), and NCBI E-utilities — all free, no auth. Polite throttling: ~200ms between requests.

UniProt name matching uses the recommended name + all alternative names + EC numbers + gene synonyms, joined into a single searchable string. This catches the case where the canonical name is enzymatic ("Luciferin 4-monooxygenase") but the project uses the family name ("luciferase").

## Structured verification (preferred for new projects)

Encourage the user to define reference data in a YAML file with explicit expectations:

```yaml
references:
  - id: ManducaApoLipIII
    type: uniprot_accession
    value: P14217
    expected:
      protein_name_contains: "apolipophorin"
      organism: "Manduca sexta"
      length_range: [100, 250]
      pfam_contains: PF07464
  - id: original_esm2_paper
    type: doi
    value: 10.1126/science.ade2574
    expected:
      title_contains: "evolutionary-scale prediction"
      year: 2023
```

Then:
```bash
python3 ~/.claude/skills/verify-references/verifier.py verify references.yaml
```

This catches the failure mode where an identifier is valid (returns a record) but the record is for the WRONG protein/paper. Without `expected` fields, the verifier can only confirm "this ID exists somewhere" — not "this ID is the thing you claimed."

## Reporting

After running:
1. Show the user the per-identifier results (VERIFIED / MISMATCH / NOT_FOUND counts)
2. For each MISMATCH or NOT_FOUND, recommend the next action:
   - MISMATCH: re-derive the correct identifier by searching the source by name + organism (NEVER from your own memory)
   - NOT_FOUND: the identifier likely never existed in this database; check if it lives in a different source (e.g., obsolete UniProt accession moved to a new ID)
3. If the project has hardcoded Python dicts of identifiers (like `POSITIVE_CONTROLS = {...}`), recommend migrating to a YAML manifest so future verifications are sustainable.

## Re-derivation pattern (when something is wrong)

When the user needs a CORRECT identifier to replace a wrong one, NEVER generate it from memory. Instead:

1. Use the authoritative source's search-by-name API:
   - UniProt: `https://rest.uniprot.org/uniprotkb/search?query=organism_name:"Manduca sexta"+AND+protein_name:"apolipophorin"&format=json`
   - CrossRef: `https://api.crossref.org/works?query.title=<title>&query.author=<author>`
2. Pick the reviewed (SwissProt / canonical / published) entry
3. Sanity-check returned length / Pfam / year against expectation
4. Only then offer the identifier as a replacement, citing the source URL

## Identifier types currently supported

| Type | Pattern | Authoritative source |
|---|---|---|
| `uniprot` | UniProt accessions (P12345, Q9XXX9, etc.) | rest.uniprot.org |
| `pfam` | Pfam family IDs (PF\d{5}) | InterPro (ebi.ac.uk) |
| `interpro` | InterPro entries (IPR\d{6}) | InterPro |
| `go` | Gene Ontology terms (GO:\d{7}) | QuickGO (ebi.ac.uk) |
| `chebi` | ChEBI compounds (CHEBI:\d+) | EBI OLS |
| `ncbi_protein` | NCBI protein accessions (NP_, XP_, WP_, YP_) | NCBI E-utilities |
| `ncbi_nucleotide` | NCBI nucleotide accessions (NC_, NM_, XM_, NR_) | NCBI E-utilities |
| `ncbi_assembly` | NCBI genome assemblies (GCA_, GCF_) | NCBI Datasets v2 |
| `doi` | Digital Object Identifiers | api.crossref.org → fallback api.datacite.org |
| `pubmed` | PubMed IDs (requires PMID: prefix to disambiguate) | NCBI E-utilities |
| `orcid` | ORCID author IDs (0000-0000-0000-000X) | pub.orcid.org |
| `arxiv` | arXiv preprint IDs (new YYMM.NNNN(N) or old subject.class/YYMMNNN, optional vN suffix, optional `arXiv:` prefix) | export.arxiv.org Atom XML API |

DOI verification automatically falls back to DataCite when CrossRef returns 404. Required for Zenodo, Figshare, and other DataCite-registered dataset DOIs which CrossRef doesn't index.

arXiv lookups return `{title, authors, year, journal, arxiv_id}` extracted from the Atom XML response. The skill auto-strips `arXiv:` prefixes and version suffixes (vN) before querying so manifest entries can use either form. In YAML manifests, quote numeric-looking arXiv IDs like `"1702.01417"` so the loader does not coerce them to floats.

JSON output mode (`--format json`) auto-suppresses `-v` verbose output so the stderr stream doesn't pollute the JSON when piped via `2>&1`. If you want verbose progress AND JSON output, redirect stderr separately: `verifier.py -v --format json scan . 2>verbose.log | jq .`

All regex patterns use non-alnum lookarounds (not `\b`) so accessions embedded in labels like `"ManducaApoLipIII_P80668":` are matched correctly. (Word boundary `\b` treats `_` as a word character, which silently misses accessions adjacent to underscores -- a subtle bug.)

Use `--types <comma-separated>` to limit which types are scanned. Example: `--types uniprot,pfam,doi`.

Adding a new identifier type: edit `verifier.py`, add to `SPECS` registry. Each spec needs:
- `pattern`: regex with non-alnum lookarounds
- `url_template`: lookup URL with `{id}` placeholder
- `extractor`: function mapping JSON response → comparison fields dict (`name`, `organism`, `length`, etc.)
- Optional `normalize`: function to strip the raw match to the API-accepted id (e.g. "PMID:12345" → "12345")

## When NOT to use

- For unstructured natural-language references in prose (e.g., "as shown in Smith et al. 2024"). Use a citation manager.
- When the identifier is something the user defined themselves (internal lab IDs, project codes).
- When the project is small enough that manual verification is cheaper than tool setup.
