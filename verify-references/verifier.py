#!/usr/bin/env python3
"""
verify-references: look up scientific identifiers against authoritative sources.

Stdlib-only (urllib, re, json) so it works in any Python 3.10+ environment
without dependencies. Designed to catch LLM-hallucinated identifiers --
right format, wrong specific value.

Two modes:
  scan <path>        Find identifier-looking patterns in files under <path>,
                     look each up, and flag MISMATCH where surrounding
                     structural context (dict key, FASTA header, parenthetical
                     naming, comment) implies a different protein/paper than
                     UniProt/CrossRef/etc. returns.
  verify <manifest>  Read a YAML/JSON manifest with explicit `references:`
                     entries (id, type, value, expected) and verify each
                     against the appropriate authoritative source.

The scanner is conservative: it only flags MISMATCH when a STRUCTURAL
context binds an expected protein/organism name tightly to the identifier
(e.g. dict key like "ManducaApoLipIII_P80668" → expects "apolipophorin"
in the protein name AND "Manduca" in organism). Loose mentions in prose,
log files, or analysis dumps are reported as EXISTS (no expectation), not
MISMATCH.

Exit code:
  0 = all VERIFIED or EXISTS (no mismatches)
  1 = at least one MISMATCH or NOT_FOUND
  2 = fetch errors prevented complete verification
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

USER_AGENT = "claude-verify-references/2.0"
DEFAULT_TIMEOUT = 15
POLITENESS_SLEEP = 0.2

# --- per-type spec --------------------------------------------------------


@dataclass
class IdSpec:
    type_name: str
    display_name: str
    pattern: str
    url_template: str
    extractor: Callable[[dict], dict]
    accept: str = "application/json"
    normalize: Callable[[str], str] | None = None  # raw match → API id
    # Response body parser. Default is JSON; override for sources that return
    # XML or other formats (e.g. arXiv returns Atom XML).
    parser: Callable[[bytes], dict] | None = None
    # Optional fallback: if primary lookup returns 404, retry against this URL
    # template with the fallback extractor. Useful for DOIs (CrossRef →
    # DataCite for Zenodo / Figshare / dataset DOIs).
    fallback_url_template: str | None = None
    fallback_extractor: Callable[[dict], dict] | None = None


def _strip_to_digits(s: str) -> str:
    m = re.search(r"\d+", s)
    return m.group(0) if m else s


def _strip_chebi_prefix(s: str) -> str:
    return s.split(":", 1)[1] if ":" in s else s


def _strip_go_prefix(s: str) -> str:
    return s  # keep "GO:0000000" intact; APIs accept it that way


def _extract_uniprot(j: dict) -> dict:
    """Collect recommended + alternative + submission names into a single
    pipe-joined string so substring matches catch nomenclature variants
    (e.g. 'luciferase' matches when the canonical name is 'Luciferin 4-monooxygenase')."""
    desc = j.get("proteinDescription", {})
    names: list[str] = []

    def _harvest(entry: dict) -> None:
        full = entry.get("fullName", {}).get("value", "")
        if full:
            names.append(full)
        for short in entry.get("shortNames", []) or []:
            v = short.get("value", "")
            if v:
                names.append(v)
        for ec in entry.get("ecNumbers", []) or []:
            v = ec.get("value", "")
            if v:
                names.append(f"EC {v}")

    rec = desc.get("recommendedName")
    if isinstance(rec, dict):
        _harvest(rec)
    for entry in desc.get("alternativeNames", []) or []:
        _harvest(entry)
    for entry in desc.get("submissionNames", []) or []:
        _harvest(entry)
    # Also pull gene-symbol synonyms; useful when "EcNusA" matches gene name "nusA"
    for gene in j.get("genes", []) or []:
        primary = gene.get("geneName", {}).get("value", "")
        if primary:
            names.append(primary)
        for syn in gene.get("synonyms", []) or []:
            v = syn.get("value", "")
            if v:
                names.append(v)
    name_str = " | ".join(names)

    organism = j.get("organism", {}).get("scientificName", "")
    length = j.get("sequence", {}).get("length", 0)
    pfam = [
        ref.get("id", "")
        for ref in j.get("uniProtKBCrossReferences", [])
        if ref.get("database") == "Pfam"
    ]
    return {"name": name_str, "organism": organism, "length": length, "pfam": pfam}


def _extract_pfam(j: dict) -> dict:
    """InterPro returns: {metadata: {name: {name: 'X', short: 'Y'}, description: [...]}}."""
    meta = j.get("metadata", {})
    raw_name = meta.get("name", {})
    if isinstance(raw_name, dict):
        full = raw_name.get("name", "")
        short = raw_name.get("short", "")
        name = full or short
    else:
        name = str(raw_name)
    desc_field = meta.get("description")
    description = ""
    if isinstance(desc_field, list) and desc_field:
        first = desc_field[0]
        if isinstance(first, dict):
            description = first.get("text", "")
        else:
            description = str(first)
    elif isinstance(desc_field, str):
        description = desc_field
    return {"name": name, "description": description}


def _extract_crossref(j: dict) -> dict:
    msg = j.get("message", {})
    return {
        "title": " ".join(msg.get("title", [])),
        "authors": [a.get("family", "") for a in msg.get("author", [])],
        "year": (msg.get("created", {}).get("date-parts") or [[None]])[0][0],
        "journal": " ".join(msg.get("container-title", [])),
        "doi": msg.get("DOI", ""),
    }


def _extract_ncbi(j: dict) -> dict:
    summary = j.get("result", {})
    for key in summary:
        if key == "uids":
            continue
        rec = summary[key]
        return {
            "name": rec.get("title", ""),
            "organism": rec.get("organism", ""),
            "length": rec.get("slen", 0),
        }
    return {}


def _extract_pubmed(j: dict) -> dict:
    """NCBI E-utilities esummary db=pubmed."""
    summary = j.get("result", {})
    for key in summary:
        if key == "uids":
            continue
        rec = summary[key]
        return {
            "title": rec.get("title", ""),
            "authors": [a.get("name", "") for a in rec.get("authors", [])],
            "year": (rec.get("pubdate") or "").split(" ")[0],
            "journal": rec.get("source", ""),
        }
    return {}


def _extract_go(j: dict) -> dict:
    """QuickGO returns: {numberOfHits: N, results: [{id, name, definition: {text}, aspect, isObsolete}]}."""
    results = j.get("results", [])
    if not results:
        return {}
    r = results[0]
    defn = r.get("definition", {})
    description = defn.get("text", "") if isinstance(defn, dict) else str(defn)
    return {
        "name": r.get("name", ""),
        "description": description,
        "aspect": r.get("aspect", ""),  # biological_process / molecular_function / cellular_component
        "obsolete": bool(r.get("isObsolete", False)),
    }


def _extract_chebi(j: dict) -> dict:
    """EBI OLS returns: {_embedded: {terms: [{label, description, synonyms}]}}."""
    terms = j.get("_embedded", {}).get("terms", [])
    if not terms:
        return {}
    t = terms[0]
    desc = t.get("description", [])
    if isinstance(desc, list):
        desc = " | ".join(str(d) for d in desc)
    return {
        "name": t.get("label", ""),
        "description": str(desc),
        "synonyms": t.get("synonyms", []) or [],
    }


def _extract_orcid(j: dict) -> dict:
    """ORCID public API v3.0 record endpoint."""
    person = j.get("person", {})
    name = person.get("name") or {}
    given = (name.get("given-names") or {}).get("value", "") if isinstance(name, dict) else ""
    family = (name.get("family-name") or {}).get("value", "") if isinstance(name, dict) else ""
    full = f"{given} {family}".strip()
    return {"name": full, "given": given, "family": family}


def _extract_datacite(j: dict) -> dict:
    """DataCite API returns: {data: {attributes: {titles: [...], publisher,
    publicationYear, types: {resourceTypeGeneral, ...}}}}.
    Used as fallback when CrossRef 404s -- typical for Zenodo, Figshare,
    and other DataCite-registered DOIs."""
    attrs = (j.get("data") or {}).get("attributes", {})
    titles = attrs.get("titles") or []
    title = ""
    if titles:
        first = titles[0]
        title = first.get("title", "") if isinstance(first, dict) else str(first)
    return {
        "title": title,
        "authors": [
            c.get("name", "") or c.get("familyName", "")
            for c in attrs.get("creators", []) or []
        ],
        "year": attrs.get("publicationYear", ""),
        "journal": attrs.get("publisher", ""),
        "doi": attrs.get("doi", ""),
        "type": (attrs.get("types") or {}).get("resourceTypeGeneral", ""),
    }


def _parse_arxiv_atom(raw: bytes) -> dict:
    """Parse the Atom XML response from arxiv.org/api/query into a flat dict.

    The arXiv API returns an Atom feed with one <entry> per result. We extract
    the first entry's title, authors, published date, DOI (if present), and
    journal-ref (if present), and return a dict matching the shape that other
    extractors produce.
    """
    import xml.etree.ElementTree as ET
    ns = {"a": "http://www.w3.org/2005/Atom",
          "arxiv": "http://arxiv.org/schemas/atom"}
    root = ET.fromstring(raw.decode("utf-8", errors="replace"))
    entry = root.find("a:entry", ns)
    if entry is None:
        return {"title": "", "authors": [], "year": "", "journal": "",
                "arxiv_id": ""}
    title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
    title = " ".join(title.split())  # collapse whitespace
    published = entry.findtext("a:published", default="", namespaces=ns) or ""
    year = published[:4] if len(published) >= 4 else ""
    authors = [
        (a.findtext("a:name", default="", namespaces=ns) or "").strip()
        for a in entry.findall("a:author", ns)
    ]
    raw_id = entry.findtext("a:id", default="", namespaces=ns) or ""
    arxiv_id = raw_id.rsplit("/", 1)[-1] if raw_id else ""
    journal = (entry.findtext("arxiv:journal_ref", default="", namespaces=ns)
               or "arXiv").strip()
    return {"title": title, "authors": authors, "year": year,
            "journal": journal, "arxiv_id": arxiv_id}


def _normalize_arxiv(s: str) -> str:
    """Strip 'arXiv:' prefix and version suffix so the ID matches what the
    API expects (the id_list parameter accepts both with and without version,
    but we use the unversioned form for consistency)."""
    # Defensive: YAML may coerce a bare numeric arXiv ID like 1702.01417 to a
    # float. Force to string before any further processing.
    s = str(s).strip()
    if s.lower().startswith("arxiv:"):
        s = s.split(":", 1)[1]
    # Strip trailing version like vN
    m = re.match(r"^(.*?)v\d+$", s)
    if m:
        s = m.group(1)
    return s


def _extract_ncbi_assembly(j: dict) -> dict:
    """NCBI Datasets v2alpha returns: {reports: [{accession, organism,
    assembly_info, ...}], total_count}."""
    reports = j.get("reports") or []
    if not reports:
        return {}
    r = reports[0]
    org = r.get("organism") or {}
    info = r.get("assembly_info") or {}
    return {
        "name": info.get("assembly_name", "") or r.get("accession", ""),
        "organism": org.get("organism_name", ""),
        "taxon_id": org.get("tax_id", ""),
        "level": info.get("assembly_level", ""),
        "submitter": info.get("submitter", ""),
    }


SPECS: dict[str, IdSpec] = {
    "uniprot": IdSpec(
        type_name="uniprot",
        display_name="UniProt accession",
        # Use non-alnum lookarounds (not \b) so accessions embedded in labels
        # like "ManducaApoLipIII_P80668" are still matched. \b treats _ as a
        # word character, so it fails to find P80668 when preceded by '_'.
        pattern=(
            r"(?<![A-Za-z0-9])"
            r"(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9][A-Z][A-Z0-9]{2}[0-9])"
            r"(?![A-Za-z0-9])"
        ),
        url_template="https://rest.uniprot.org/uniprotkb/{id}.json",
        extractor=_extract_uniprot,
    ),
    "pfam": IdSpec(
        type_name="pfam",
        display_name="Pfam family ID",
        pattern=r"(?<![A-Za-z0-9])PF\d{5}(?![A-Za-z0-9])",
        url_template="https://www.ebi.ac.uk/interpro/api/entry/pfam/{id}",
        extractor=_extract_pfam,
    ),
    "doi": IdSpec(
        type_name="doi",
        display_name="DOI",
        pattern=r"(?<![A-Za-z0-9])10\.\d{4,9}/[-._;()/:A-Za-z0-9]+(?![A-Za-z0-9])",
        url_template="https://api.crossref.org/works/{id}",
        extractor=_extract_crossref,
        # Zenodo, Figshare, and other DataCite-registered DOIs aren't in
        # CrossRef -- fall back to DataCite on 404.
        fallback_url_template="https://api.datacite.org/dois/{id}",
        fallback_extractor=_extract_datacite,
    ),
    "ncbi_protein": IdSpec(
        type_name="ncbi_protein",
        display_name="NCBI protein accession",
        pattern=r"(?<![A-Za-z0-9])(?:NP|XP|WP|YP)_\d+(?:\.\d+)?(?![A-Za-z0-9])",
        url_template="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=protein&id={id}&retmode=json",
        extractor=_extract_ncbi,
    ),
    "ncbi_nucleotide": IdSpec(
        type_name="ncbi_nucleotide",
        display_name="NCBI nucleotide accession",
        pattern=r"(?<![A-Za-z0-9])(?:NC|NM|XM|NR)_\d+(?:\.\d+)?(?![A-Za-z0-9])",
        url_template="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=nuccore&id={id}&retmode=json",
        extractor=_extract_ncbi,
    ),
    "interpro": IdSpec(
        type_name="interpro",
        display_name="InterPro entry",
        pattern=r"(?<![A-Za-z0-9])IPR\d{6}(?![A-Za-z0-9])",
        url_template="https://www.ebi.ac.uk/interpro/api/entry/InterPro/{id}",
        extractor=_extract_pfam,  # same nested-name shape as Pfam endpoint
    ),
    "go": IdSpec(
        type_name="go",
        display_name="GO term",
        pattern=r"(?<![A-Za-z0-9])GO:\d{7}(?![A-Za-z0-9])",
        url_template="https://www.ebi.ac.uk/QuickGO/services/ontology/go/terms/{id}",
        extractor=_extract_go,
        normalize=_strip_go_prefix,
    ),
    "pubmed": IdSpec(
        type_name="pubmed",
        display_name="PubMed ID",
        # Require an explicit prefix ('PMID' / 'pmid' / 'PubMed') to avoid
        # matching arbitrary 6-9 digit numbers.
        pattern=r"(?<![A-Za-z0-9])(?:PMID|pmid|PubMed|pubmed)[:\s_]*\d{6,9}(?![A-Za-z0-9])",
        url_template="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed&id={id}&retmode=json",
        extractor=_extract_pubmed,
        normalize=_strip_to_digits,
    ),
    "chebi": IdSpec(
        type_name="chebi",
        display_name="ChEBI compound",
        pattern=r"(?<![A-Za-z0-9])CHEBI:\d+(?![A-Za-z0-9])",
        url_template="https://www.ebi.ac.uk/ols/api/ontologies/chebi/terms?obo_id=CHEBI:{id}",
        extractor=_extract_chebi,
        normalize=_strip_chebi_prefix,
    ),
    "orcid": IdSpec(
        type_name="orcid",
        display_name="ORCID ID",
        pattern=r"(?<![A-Za-z0-9])\d{4}-\d{4}-\d{4}-\d{3}[\dX](?![A-Za-z0-9])",
        url_template="https://pub.orcid.org/v3.0/{id}/record",
        extractor=_extract_orcid,
    ),
    "ncbi_assembly": IdSpec(
        type_name="ncbi_assembly",
        display_name="NCBI genome assembly accession",
        pattern=r"(?<![A-Za-z0-9])GC[AF]_\d{9}(?:\.\d+)?(?![A-Za-z0-9])",
        url_template="https://api.ncbi.nlm.nih.gov/datasets/v2/genome/accession/{id}/dataset_report",
        extractor=_extract_ncbi_assembly,
    ),
    "arxiv": IdSpec(
        type_name="arxiv",
        display_name="arXiv preprint",
        # Matches new-format IDs (YYMM.NNNN or YYMM.NNNNN, optional vN) and
        # old-format IDs (subject[.subclass]/YYMMNNN, optional vN). Both are
        # captured with optional "arXiv:" prefix at scan time; the normalize
        # function strips both prefix and version before query.
        pattern=(
            r"(?<![A-Za-z0-9])"
            r"(?:arXiv:)?"
            r"(?:"
            r"\d{4}\.\d{4,5}(?:v\d+)?"
            r"|"
            r"[a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?"
            r")"
            r"(?![A-Za-z0-9])"
        ),
        url_template="http://export.arxiv.org/api/query?id_list={id}",
        extractor=lambda d: d,  # parser already returns the flat dict
        parser=_parse_arxiv_atom,
        accept="application/atom+xml",
        normalize=_normalize_arxiv,
    ),
}

# --- network -------------------------------------------------------------


def _fetch_json(url: str, accept: str = "application/json") -> dict:
    """JSON fetch (default). Kept for backward compatibility with any callers
    outside the verify_one path."""
    return _fetch_response(url, accept=accept, parser=_default_json_parser)


def _default_json_parser(raw: bytes) -> dict:
    return json.loads(raw.decode("utf-8"))


def _fetch_response(
    url: str,
    accept: str = "application/json",
    parser: Callable[[bytes], dict] | None = None,
) -> dict:
    """Generic fetch that returns the parsed response as a dict. The parser
    determines how to convert bytes to a dict (default: JSON). Specs that
    return XML or other formats should pass a custom parser."""
    parser = parser or _default_json_parser
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": accept}
    )
    with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as r:
        return parser(r.read())


# --- verification --------------------------------------------------------


@dataclass
class Hit:
    """A single occurrence of an identifier in a file with its structural context."""
    type_name: str
    identifier: str
    file_path: Path
    line_no: int
    line: str
    pattern_name: str  # which structural pattern anchored this (or 'loose')
    expected: dict


@dataclass
class Result:
    identifier: str
    type_name: str
    status: str  # VERIFIED | MISMATCH | NOT_FOUND | EXISTS | FETCH_FAIL
    fields: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    source_url: str = ""
    hits: list[Hit] = field(default_factory=list)


def _match_expected(fields: dict, expected: dict) -> list[str]:
    issues: list[str] = []
    for key, want in expected.items():
        # Word-overlap check: the record's own name/description words vs the
        # fetched record's name. Zero overlap of *salient* words (stopwords
        # stripped from both sides) means the accession is almost certainly the
        # wrong protein. This is how structured-record context is verified.
        if key == "name_words":
            got_name = str(fields.get("name", "")).lower()
            got_words = {w for w in re.findall(r"[a-z0-9]+", got_name)
                         if w not in _NAME_STOPWORDS and len(w) >= 3}
            want_words = {w.lower() for w in want
                          if w.lower() not in _NAME_STOPWORDS and len(w) >= 3}
            if want_words and not (want_words & got_words):
                issues.append(
                    f"name_words: none of {sorted(want_words)} found in fetched "
                    f"name '{fields.get('name', '')}'"
                )
            continue
        field_key = {
            "protein_name_contains": "name",
            "title_contains": "title",
            "organism_contains": "organism",
            "organism": "organism",
            "length_range": "length",
            "pfam_contains": "pfam",
            "year": "year",
        }.get(key, key)
        got = fields.get(field_key, "")

        if isinstance(want, str):
            if want.lower() not in str(got).lower():
                issues.append(
                    f"{key}: want substring '{want}' (case-insensitive), got '{got}'"
                )
        elif (
            isinstance(want, (list, tuple))
            and len(want) == 2
            and all(isinstance(x, (int, float)) for x in want)
            and key.endswith("_range")
        ):
            lo, hi = want
            try:
                got_n = float(got)
                if not lo <= got_n <= hi:
                    issues.append(f"{key}: want range [{lo}, {hi}], got {got_n}")
            except (ValueError, TypeError):
                issues.append(f"{key}: want range, got non-numeric '{got}'")
        elif isinstance(want, (list, tuple)):
            got_set = set(got) if isinstance(got, (list, tuple)) else {got}
            for w in want:
                if w not in got_set:
                    issues.append(
                        f"{key}: want '{w}' in {sorted(got_set)}, missing"
                    )
        elif isinstance(want, int):
            try:
                if int(got) != want:
                    issues.append(f"{key}: want {want}, got {got}")
            except (ValueError, TypeError):
                issues.append(f"{key}: want {want}, got non-int '{got}'")
        else:
            if got != want:
                issues.append(f"{key}: want {want!r}, got {got!r}")
    return issues


# Aliases for the type_name field in manifest entries. Lets callers use
# either the short internal name ("uniprot") or a longer descriptive name
# ("uniprot_accession") -- both map to the same spec.
TYPE_ALIASES: dict[str, str] = {
    "uniprot_accession": "uniprot",
    "ncbi_protein_accession": "ncbi_protein",
    "ncbi_nucleotide_accession": "ncbi_nucleotide",
    "ncbi_assembly_accession": "ncbi_assembly",
    "pfam_id": "pfam",
    "interpro_id": "interpro",
    "go_term": "go",
    "chebi_id": "chebi",
    "pubmed_id": "pubmed",
    "pmid": "pubmed",
    "arxiv_id": "arxiv",
    "preprint": "arxiv",
}


def verify_one(
    type_name: str, identifier: str, expected: dict | None = None
) -> Result:
    type_name = TYPE_ALIASES.get(type_name, type_name)
    spec = SPECS.get(type_name)
    if not spec:
        return Result(
            identifier=identifier,
            type_name=type_name,
            status="FETCH_FAIL",
            issues=[f"unknown identifier type: {type_name}"],
        )
    api_id = spec.normalize(identifier) if spec.normalize else identifier
    url = spec.url_template.format(id=api_id)
    fields: dict | None = None
    try:
        raw = _fetch_response(url, accept=spec.accept, parser=spec.parser)
        fields = spec.extractor(raw)
    except urllib.error.HTTPError as e:
        # Try fallback source if 404 and one is configured (e.g. DOI: CrossRef
        # → DataCite for Zenodo/Figshare/dataset DOIs).
        if (
            e.code == 404
            and spec.fallback_url_template
            and spec.fallback_extractor
        ):
            fb_url = spec.fallback_url_template.format(id=api_id)
            try:
                fb_raw = _fetch_response(fb_url, accept=spec.accept,
                                         parser=spec.parser)
                fields = spec.fallback_extractor(fb_raw)
                url = fb_url
            except urllib.error.HTTPError as e2:
                return Result(
                    identifier=identifier, type_name=type_name,
                    status="NOT_FOUND" if e2.code == 404 else f"HTTP_{e2.code}",
                    source_url=fb_url, issues=[str(e2)],
                )
            except Exception as e2:  # noqa: BLE001
                return Result(
                    identifier=identifier, type_name=type_name,
                    status="FETCH_FAIL", source_url=fb_url, issues=[str(e2)],
                )
        else:
            return Result(
                identifier=identifier, type_name=type_name,
                status="NOT_FOUND" if e.code == 404 else f"HTTP_{e.code}",
                source_url=url, issues=[str(e)],
            )
    except Exception as e:  # noqa: BLE001
        return Result(
            identifier=identifier, type_name=type_name,
            status="FETCH_FAIL", source_url=url, issues=[str(e)],
        )
    if not expected:
        return Result(
            identifier=identifier,
            type_name=type_name,
            status="EXISTS",
            fields=fields,
            source_url=url,
        )
    issues = _match_expected(fields, expected)
    return Result(
        identifier=identifier,
        type_name=type_name,
        status="VERIFIED" if not issues else "MISMATCH",
        fields=fields,
        issues=issues,
        source_url=url,
    )


# --- file filtering ------------------------------------------------------


SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".tox",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
    "logs",
}
SKIP_EXTS = {
    ".parquet", ".npy", ".npz", ".pkl", ".h5", ".hdf5",
    ".jpg", ".jpeg", ".png", ".pdf", ".gz", ".bz2", ".xz", ".zip",
    ".bin", ".onnx", ".pt", ".pth", ".safetensors",
    ".log", ".out", ".err",
}
SKIP_NAME_PATTERNS = [
    re.compile(r".*_analysis\.txt$"),
    re.compile(r".*_report\.txt$"),
    re.compile(r".*_diagnostic\.txt$"),
    re.compile(r"^.*\.lock$"),
]


def _iter_files(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() in SKIP_EXTS:
            continue
        if any(pat.match(p.name) for pat in SKIP_NAME_PATTERNS):
            continue
        try:
            if p.stat().st_size > 2_000_000:
                continue
        except OSError:
            continue
        yield p


# --- structural context detection ----------------------------------------
# Each detector returns (pattern_name, expected_dict) if it matches the
# identifier's position; otherwise (None, {}). Detectors are tried in order;
# first match wins.


# Common protein-name keyword vocabulary used to extract expectations
# from camelcase tokens like "ManducaApoLipIII".
NAME_KEYWORD_VOCAB = {
    "apolipophorin": ["ApoLip", "apolipoprotein"],
    "apolipoprotein": ["ApoA", "ApoB", "ApoE"],
    "chaperonin": ["GroEL", "GroES", "TRiC", "thermosome", "Cct", "TCP1"],
    "chaperone": ["DnaK", "DnaJ", "ClpB", "Hsp70", "Hsp90"],
    "luciferase": ["Luc"],
    "thioredoxin": ["TrxA", "Trx"],
    "maltose": ["MBP"],
    "fluorescent": ["GFP", "RFP", "YFP", "CFP", "mCherry"],
    "heat shock": ["HSP", "Hsp", "sHSP", "IbpA", "IbpB"],
    "sumo": ["SUMO", "Smt3"],
    "glutathione": ["GST"],
    "ubiquitin": ["Ub"],
    "saposin": ["Sap"],
}

_NAME_TOKEN_TO_KEYWORD = {
    tok.lower(): kw
    for kw, toks in NAME_KEYWORD_VOCAB.items()
    for tok in toks
}

# Common organism binomials -- used to spot organism names in tokens like
# "Manduca" appearing in camelcase labels.
COMMON_GENERA = {
    "manduca", "galleria", "drosophila", "homo", "escherichia", "saccharomyces",
    "thermoplasma", "methanocaldococcus", "wuchereria", "artemia", "oplophorus",
    "gaussia", "renilla", "photinus", "aequorea", "fasciola",
}


def _expected_from_camel_token(tok: str) -> dict:
    """Given a CamelCase token like 'ManducaApoLipIII', try to extract
    expected protein-name keyword and/or organism. Returns {} if nothing matches."""
    parts = re.findall(r"[A-Z][a-z]+|[A-Z]+(?=[A-Z]|$)|[a-z]+", tok)
    parts_lower = [p.lower() for p in parts]
    expected: dict = {}
    # organism: first part if it's a known genus
    if parts_lower and parts_lower[0] in COMMON_GENERA:
        expected["organism_contains"] = parts[0]
    # protein name keyword: longest matching substring in the rest
    for i in range(len(parts), 0, -1):
        candidate = "".join(parts[:i] if not expected else parts[1:])
        for tok_lower, keyword in _NAME_TOKEN_TO_KEYWORD.items():
            if tok_lower in candidate.lower():
                expected["protein_name_contains"] = keyword
                return expected
    return expected


def _detect_dict_key_with_acc(line: str, identifier: str) -> tuple[str | None, dict]:
    """Pattern: "ProteinName_ACCESSION": { ... in Python/JSON dict.
    The label preceding the underscore is mined for a protein-name keyword
    and/or organism via camelcase splitting."""
    m = re.search(
        rf'["\']([A-Za-z0-9]+)_{re.escape(identifier)}["\']\s*:',
        line,
    )
    if not m:
        return None, {}
    expected = _expected_from_camel_token(m.group(1))
    return ("dict_key", expected) if expected else (None, {})


def _detect_fasta_header(line: str, identifier: str) -> tuple[str | None, dict]:
    """Pattern: >SOMETHING|Name_ACC|description... -- typically POSITIVE_CONTROL or seed FASTA."""
    if not line.lstrip().startswith(">"):
        return None, {}
    if identifier not in line:
        return None, {}
    # Try to find "Label_<acc>" first
    m = re.search(rf'([A-Za-z0-9]+)_{re.escape(identifier)}', line)
    expected: dict = {}
    if m:
        expected.update(_expected_from_camel_token(m.group(1)))
    # Description text after second pipe
    fields = line.split("|")
    if len(fields) >= 3:
        desc = "|".join(fields[2:]).strip()
        # Mine description for a known organism binomial
        for genus in COMMON_GENERA:
            pat = rf"\b{genus}\s+([a-z]+)\b"
            mo = re.search(pat, desc, re.IGNORECASE)
            if mo:
                expected["organism_contains"] = genus.capitalize()
                break
        # Mine description for known protein keywords
        for keyword in NAME_KEYWORD_VOCAB:
            if keyword.lower() in desc.lower():
                expected["protein_name_contains"] = keyword
                break
    return ("fasta_header", expected) if expected else (None, {})


def _detect_parenthetical(line: str, identifier: str) -> tuple[str | None, dict]:
    """Pattern: 'ProteinName (ACCESSION)' -- each identifier has its own
    preceding label. Useful for prose like 'MBP (P0AEX9), NusA (P0A5Y6)'."""
    # Find the closest non-paren token immediately before `(<id>)`
    m = re.search(
        rf"([A-Za-z][A-Za-z0-9_\-]+)\s*\(\s*{re.escape(identifier)}\s*\)",
        line,
    )
    if not m:
        return None, {}
    label = m.group(1)
    # Try keyword match against the literal label
    expected: dict = {}
    for tok_lower, keyword in _NAME_TOKEN_TO_KEYWORD.items():
        if tok_lower in label.lower():
            expected["protein_name_contains"] = keyword
            break
    # If no keyword match, fall back to expecting the label literally
    if not expected:
        # Strip suffixes like "-III" / "1" for a softer match
        soft = re.sub(r"[-_]?[IVX]+$|\d+$", "", label).strip("_-")
        if soft and len(soft) >= 3:
            expected["protein_name_contains"] = soft
    return ("parenthetical", expected) if expected else (None, {})


def _detect_trailing_comment(line: str, identifier: str) -> tuple[str | None, dict]:
    """Pattern: <code with identifier> # protein description here"""
    if "#" not in line:
        return None, {}
    code, _, comment = line.partition("#")
    if identifier not in code:
        return None, {}
    expected: dict = {}
    # Look for organism binomial in the comment
    for genus in COMMON_GENERA:
        pat = rf"\b{genus}\s+([a-z]+)\b"
        mo = re.search(pat, comment, re.IGNORECASE)
        if mo:
            expected["organism_contains"] = genus.capitalize()
            break
    # Look for protein keyword in the comment
    for keyword in NAME_KEYWORD_VOCAB:
        if keyword.lower() in comment.lower():
            expected["protein_name_contains"] = keyword
            break
    return ("trailing_comment", expected) if expected else (None, {})


# Noise words stripped from both sides of a name-overlap comparison so that
# overlap is driven by salient protein terms, not generic boilerplate. Without
# this, "aerobactin receptor protein" vs "hypothetical protein" would falsely
# match on "protein".
_NAME_STOPWORDS = {
    "protein", "family", "domain", "containing", "putative", "subunit",
    "type", "system", "related", "like", "partial", "chain", "group",
    "class", "member", "associated", "the", "and", "from", "with", "for",
    "multispecies", "uncharacterized", "hypothetical", "probable", "putative",
}

# Field names (in dataclasses / dict-of-records) that state a protein's identity.
# The field whose value *contains* the accession is excluded as a name source.
_NAME_FIELD_KEYS = {
    "name", "product", "gene", "gene_name", "genename", "description", "desc",
    "label", "title", "protein_name", "proteinname", "annotation", "note",
}


def _enclosing_block(text: str, offset: int, max_span: int = 2000) -> str | None:
    """Return the smallest ``(...)`` or ``{...}`` block enclosing ``offset``.

    Lets a multi-line structured record (dataclass call, dict literal) be mined
    as one unit, so a ``name=``/``description=`` field is associated with the
    ``protein_id=`` field even though they sit on different lines. Bounded by
    bracket matching so it never bleeds into a neighbouring record.
    """
    start = None
    depth = 0
    i = min(offset, len(text) - 1)
    while i >= 0 and offset - i < max_span:
        c = text[i]
        if c in ")}":
            depth += 1
        elif c in "({":
            if depth == 0:
                start = i
                break
            depth -= 1
        i -= 1
    if start is None:
        return None
    open_ch = text[start]
    close_ch = ")" if open_ch == "(" else "}"
    depth = 0
    j = start
    while j < len(text) and j - start < max_span:
        c = text[j]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : j + 1]
        j += 1
    return None


