from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, quote, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from xml.etree import ElementTree as ET
from html import unescape
from datetime import datetime, timezone
import re
import json

APP_VERSION = "1.0.0"
USER_AGENT = "MultiSourceResearchEngine/1.0 (local educational research app)"
VALID_SOURCES = {"openalex", "crossref", "arxiv", "pubmed", "wikipedia", "europepmc", "semanticscholar"}

STOPWORDS = set("a an the and or but if then so of to in on at by for from with without about into over under is are was were be been being do does did can could would should what which who whom where when why how find show tell give get make explain search research information data papers articles studies literature sources evidence all every latest best top related relevant that this these those it its their they them i we you my our your please help me want need looking explore discover investigate compare evaluate overview summarize summary on topic regarding around through across between during using used use".split())


def fetch_bytes(url, timeout=22, accept="application/json"):
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urlopen(req, timeout=timeout) as response:
            return response.read()
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(300).decode("utf-8", "replace")
        except Exception:
            pass
        if exc.code == 429:
            raise RuntimeError("Source rate limit reached. Wait a little and retry.")
        raise RuntimeError(f"Source returned HTTP {exc.code}. {detail[:160]}")
    except URLError as exc:
        raise RuntimeError(f"Network error: {getattr(exc, 'reason', exc)}")
    except TimeoutError:
        raise RuntimeError("Source request timed out.")


def fetch_json(base, params, timeout=22):
    url = base + ("&" if "?" in base else "?") + urlencode(params)
    return json.loads(fetch_bytes(url, timeout=timeout).decode("utf-8", "replace"))


def fetch_text(base, params, timeout=22, accept="application/atom+xml, application/xml, text/xml, */*"):
    url = base + ("&" if "?" in base else "?") + urlencode(params)
    return fetch_bytes(url, timeout=timeout, accept=accept).decode("utf-8", "replace")


