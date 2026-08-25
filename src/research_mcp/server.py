"""Web / academic / domain-data search MCP server.

Free, no-auth-required tools (best-effort rate limits). For sites that support
auth (HuggingFace), tokens are pulled from env vars passed in via mcp.json.

Tools:
  arxiv_search           — arXiv papers (CS, stat, q-bio, q-fin)
  semantic_scholar_search — cross-domain academic search with citations
  openalex_search        — open catalog of scholarly works
  pubmed_search          — biomedical literature (NCBI E-utilities)
  crossref_lookup        — DOI -> full metadata
  bioRxiv_search         — biology preprints
  hf_search_models       — HuggingFace models
  hf_search_datasets     — HuggingFace datasets
  uniprot_search         — protein sequences / function
  pdb_search             — protein structures
  ensembl_search         — genome / gene lookup
  fred_series            — Federal Reserve economic data series

All tools return structured JSON with at minimum: {title, url, ...}.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from mcp.server import MCPServer

# Strip inherited proxy env vars (a dead SOCKS proxy in the parent shell
# would break urllib/requests calls to the academic APIs).
for _k in list(os.environ):
    if _k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        del os.environ[_k]

LOG = logging.getLogger("research-mcp")
logging.basicConfig(level=logging.INFO, stream=__import__("sys").stderr)

UA = os.environ.get("RESEARCH_MCP_UA", "ds-agent-research-mcp/0.1 (mailto:you@example.com)")
HF_TOKEN = os.environ.get("HF_TOKEN", "")  # optional — hf_* work without it, but rate-limited

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
    "medline": "https://www.ncbi.nlm.nih.gov/entrez/eutils/1.0/",
}

server = MCPServer("research-mcp")


def _http_json(url: str, *, headers: dict | None = None, timeout: float = 20.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
    ctx = _ssl_ctx()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode())


def _http_text(url: str, *, headers: dict | None = None, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*", **(headers or {})})
    ctx = _ssl_ctx()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read().decode("utf-8", errors="replace")


def _ssl_ctx():
    """Build an SSL context that uses the system + sandbox CA bundle.

    Many sandboxed environments (and the user's own prod VPS) inject a
    self-signed CA via REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE. Without loading
    that bundle, urllib raises CERTIFICATE_VERIFY_FAILED on every call.
    """
    import ssl
    cafile = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE")
    if cafile and os.path.exists(cafile):
        ctx = ssl.create_default_context(cafile=cafile)
    else:
        ctx = ssl.create_default_context()
    return ctx


# ---------------- tool implementations ----------------

def arxiv_search(query: str, max_results: int = 10) -> list[dict]:
    """Search arXiv. `query` may include field qualifiers like au:Author, ti:Title, abs:Abstract."""
    q = urllib.parse.quote(query)
    url = f"http://export.arxiv.org/api/query?search_query=all:{q}&start=0&max_results={int(max_results)}&sortBy=relevance&sortOrder=descending"
    text = _http_text(url, timeout=20.0)
    root = ET.fromstring(text)
    out = []
    for e in root.findall("atom:entry", NS):
        title = (e.findtext("atom:title", default="", namespaces=NS) or "").strip().replace("\n", " ")
        summary = (e.findtext("atom:summary", default="", namespaces=NS) or "").strip()
        link = e.findtext("atom:id", default="", namespaces=NS)
        authors = [a.findtext("atom:name", default="", namespaces=NS) for a in e.findall("atom:author", NS)]
        published = e.findtext("atom:published", default="", namespaces=NS)
        cats = [c.attrib.get("term", "") for c in e.findall("atom:category", NS)]
        out.append({
            "title": title, "url": link, "summary": summary[:600],
            "authors": authors, "published": published, "categories": cats,
        })
    return out


def semantic_scholar_search(query: str, limit: int = 10, year_from: int | None = None) -> list[dict]:
    params = {"query": query, "limit": int(limit), "fields": "title,authors,year,abstract,url,citationCount,venue,externalIds"}
    if year_from:
        params["year"] = f"{year_from}-"
    url = "https://api.semanticscholar.org/graph/v1/paper/search?" + urllib.parse.urlencode(params)
    try:
        data = _http_json(url, timeout=20.0)
    except Exception as e:
        return [{"error": f"semantic_scholar failed: {e}"}]
    out = []
    for p in data.get("data", []) or []:
        out.append({
            "title": p.get("title"),
            "url": p.get("url"),
            "year": p.get("year"),
            "venue": p.get("venue"),
            "authors": [a.get("name") for a in (p.get("authors") or [])],
            "abstract": (p.get("abstract") or "")[:500],
            "citations": p.get("citationCount"),
            "doi": (p.get("externalIds") or {}).get("DOI"),
            "arxiv_id": (p.get("externalIds") or {}).get("ArXiv"),
        })
    return out


def openalex_search(query: str, limit: int = 10, year_from: int | None = None) -> list[dict]:
    params = {"search": query, "per_page": int(limit)}
    if year_from:
        params["filter"] = f"publication_year:>{year_from-1}"
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    try:
        data = _http_json(url, timeout=20.0)
    except Exception as e:
        return [{"error": f"openalex failed: {e}"}]
    out = []
    for w in data.get("results", []):
        out.append({
            "title": w.get("title"),
            "url": w.get("doi") or w.get("id"),
            "year": w.get("publication_year"),
            "venue": (w.get("primary_location") or {}).get("source", {}).get("display_name") if w.get("primary_location") else None,
            "authors": [a.get("author", {}).get("display_name") for a in (w.get("authorships") or [])],
            "cited_by_count": w.get("cited_by_count"),
            "doi": w.get("doi"),
            "open_access": (w.get("open_access") or {}).get("is_oa"),
        })
    return out


def pubmed_search(term: str, max_results: int = 10) -> list[dict]:
    """NCBI E-utilities: esearch + esummary."""
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    # esearch
    url = f"{base}/esearch.fcgi?db=pubmed&retmax={int(max_results)}&retmode=json&term={urllib.parse.quote(term)}"
    try:
        es = _http_json(url, timeout=15.0)
    except Exception as e:
        return [{"error": f"pubmed esearch failed: {e}"}]
    ids = es.get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []
    # esummary
    s = _http_json(f"{base}/esummary.fcgi?db=pubmed&id={','.join(ids)}&retmode=json", timeout=15.0)
    out = []
    result = s.get("result", {})
    for pid in ids:
        d = result.get(pid, {})
        if not d:
            continue
        authors = [a.get("name") for a in d.get("authors", [])]
        out.append({
            "pmid": pid,
            "title": d.get("title"),
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
            "journal": d.get("fulljournalname") or d.get("source"),
            "year": (d.get("pubdate") or "").split(" ")[0],
            "authors": authors[:10],
            "doi": next((aid["value"] for aid in d.get("articleids", []) if aid.get("idtype") == "doi"), None),
        })
    return out


def crossref_lookup(doi: str) -> dict:
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi)}"
    try:
        d = _http_json(url, timeout=15.0).get("message", {})
    except Exception as e:
        return {"error": f"crossref failed: {e}"}
    return {
        "doi": d.get("DOI"),
        "title": (d.get("title") or [None])[0],
        "url": d.get("URL"),
        "year": (d.get("published", {}).get("date-parts") or [[None]])[0][0],
        "authors": [f"{a.get('given','')} {a.get('family','')}".strip() for a in (d.get("author") or [])],
        "journal": d.get("container-title", [None])[0] if d.get("container-title") else None,
        "abstract": (d.get("abstract") or "")[:800],
        "type": d.get("type"),
        "citation_count": d.get("is-referenced-by-count"),
    }


def bioRxiv_search(query: str, max_results: int = 10) -> list[dict]:
    # bioRxiv's "text" endpoint is unofficially usable
    url = f"https://api.biorxiv.org/details/biorxiv/{urllib.parse.quote(query)}/0?num_results={int(max_results)}"
    try:
        d = _http_json(url, timeout=15.0)
    except Exception as e:
        return [{"error": f"biorxiv failed: {e}"}]
    out = []
    for c in d.get("collection", []) or []:
        out.append({
            "title": c.get("title"),
            "doi": c.get("doi"),
            "url": f"https://www.biorxiv.org/content/{c.get('doi')}v{c.get('version','1')}",
            "authors": (c.get("authors") or "").split(";")[:10],
            "date": c.get("date"),
            "category": c.get("category"),
            "abstract": (c.get("abstract") or "")[:600],
        })
    return out


def hf_search_models(query: str, limit: int = 10) -> list[dict]:
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
    url = f"https://huggingface.co/api/models?search={urllib.parse.quote(query)}&limit={int(limit)}&full=false&config=true"
    try:
        items = _http_json(url, headers=headers, timeout=15.0)
    except Exception as e:
        return [{"error": f"hf models failed: {e}"}]
    out = []
    for m in items or []:
        out.append({
            "id": m.get("id") or m.get("modelId"),
            "url": f"https://huggingface.co/{m.get('id') or m.get('modelId')}",
            "downloads": m.get("downloads"),
            "likes": m.get("likes"),
            "tags": (m.get("tags") or [])[:8],
            "pipeline_tag": m.get("pipeline_tag"),
            "library": m.get("library_name"),
        })
    return out


def hf_search_datasets(query: str, limit: int = 10) -> list[dict]:
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}
    url = f"https://huggingface.co/api/datasets?search={urllib.parse.quote(query)}&limit={int(limit)}"
    try:
        items = _http_json(url, headers=headers, timeout=15.0)
    except Exception as e:
        return [{"error": f"hf datasets failed: {e}"}]
    return [{
        "id": d.get("id"),
        "url": f"https://huggingface.co/datasets/{d.get('id')}",
        "downloads": d.get("downloads"),
        "likes": d.get("likes"),
        "tags": (d.get("tags") or [])[:8],
    } for d in items or []]


def uniprot_search(query: str, limit: int = 10) -> list[dict]:
    # UniProt search (new REST API, no auth)
    url = f"https://rest.uniprot.org/uniprotkb/search?format=json&size={int(limit)}&query={urllib.parse.quote(query)}"
    try:
        d = _http_json(url, timeout=15.0)
    except Exception as e:
        return [{"error": f"uniprot failed: {e}"}]
    out = []
    for e in d.get("results", []) or []:
        acc = e.get("primaryAccession")
        desc = (e.get("proteinDescription") or {}).get("recommendedName", {}).get("fullName", {}).get("value", "")
        out.append({
            "accession": acc,
            "name": desc,
            "url": f"https://www.uniprot.org/uniprotkb/{acc}/entry",
            "organism": (e.get("organism") or {}).get("scientificName"),
            "length": e.get("sequence", {}).get("length"),
            "reviewed": e.get("entryType") == "UniProtKB reviewed (Swiss-Prot)",
        })
    return out


def pdb_search(query: str, limit: int = 10) -> list[dict]:
    # RCSB PDB search API
    payload = {
        "query": {"type": "terminal", "service": "full_text", "parameters": {"value": query}},
        "return_type": "entry",
        "request_options": {"paginate": {"start": 0, "rows": int(limit)}},
    }
    try:
        d = _http_json("https://search.rcsb.org/rcsbsearch/v2/query", timeout=15.0)
    except Exception as e:
        return [{"error": f"pdb failed: {e}"}]
    # the real API needs POST JSON; we use the GET form
    url = f"https://search.rcsb.org/rcsbsearch/v2/query?json={urllib.parse.quote(json.dumps(payload))}"
    try:
        d = _http_json(url, timeout=15.0)
    except Exception as e:
        return [{"error": f"pdb failed: {e}"}]
    ids = [h.get("identifier") for h in (d.get("result_set") or [])]
    return [{"pdb_id": pid, "url": f"https://www.rcsb.org/structure/{pid}"} for pid in ids]


def ensembl_search(gene: str, species: str = "human") -> list[dict]:
    url = f"https://rest.ensembl.org/lookup/symbol/{urllib.parse.quote(species)}/{urllib.parse.quote(gene)}?expand=0"
    try:
        d = _http_json(url, headers={"Accept": "application/json"}, timeout=15.0)
    except Exception as e:
        return [{"error": f"ensembl failed: {e}"}]
    if isinstance(d, dict) and "error" in d:
        return [d]
    return [{
        "id": d.get("id"),
        "biotype": d.get("biotype"),
        "description": d.get("description"),
        "chromosome": d.get("seq_region_name"),
        "start": d.get("start"), "end": d.get("end"),
        "strand": d.get("strand"),
        "url": f"https://www.ensembl.org/{species}/Gene/Summary?g={d.get('id')}",
    }]


def fred_series(series_id: str, limit: int = 10) -> list[dict]:
    """FRED observation fetch — no API key needed for small requests, but ideally pass
    FRED_API_KEY env (free at https://fred.stlouisfed.org/docs/api/api_key.html)."""
    key = os.environ.get("FRED_API_KEY", "")
    if not key:
        return [{"error": "FRED_API_KEY env not set. Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html"}]
    url = f"https://api.stlouisfed.org/fred/series/observations?series_id={urllib.parse.quote(series_id)}&file_type=json&limit={int(limit)}&api_key={key}"
    try:
        d = _http_json(url, timeout=15.0)
    except Exception as e:
        return [{"error": f"fred failed: {e}"}]
    return d.get("observations", [])


# ---------------- MCP tools (2.x API) ----------------

@server.tool(name="arxiv_search")
def arxiv_search_tool(query: str, max_results: int = 10) -> str:
    """Search arXiv. Field qualifiers: au:Author, ti:Title, abs:Abstract."""
    return json.dumps(arxiv_search(query, max_results), indent=2, default=str)


@server.tool(name="semantic_scholar_search")
def semantic_scholar_search_tool(query: str, limit: int = 10, year_from: int | None = None) -> str:
    """Cross-domain academic search with citations (Semantic Scholar)."""
    return json.dumps(semantic_scholar_search(query, limit, year_from), indent=2, default=str)


@server.tool(name="openalex_search")
def openalex_search_tool(query: str, limit: int = 10, year_from: int | None = None) -> str:
    """OpenAlex scholarly works search (no auth, no rate limit for personal use)."""
    return json.dumps(openalex_search(query, limit, year_from), indent=2, default=str)


@server.tool(name="pubmed_search")
def pubmed_search_tool(term: str, max_results: int = 10) -> str:
    """PubMed biomedical literature search (NCBI E-utilities)."""
    return json.dumps(pubmed_search(term, max_results), indent=2, default=str)


@server.tool(name="crossref_lookup")
def crossref_lookup_tool(doi: str) -> str:
    """Look up a paper by DOI via CrossRef."""
    return json.dumps(crossref_lookup(doi), indent=2, default=str)


@server.tool(name="biorxiv_search")
def biorxiv_search_tool(query: str, max_results: int = 10) -> str:
    """Search bioRxiv preprints."""
    return json.dumps(bioRxiv_search(query, max_results), indent=2, default=str)


@server.tool(name="hf_search_models")
def hf_search_models_tool(query: str, limit: int = 10) -> str:
    """HuggingFace model search. Set HF_TOKEN env for higher rate limits."""
    return json.dumps(hf_search_models(query, limit), indent=2, default=str)


@server.tool(name="hf_search_datasets")
def hf_search_datasets_tool(query: str, limit: int = 10) -> str:
    """HuggingFace dataset search."""
    return json.dumps(hf_search_datasets(query, limit), indent=2, default=str)


@server.tool(name="uniprot_search")
def uniprot_search_tool(query: str, limit: int = 10) -> str:
    """UniProt protein search (Swiss-Prot + TrEMBL)."""
    return json.dumps(uniprot_search(query, limit), indent=2, default=str)


@server.tool(name="pdb_search")
def pdb_search_tool(query: str, limit: int = 10) -> str:
    """RCSB PDB protein structure search."""
    return json.dumps(pdb_search(query, limit), indent=2, default=str)


@server.tool(name="ensembl_search")
def ensembl_search_tool(gene: str, species: str = "human") -> str:
    """Look up a gene by symbol in Ensembl (default: human)."""
    return json.dumps(ensembl_search(gene, species), indent=2, default=str)


@server.tool(name="fred_series")
def fred_series_tool(series_id: str, limit: int = 10) -> str:
    """Fetch FRED economic time-series observations. Requires FRED_API_KEY env (free)."""
    return json.dumps(fred_series(series_id, limit), indent=2, default=str)


def main() -> None:
    import asyncio
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