_FIELD_ASSIGN_RE = re.compile(r"""(\w+)\s*[=:]\s*["']([^"']+)["']""")


def _detect_structured_record(block: str, identifier: str) -> tuple[str | None, dict]:
    """Pattern: a multi-line record where identity and accession are sibling
    fields, e.g. ``Anchor(name="...IutA", protein_id="WP_...", description="...")``.

    Mines the identity-bearing fields (name/product/gene/description/...) for the
    expected protein-name words, excluding the field that holds the accession.
    Returns a ``name_words`` expectation verified by word-overlap against the
    fetched record's name.
    """
    if identifier not in block:
        return None, {}
    name_text_parts: list[str] = []
    for m in _FIELD_ASSIGN_RE.finditer(block):
        field, value = m.group(1).lower(), m.group(2)
        if identifier in value:
            continue  # this is the accession field itself
        if field in _NAME_FIELD_KEYS:
            name_text_parts.append(value)
    if not name_text_parts:
        return None, {}
    words = {
        w for w in re.findall(r"[a-z0-9]+", " ".join(name_text_parts).lower())
        if len(w) >= 3 and w not in _NAME_STOPWORDS
    }
    if not words:
        return None, {}
    return "structured_record", {"name_words": sorted(words)}


STRUCTURAL_DETECTORS: list[Callable[[str, str], tuple[str | None, dict]]] = [
    _detect_fasta_header,   # most specific
    _detect_dict_key_with_acc,
    _detect_parenthetical,
    _detect_trailing_comment,
]