def clean_html(text):
    text = re.sub(r"<br\s*/?>", " ", text or "", flags=re.I)
    text = re.sub(r"</(?:p|div|li|span)>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def get_text(el):
    if el is None:
        return ""
    return re.sub(r"\s+", " ", " ".join(el.itertext())).strip()


def year_of(value):
    if isinstance(value, int):
        return value
    m = re.search(r"\b(19\d{2}|20\d{2})\b", str(value or ""))
    return int(m.group(1)) if m else None


def normalize_query(question):
    q = re.sub(r"https?://\S+", " ", question or "")
    q = re.sub(r"[^\w\s\-./]", " ", q, flags=re.UNICODE)
    words = re.findall(r"[\w-]+", q.lower(), flags=re.UNICODE)
    kept = [w for w in words if w not in STOPWORDS and (len(w) > 2 or any(ch.isdigit() for ch in w))]
    if not kept:
        kept = words[:]
    return " ".join(kept[:18]) or (question or "").strip()


def make_item(title, url, source, **kwargs):
    title = re.sub(r"\s+", " ", str(title or "Untitled result")).strip()
    url = str(url or "")
    summary = re.sub(r"\s+", " ", str(kwargs.get("summary") or "")).strip()
    if len(summary) > 1100:
        summary = summary[:1097].rsplit(" ", 1)[0] + "…"
    return {
        "title": title,
        "url": url,
        "source": source,
        "year": year_of(kwargs.get("year")),
        "authors": kwargs.get("authors") or [],
        "venue": kwargs.get("venue") or "",
        "summary": summary,
        "doi": kwargs.get("doi") or "",
        "open_access": kwargs.get("open_access"),
        "kind": kwargs.get("kind") or "",
        "citation_count": kwargs.get("citation_count"),
    }


def search_openalex(q, limit, year_from=None, year_to=None):
    params = {"search": q, "per-page": limit, "sort": "relevance_score:desc",
              "select": "id,doi,title,publication_year,publication_date,authorships,primary_location,open_access,type,abstract_inverted_index,cited_by_count"}
    filters = []
    if year_from:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if year_to:
        filters.append(f"to_publication_date:{year_to}-12-31")
    if filters:
        params["filter"] = ",".join(filters)
    data = fetch_json("https://api.openalex.org/works", params)
    out = []
    for x in data.get("results", []):
        title = x.get("title") or "Untitled"
        doi = (x.get("doi") or "").replace("https://doi.org/", "")
        loc = x.get("primary_location") or {}
        source = (loc.get("source") or {}).get("display_name") or ""
        url = x.get("doi") or loc.get("landing_page_url") or x.get("id")
        auths = [((a.get("author") or {}).get("display_name") or "") for a in (x.get("authorships") or [])[:6]]
        abstract = ""
        inv = x.get("abstract_inverted_index") or {}
        if inv:
            terms = []
            for word, positions in inv.items():
                for pos in positions:
                    terms.append((pos, word))
            abstract = " ".join(w for _, w in sorted(terms))
        out.append(make_item(title, url, "OpenAlex", year=x.get("publication_year"), authors=auths, venue=source,
                             summary=abstract, doi=doi, open_access=(x.get("open_access") or {}).get("is_oa"),
                             kind=x.get("type"), citation_count=x.get("cited_by_count")))
    return out


def search_crossref(q, limit, year_from=None, year_to=None):
    params = {"query.bibliographic": q, "rows": limit,
              "select": "DOI,title,author,published,published-print,published-online,container-title,type,URL,abstract,score"}
    filters = []
    if year_from:
        filters.append(f"from-pub-date:{year_from}-01-01")
    if year_to:
        filters.append(f"until-pub-date:{year_to}-12-31")
    if filters:
        params["filter"] = ",".join(filters)
    data = fetch_json("https://api.crossref.org/works", params)
    out = []
    for x in ((data.get("message") or {}).get("items") or []):
        titles = x.get("title") or []
        dateparts = (((x.get("published") or {}).get("date-parts") or [[None]])[0])
        yr = dateparts[0] if dateparts else None
        authors = []
        for a in (x.get("author") or [])[:6]:
            name = " ".join(z for z in [a.get("given"), a.get("family")] if z)
            if name: authors.append(name)
        doi = x.get("DOI") or ""
        out.append(make_item(titles[0] if titles else "Untitled", x.get("URL") or ("https://doi.org/" + doi if doi else ""),
                             "Crossref", year=yr, authors=authors,
                             venue=(x.get("container-title") or [""])[0], summary=clean_html(x.get("abstract") or ""),
                             doi=doi, kind=x.get("type")))
    return out


def search_arxiv(q, limit, year_from=None, year_to=None):
    # arXiv's Atom API supports one expression per request; normalize syntax to avoid malformed advanced queries.
    query = re.sub(r"[^\w\s\-]", " ", q).strip()
    expr = "all:" + query
    if year_from or year_to:
        start = f"{year_from or 1900}01010000"
        end = f"{year_to or datetime.now(timezone.utc).year}12312359"
        expr = f"({expr}) AND submittedDate:[{start} TO {end}]"
    xml = fetch_text("https://export.arxiv.org/api/query", {"search_query": expr, "start": 0, "max_results": limit,
                       "sortBy": "relevance", "sortOrder": "descending"}, timeout=28)
    root = ET.fromstring(xml)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in root.findall("a:entry", ns):
        title = get_text(e.find("a:title", ns))
        summary = get_text(e.find("a:summary", ns))
        url = get_text(e.find("a:id", ns))
        published = get_text(e.find("a:published", ns))
        authors = [get_text(a.find("a:name", ns)) for a in e.findall("a:author", ns)[:6]]
        cats = [c.attrib.get("term", "") for c in e.findall("a:category", ns)[:3]]
        out.append(make_item(title, url, "arXiv", year=published, authors=authors,
                             venue=", ".join(cats), summary=summary, kind="preprint / research paper"))
    return out


def search_pubmed(q, limit, year_from=None, year_to=None):
    params = {"db": "pubmed", "term": q, "retmax": limit, "retmode": "json", "sort": "relevance"}
    if year_from or year_to:
        params.update({"datetype": "pdat", "mindate": f"{year_from or 1900}/01/01", "maxdate": f"{year_to or datetime.now(timezone.utc).year}/12/31"})
    data = fetch_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", params)
    ids = ((data.get("esearchresult") or {}).get("idlist") or [])
    if not ids:
        return []
    xml = fetch_text("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi", {"db": "pubmed", "id": ",".join(ids), "retmode": "xml"}, timeout=24, accept="application/xml, text/xml, */*")
    root = ET.fromstring(xml)
    out = []
    for article in root.findall(".//PubmedArticle"):
        title = get_text(article.find(".//ArticleTitle")) or "Untitled"
        pmid = get_text(article.find(".//PMID"))
        abstract_parts = [get_text(a) for a in article.findall(".//Abstract/AbstractText")]
        abstract = " ".join(p for p in abstract_parts if p)
        year = get_text(article.find(".//PubDate/Year"))
        if not year:
            year = get_text(article.find(".//PubDate/MedlineDate"))
        authors = []
        for a in article.findall(".//AuthorList/Author")[:6]:
            name = " ".join(z for z in [get_text(a.find("ForeName")), get_text(a.find("LastName"))] if z)
            if name: authors.append(name)
        journal = get_text(article.find(".//Journal/Title"))
        doi = ""
        for aid in article.findall(".//ArticleId"):
            if aid.attrib.get("IdType") == "doi":
                doi = get_text(aid)
                break
        url = "https://pubmed.ncbi.nlm.nih.gov/" + pmid + "/" if pmid else ("https://doi.org/" + doi if doi else "")
        out.append(make_item(title, url, "PubMed", year=year, authors=authors, venue=journal, summary=abstract, doi=doi, kind="biomedical literature"))
    return out


def search_wikipedia(q, limit, year_from=None, year_to=None):
    params = {"action": "query", "list": "search", "srsearch": q, "srlimit": limit,
              "srprop": "snippet|timestamp", "format": "json", "formatversion": 2, "origin": "*"}
    data = fetch_json("https://en.wikipedia.org/w/api.php", params)
    out = []
    for x in ((data.get("query") or {}).get("search") or []):
        timestamp = x.get("timestamp") or ""
        yr = year_of(timestamp)  # Last-edited year, not publication year.
        slug = quote(str(x.get("title", "")).replace(" ", "_"), safe="()'!,._-~")
        out.append(make_item(x.get("title"), "https://en.wikipedia.org/wiki/" + slug, "Wikipedia",
                             year=yr, summary=clean_html(x.get("snippet") or ""), kind="encyclopedia overview"))
    return out

def search_europe_pmc(q, limit, year_from=None, year_to=None):
    """Search Europe PMC's public article metadata and abstract endpoint."""
    parts = [q]
    if year_from:
        parts.append(f"FIRST_PDATE:[{year_from} TO {year_to or datetime.now(timezone.utc).year}]")
    elif year_to:
        parts.append(f"FIRST_PDATE:[1900 TO {year_to}]")
    params = {"query": " AND ".join(f"({p})" for p in parts), "format": "json", "pageSize": limit,
              "resultType": "core"}
    data = fetch_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search", params, timeout=24)
    out = []
    for x in ((data.get("resultList") or {}).get("result") or []):
        doi = x.get("doi") or ""
        pmid = x.get("pmid") or ""
        pmcid = x.get("pmcid") or ""
        url = (f"https://europepmc.org/articles/{pmcid}" if pmcid else
               f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else
               f"https://doi.org/{doi}" if doi else "https://europepmc.org/search?query=" + quote(q, safe=""))
        authors_raw = x.get("authorString") or ""
        authors = [a.strip() for a in re.split(r",\s*(?![^()]*\))", authors_raw) if a.strip()][:6]
        out.append(make_item(x.get("title"), url, "Europe PMC", year=x.get("firstPublicationDate") or x.get("pubYear"),
                             authors=authors, venue=x.get("journalTitle") or "", summary=clean_html(x.get("abstractText") or ""),
                             doi=doi, kind=x.get("pubType") or "biomedical / life-sciences literature",
                             open_access=(str(x.get("isOpenAccess") or "").upper() == "Y")))
    return out


def search_semantic_scholar(q, limit, year_from=None, year_to=None):
    """Search Semantic Scholar's public graph endpoint; availability is subject to provider limits."""
    params = {"query": q, "limit": limit,
              "fields": "paperId,title,year,authors,abstract,venue,externalIds,citationCount,url,openAccessPdf"}
    if year_from or year_to:
        params["year"] = f"{year_from or 1900}-{year_to or datetime.now(timezone.utc).year}"
    data = fetch_json("https://api.semanticscholar.org/graph/v1/paper/search", params, timeout=24)
    out = []
    for x in data.get("data") or []:
        ext = x.get("externalIds") or {}
        doi = ext.get("DOI") or ""
        paper_id = x.get("paperId") or ""
        url = x.get("url") or ("https://www.semanticscholar.org/paper/" + paper_id if paper_id else ("https://doi.org/" + doi if doi else ""))
        authors = [a.get("name", "") for a in (x.get("authors") or [])[:6] if a.get("name")]
        oa = x.get("openAccessPdf") or {}
        out.append(make_item(x.get("title"), url, "Semantic Scholar", year=x.get("year"), authors=authors,
                             venue=x.get("venue") or "", summary=x.get("abstract") or "", doi=doi,
                             citation_count=x.get("citationCount"), open_access=bool(oa.get("url")),
                             kind="scholarly paper"))
    return out


# Small, transparent vocabulary expansion rules. They help plan searches but do not
# claim to understand the topic as an LLM would. Variants are visible to the user.
_EXPANSION_RULES = [
    (("soil stabilization", "soil stabilisation", "ground improvement"), [
        ("soil improvement ground improvement geotechnical", "Terminology and adjacent engineering methods"),
        ("soil reinforcement sustainable binders field performance", "Performance and sustainable alternatives"),
    ]),
    (("low carbon concrete", "low-carbon concrete", "sustainable concrete", "cement alternative"), [
        ("low-carbon cement supplementary cementitious materials", "Alternative binders and cement replacement"),
        ("concrete embodied carbon life cycle assessment durability", "Lifecycle impacts and long-term performance"),
    ]),
    (("biochar",), [
        ("biochar soil amendment remediation field study", "Applications and field evidence"),
        ("biochar environmental impacts life cycle assessment", "Environmental performance and lifecycle"),
    ]),
    (("geopolymer", "alkali activated", "alkali-activated"), [
        ("geopolymer alkali-activated materials low carbon binder", "Related binder terminology"),
        ("geopolymer durability leaching life cycle assessment", "Durability and environmental risk"),
    ]),
    (("microplastic", "microplastics"), [
        ("microplastics wastewater removal treatment", "Treatment methods and wastewater applications"),
        ("microplastic detection remediation systematic review", "Detection and review literature"),
    ]),
    (("earthquake prediction", "earthquake forecasting"), [
        ("earthquake forecasting machine learning seismic hazard", "Related forecasting terminology"),
        ("earthquake prediction model validation reproducibility", "Validation and limitations"),
    ]),
    (("carbon capture", "direct air capture", "carbon removal"), [
        ("carbon dioxide removal direct air capture technologies", "Adjacent technology terminology"),
        ("carbon capture lifecycle assessment cost scale-up", "Cost, lifecycle, and scale-up"),
    ]),
    (("climate resilience", "climate resilient", "climate-resilient"), [
        ("climate adaptation resilience infrastructure risk assessment", "Related adaptation and risk terminology"),
        ("climate resilience lifecycle cost field evaluation", "Performance evaluation and lifecycle costs"),
    ]),
]


def plan_queries(question, core_query, depth="balanced"):
    depth = depth if depth in {"quick", "balanced", "deep"} else "balanced"
    expanded = []
    lower = (question or "").lower()
    for needles, rows in _EXPANSION_RULES:
        if any(n in lower for n in needles):
            expanded.extend(rows)
    # Generic fallback provides alternate research angles, rather than silently
    # pretending a thesaurus or AI query planner was used.
    if not expanded:
        expanded = [
            (core_query + " systematic review", "Look for synthesis and review literature"),
            (core_query + " evaluation limitations", "Look for evaluations, caveats, and limitations"),
        ]
    # Avoid redundant variants and keep URLs/query payloads bounded.
    seen = {core_query.lower()}
    options = [{"query": core_query, "label": "Core question", "reason": "Normalized from your question"}]
    for query, reason in expanded:
        query = re.sub(r"\s+", " ", query).strip()
        # Keep the user's core topic while appending an adjacent concept/angle.
        if core_query.lower() not in query.lower():
            query = core_query + " " + query
        query = re.sub(r"\s+", " ", query).strip()[:240]
        if query.lower() not in seen:
            seen.add(query.lower())
            options.append({"query": query, "label": "Expanded search", "reason": reason})
    count = {"quick": 1, "balanced": 2, "deep": 3}[depth]
    return {"depth": depth, "queries": options[:count],
            "note": "Expansion uses transparent topic rules or generic review/limitations angles. Review variants before interpreting result coverage."}


SOURCE_FUNCTIONS = {
    "openalex": ("OpenAlex", search_openalex),
    "crossref": ("Crossref", search_crossref),
    "arxiv": ("arXiv", search_arxiv),
    "pubmed": ("PubMed", search_pubmed),
    "wikipedia": ("Wikipedia", search_wikipedia),
    "europepmc": ("Europe PMC", search_europe_pmc),
    "semanticscholar": ("Semantic Scholar", search_semantic_scholar),
}


def deduplicate(all_items):
    merged = {}
    order = []
    for item in all_items:
        doi = (item.get("doi") or "").lower().strip().replace("https://doi.org/", "")
        title_key = re.sub(r"[^a-z0-9]", "", (item.get("title") or "").lower())
        key = "doi:" + doi if doi else "title:" + title_key if title_key else "url:" + item.get("url", "")
        if key not in merged:
            row = dict(item)
            row["sources"] = [item["source"]]
            row["source_count"] = 1
            row["matched_queries"] = list(dict.fromkeys(item.get("matched_queries") or ([item["matched_query"]] if item.get("matched_query") else [])))
            merged[key] = row
            order.append(key)
        else:
            row = merged[key]
            for matched in (item.get("matched_queries") or ([item["matched_query"]] if item.get("matched_query") else [])):
                if matched not in row.setdefault("matched_queries", []):
                    row["matched_queries"].append(matched)
            if item["source"] not in row["sources"]:
                row["sources"].append(item["source"])
                row["source_count"] = len(row["sources"])
            if len(item.get("summary") or "") > len(row.get("summary") or ""):
                row["summary"] = item["summary"]
            if not row.get("authors") and item.get("authors"):
                row["authors"] = item["authors"]
            if not row.get("year") and item.get("year"):
                row["year"] = item["year"]
            if not row.get("venue") and item.get("venue"):
                row["venue"] = item["venue"]
            if not row.get("doi") and item.get("doi"):
                row["doi"] = item["doi"]
            if item.get("open_access") is True:
                row["open_access"] = True
            if item.get("citation_count") and (not row.get("citation_count") or item["citation_count"] > row["citation_count"]):
                row["citation_count"] = item["citation_count"]
    results = [merged[k] for k in order]
    # Prefer richly described results, then newest; preserve no claim of cross-source relevance ranking.
    results.sort(key=lambda x: (bool(x.get("summary")), x.get("year") or 0, x.get("source_count") or 1), reverse=True)
    return results


def build_search_response(payload):
    question = str(payload.get("question", "")).strip()
    if not question:
        return {"error": "Enter a research question first."}, 400
    if len(question) > 500:
        return {"error": "Keep the question under 500 characters."}, 400
    source_keys = [s for s in (payload.get("sources") or list(SOURCE_FUNCTIONS.keys())) if s in VALID_SOURCES]
    if not source_keys:
        return {"error": "Choose at least one source."}, 400
    try:
        limit = max(3, min(15, int(payload.get("limit", 8))))
    except (ValueError, TypeError):
        limit = 8
    def valid_year(x):
        try:
            y = int(x)
            return y if 1900 <= y <= datetime.now(timezone.utc).year + 1 else None
        except (ValueError, TypeError):
            return None
    year_from, year_to = valid_year(payload.get("year_from")), valid_year(payload.get("year_to"))
    if year_from and year_to and year_from > year_to:
        return {"error": "The start year must be before or equal to the end year."}, 400
    core_query = normalize_query(question)
    research_plan = plan_queries(question, core_query, str(payload.get("depth", "balanced")))
    queries = research_plan["queries"]
    statuses = {}
    collected = []

    def run_source(key):
        label, fn = SOURCE_FUNCTIONS[key]
        source_items = []
        errors = []
        successful_queries = 0
        for qitem in queries:
            try:
                data = fn(qitem["query"], limit, year_from, year_to)
                successful_queries += 1
                for item in data:
                    item["matched_query"] = qitem["query"]
                    item["matched_queries"] = [qitem["query"]]
                    source_items.append(item)
            except Exception as exc:
                errors.append(str(exc)[:180])
        distinct = deduplicate(source_items)
        if successful_queries == 0:
            status = "error"
            message = errors[0] if errors else "Source did not return data."
        elif errors:
            status = "partial"
            message = f"{successful_queries}/{len(queries)} query variants succeeded. " + errors[0]
        else:
            status = "ok"
            message = ""
        return key, {"key": key, "name": label, "status": status, "count": len(distinct),
                     "queries_run": successful_queries, "queries_planned": len(queries), "message": message}, distinct

    with ThreadPoolExecutor(max_workers=min(7, len(source_keys))) as pool:
        futures = [pool.submit(run_source, key) for key in source_keys]
        for future in as_completed(futures):
            key, status, items = future.result()
            statuses[key] = status
            collected.extend(items)
    results = deduplicate(collected)
    q = question
    expanded = core_query
    external_searches = [
        {"label": "Google Web", "url": "https://www.google.com/search?q=" + quote(q, safe="")},
        {"label": "Google Scholar", "url": "https://scholar.google.com/scholar?q=" + quote(q, safe="")},
        {"label": "Semantic Scholar", "url": "https://www.semanticscholar.org/search?q=" + quote(q, safe="")},
        {"label": "Google Patents", "url": "https://patents.google.com/?q=" + quote(q, safe="")},
        {"label": "Wayback Machine", "url": "https://web.archive.org/"},
        {"label": "Google dork search", "url": "https://www.google.com/search?q=" + quote('"' + q + '" OR ' + expanded, safe="")},
    ]
    success_count = sum(1 for s in statuses.values() if s.get("status") in {"ok", "partial"})
    return ({
        "question": question, "query_used": core_query, "research_plan": research_plan, "results": results,
        "sources": [statuses.get(k, {"key": k, "name": SOURCE_FUNCTIONS[k][0], "status": "skipped", "count": 0}) for k in source_keys],
        "result_count": len(results), "raw_count": len(collected),
        "successful_sources": success_count,
        "external_searches": external_searches,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "note": "Results reflect only selected public source APIs and the query variants shown in the research plan. This is not an exhaustive search of the web. Source failures or zero results do not establish that no evidence exists."
    }, 200)


class ResearchHandler(BaseHTTPRequestHandler):
    server_version = "AtlasResearch/1.0"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        encoded = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            html_path = Path(__file__).resolve().parent / "templates" / "index.html"
            try:
                html = html_path.read_text(encoding="utf-8").replace("{{ version }}", APP_VERSION)
                self._send(200, html, "text/html; charset=utf-8")
            except OSError:
                self._send(500, "The interface template could not be loaded.", "text/plain; charset=utf-8")
        elif path == "/api/health":
            self._send(200, json.dumps({"ok": True, "version": APP_VERSION, "sources": [v[0] for v in SOURCE_FUNCTIONS.values()]}))
        else:
            self._send(404, json.dumps({"error": "Not found"}))

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/search":
            self._send(404, json.dumps({"error": "Not found"}))
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 24000:
                self._send(413, json.dumps({"error": "Request body must be under 24 KB."}))
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            result, status = build_search_response(payload)
            self._send(status, json.dumps(result, ensure_ascii=False))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._send(400, json.dumps({"error": "Invalid JSON request."}))
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._send(500, json.dumps({"error": "Unexpected server error: " + str(exc)[:180]}))


def main():
    host, port = "127.0.0.1", 5055
    print("\nATLAS RESEARCH — Multi-source research engine")
    print(f"Open http://{host}:{port} in your browser")
    print("This server is bound to localhost only. Press Ctrl+C to stop.\n")
    try:
        with ThreadingHTTPServer((host, port), ResearchHandler) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        print("\nAtlas Research stopped.")


if __name__ == "__main__":
    main()