def _detect_context(
    line: str, identifier: str, block: str | None = None
) -> tuple[str, dict]:
    """Run single-line structural detectors on ``line``; if none match and a
    surrounding ``block`` is given, fall back to multi-line structured-record
    detection. Returns the first match or ('loose', {})."""
    for detector in STRUCTURAL_DETECTORS:
        name, expected = detector(line, identifier)
        if name and expected:
            return name, expected
    if block is not None:
        name, expected = _detect_structured_record(block, identifier)
        if name and expected:
            return name, expected
    return "loose", {}


# --- scan ----------------------------------------------------------------


def _scan_text(text: str) -> list[tuple[str, str, int, str, int]]:
    """Return list of (type_name, identifier, line_no, line_text, global_offset)."""
    found: list[tuple[str, str, int, str, int]] = []
    line_start = 0
    for line_no, line in enumerate(text.splitlines(), 1):
        for type_name, spec in SPECS.items():
            for m in re.finditer(spec.pattern, line):
                found.append(
                    (type_name, m.group(0), line_no, line, line_start + m.start())
                )
        line_start += len(line) + 1  # +1 for the stripped newline
    return found


def scan(target: Path, verbose: bool = False) -> list[Result]:
    # Step 1: collect all hits across files
    hits_by_id: dict[tuple[str, str], list[Hit]] = defaultdict(list)
    file_count = 0
    for f in _iter_files(target):
        file_count += 1
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for type_name, ident, line_no, line, offset in _scan_text(text):
            block = _enclosing_block(text, offset)
            pattern_name, expected = _detect_context(line, ident, block)
            spec = SPECS.get(type_name)
            dedup_key = spec.normalize(ident) if spec and spec.normalize else ident
            hits_by_id[(type_name, dedup_key)].append(
                Hit(
                    type_name=type_name,
                    identifier=ident,
                    file_path=f,
                    line_no=line_no,
                    line=line.strip(),
                    pattern_name=pattern_name,
                    expected=expected,
                )
            )

    # Step 2: pick the strongest expectation per identifier
    # (strongest = structural context with most expected fields)
    results: list[Result] = []
    for (type_name, ident), hits in sorted(hits_by_id.items()):
        # Prefer structural hits over loose ones; among structural, prefer
        # the one with the most expected fields.
        structural = [h for h in hits if h.pattern_name != "loose"]
        chosen = (
            max(structural, key=lambda h: len(h.expected))
            if structural
            else None
        )
        expected = chosen.expected if chosen else None
        if verbose:
            ctx = chosen.pattern_name if chosen else "loose"
            print(
                f"  {ident}  ({len(hits)} occurrences, context={ctx}, "
                f"expected={expected or 'none'})",
                file=sys.stderr,
                flush=True,
            )
        result = verify_one(type_name, ident, expected)
        result.hits = hits
        results.append(result)
        time.sleep(POLITENESS_SLEEP)

    print(
        f"\nscanned {file_count} files, found {len(results)} unique identifiers",
        file=sys.stderr,
    )
    return results


# --- manifest mode -------------------------------------------------------


def _load_manifest(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError:
            print(
                "ERROR: PyYAML not available; install with `pip install pyyaml` "
                "or pass a JSON manifest instead.",
                file=sys.stderr,
            )
            sys.exit(2)
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return data.get("references", [])


def verify_manifest(path: Path) -> list[Result]:
    refs = _load_manifest(path)
    results: list[Result] = []
    for ref in refs:
        type_name = ref.get("type", "")
        ident = ref.get("value", "")
        expected = ref.get("expected") or {}
        result = verify_one(type_name, ident, expected)
        results.append(result)
        time.sleep(POLITENESS_SLEEP)
    return results


# --- reporting -----------------------------------------------------------


def _color(status: str) -> str:
    return {
        "VERIFIED":   "\033[32m",
        "EXISTS":     "\033[36m",
        "MISMATCH":   "\033[31m",
        "NOT_FOUND":  "\033[31m",
        "FETCH_FAIL": "\033[33m",
    }.get(status, "")


def _results_to_dict(results: list[Result]) -> dict:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return {
        "summary": {
            "total": len(results),
            **{k.lower(): counts.get(k, 0) for k in
               ("VERIFIED", "EXISTS", "MISMATCH", "NOT_FOUND", "FETCH_FAIL")},
        },
        "results": [
            {
                "identifier": r.identifier,
                "type": r.type_name,
                "status": r.status,
                "source_url": r.source_url,
                "fields": r.fields,
                "issues": r.issues,
                "occurrences": [
                    {
                        "file": str(h.file_path),
                        "line": h.line_no,
                        "pattern": h.pattern_name,
                        "expected": h.expected,
                    }
                    for h in r.hits
                ],
            }
            for r in results
        ],
    }


def report_json(results: list[Result]) -> int:
    print(json.dumps(_results_to_dict(results), indent=2, default=str))
    if any(r.status in ("MISMATCH", "NOT_FOUND") for r in results):
        return 1
    if any(r.status == "FETCH_FAIL" for r in results):
        return 2
    return 0


def report(results: list[Result], group_by_file: bool = True) -> int:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    use_color = sys.stdout.isatty()
    reset = "\033[0m" if use_color else ""

    print()
    print("=" * 72)
    print("VERIFY-REFERENCES REPORT")
    print("=" * 72)
    print(f"\nTotal unique identifiers: {len(results)}")
    for status, n in sorted(counts.items()):
        c = _color(status) if use_color else ""
        print(f"  {c}{status}{reset}: {n}")

    # Problems (MISMATCH / NOT_FOUND / FETCH_FAIL)
    problems = [
        r for r in results if r.status not in ("VERIFIED", "EXISTS")
    ]

    if not problems:
        print("\nNo problems found.")
        return 0

    # Sort: severity (MISMATCH with multiple issues first, then single, then NOT_FOUND)
    def _sev(r: Result) -> tuple[int, int]:
        if r.status == "MISMATCH":
            return (0, -len(r.issues))
        if r.status == "NOT_FOUND":
            return (1, 0)
        return (2, 0)
    problems.sort(key=_sev)

    if group_by_file:
        print(f"\n--- Problems by file ({len(problems)} unique identifiers) ---")
        by_file: dict[Path, list[Result]] = defaultdict(list)
        for r in problems:
            # Use the first hit's file for grouping (most identifiers occur once)
            files = {h.file_path for h in r.hits} or {Path("<no-context>")}
            for f in files:
                by_file[f].append(r)
        for f in sorted(by_file):
            print(f"\n  {f}")
            for r in by_file[f]:
                c = _color(r.status) if use_color else ""
                print(f"    {c}[{r.status}]{reset} {r.type_name}={r.identifier}")
                if r.fields:
                    name = r.fields.get("name", "")
                    org = r.fields.get("organism", "")
                    if name or org:
                        print(f"      got: {name} ({org})")
                for issue in r.issues:
                    print(f"      - {issue}")
                if r.source_url:
                    print(f"      source: {r.source_url}")
    else:
        print(f"\n--- Problems ({len(problems)}) ---")
        for r in problems:
            c = _color(r.status) if use_color else ""
            print(f"\n  {c}[{r.status}]{reset} {r.type_name}={r.identifier}")
            for h in r.hits[:3]:
                print(f"    in {h.file_path}:{h.line_no} ({h.pattern_name})")
            if r.fields:
                print(f"    got: {r.fields}")
            for issue in r.issues:
                print(f"      - {issue}")
            if r.source_url:
                print(f"    source: {r.source_url}")

    if any(r.status == "MISMATCH" for r in results):
        print(
            "\nRECOMMENDATION: for MISMATCH entries, the identifier is valid but\n"
            "returns the WRONG record. Re-derive correct identifiers by searching\n"
            "the source by name + organism. DO NOT generate from memory."
        )

    if any(r.status in ("MISMATCH", "NOT_FOUND") for r in results):
        return 1
    if any(r.status == "FETCH_FAIL" for r in results):
        return 2
    return 0


# --- CLI -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print per-identifier progress to stderr")
    parser.add_argument("--no-group", action="store_true",
                        help="don't group problems by file in the report")
    parser.add_argument("--format", choices=("text", "json"), default="text",
                        help="output format (default: text). json is CI-friendly")
    parser.add_argument("--types", default=None,
                        help="comma-separated list of identifier types to scan "
                             f"(default: all). Available: {','.join(sorted(SPECS))}")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_scan = sub.add_parser("scan", help="Scan a path for identifiers")
    p_scan.add_argument("path", type=Path)

    p_verify = sub.add_parser("verify", help="Verify a manifest")
    p_verify.add_argument("manifest", type=Path)

    args = parser.parse_args()

    # In JSON mode, force-off verbose to avoid contaminating stdout if the
    # caller uses 2>&1. Verbose progress goes to stderr but mixing streams
    # via 2>&1 breaks JSON parsing downstream.
    if args.format == "json" and args.verbose:
        print(
            "verify-references: -v / --verbose suppressed in --format json mode "
            "(would break downstream JSON parsing if streams are merged).",
            file=sys.stderr,
        )
        args.verbose = False

    if args.types:
        wanted = {t.strip() for t in args.types.split(",")}
        unknown = wanted - set(SPECS)
        if unknown:
            print(f"ERROR: unknown identifier types: {sorted(unknown)}",
                  file=sys.stderr)
            return 2
        for k in list(SPECS):
            if k not in wanted:
                del SPECS[k]

    if args.mode == "scan":
        results = scan(args.path, verbose=args.verbose)
    else:
        results = verify_manifest(args.manifest)

    if args.format == "json":
        return report_json(results)
    return report(results, group_by_file=not args.no_group)


if __name__ == "__main__":
    sys.exit(main())
