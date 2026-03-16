"""
Battery Research Weekly Newsletter Bot
Fetches real papers from OpenAlex + Semantic Scholar,
summarises them with Claude, and emails the digest.
"""

import os
import json
import smtplib
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── Configuration (loaded from environment variables) ──────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_ADDRESS     = os.environ["GMAIL_ADDRESS"]        # your Gmail address
GMAIL_APP_PASSWORD= os.environ["GMAIL_APP_PASSWORD"]   # Gmail App Password (not your login password)
RECIPIENT_EMAIL   = os.environ.get("RECIPIENT_EMAIL", GMAIL_ADDRESS)
PURDUE_PROXY      = "https://proxy.lib.purdue.edu/login?url="  # Purdue library proxy prefix

# ── Research profile ────────────────────────────────────────────────────────────
RESEARCH_FOCUS = """
PhD research on silicon-graphite composite anodes and how they drive cathode degradation.
Key topics: SEI formation and evolution on Si particles, volume expansion effects,
lithium plating on graphite, cross-talk degradation mechanisms, electrolyte decomposition
products that accelerate NMC/NCA/LFP cathode aging, capacity fade in Si-graphite//NMC full cells.
"""

SEARCH_QUERIES = [
    "silicon graphite anode cathode degradation",
    "NMC degradation silicon anode full cell",
    "SEI silicon anode electrolyte decomposition",
    "lithium plating graphite silicon composite",
    "NCA capacity fade silicon graphite",
    "LFP silicon anode cross-talk",
    "volume expansion silicon cathode stress",
    "electrolyte decomposition battery cathode",
]

PURDUE_PROXY_JOURNALS = [
    "Journal of The Electrochemical Society",
    "Advanced Energy Materials",
    "ACS Energy Letters",
    "Journal of Power Sources",
    "Electrochimica Acta",
    "Chemistry of Materials",
    "Energy & Environmental Science",
    "Joule",
    "Nature Energy",
    "Small",
]

# ── HTTP helper ─────────────────────────────────────────────────────────────────
def http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {
        "User-Agent": "BatteryResearchBot/1.0 (academic research; mailto:research@example.com)"
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  HTTP error for {url[:80]}: {e}")
        return None

def http_post(url, payload, headers):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

# ── Paper fetching ──────────────────────────────────────────────────────────────
def fetch_openalex(query, days_back=7):
    """Fetch recent papers from OpenAlex (free, no key needed)."""
    since = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    params = urllib.parse.urlencode({
        "search": query,
        "filter": f"from_publication_date:{since},type:article",
        "per-page": 8,
        "select": "id,doi,title,authorships,publication_year,primary_location,abstract_inverted_index,open_access",
        "mailto": "research@example.com",
    })
    url = f"https://api.openalex.org/works?{params}"
    data = http_get(url)
    if not data:
        return []

    papers = []
    for w in data.get("results", []):
        title = w.get("title", "")
        if not title:
            continue
        doi = w.get("doi", "") or ""
        doi_clean = doi.replace("https://doi.org/", "") if doi else ""

        # Reconstruct abstract from inverted index
        abstract = ""
        inv = w.get("abstract_inverted_index") or {}
        if inv:
            word_positions = []
            for word, positions in inv.items():
                for pos in positions:
                    word_positions.append((pos, word))
            word_positions.sort()
            abstract = " ".join(w for _, w in word_positions)

        authors = []
        for a in (w.get("authorships") or [])[:3]:
            name = (a.get("author") or {}).get("display_name", "")
            if name:
                authors.append(name)
        author_str = ", ".join(authors) + (" et al." if len(w.get("authorships", [])) > 3 else "")

        journal = ""
        loc = w.get("primary_location") or {}
        src = loc.get("source") or {}
        journal = src.get("display_name", "")

        oa_url = (loc.get("pdf_url") or loc.get("landing_page_url") or "")

        papers.append({
            "title": title,
            "authors": author_str,
            "journal": journal,
            "year": w.get("publication_year", datetime.utcnow().year),
            "doi": doi_clean,
            "abstract": abstract[:1200],
            "oa_url": oa_url,
            "source": "OpenAlex",
        })
    return papers


def fetch_semantic_scholar(query, days_back=7):
    """Fetch from Semantic Scholar API."""
    params = urllib.parse.urlencode({
        "query": query,
        "fields": "title,authors,year,abstract,externalIds,publicationVenue,openAccessPdf",
        "limit": 6,
        "publicationDateOrYear": f"{(datetime.utcnow()-timedelta(days=days_back)).strftime('%Y-%m-%d')}:",
    })
    url = f"https://api.semanticscholar.org/graph/v1/paper/search?{params}"
    data = http_get(url, headers={
        "User-Agent": "BatteryResearchBot/1.0",
    })
    if not data:
        return []

    papers = []
    for p in data.get("data", []):
        title = p.get("title", "")
        if not title:
            continue
        doi = (p.get("externalIds") or {}).get("DOI", "")
        authors = [a.get("name", "") for a in (p.get("authors") or [])[:3]]
        author_str = ", ".join(a for a in authors if a)
        if len(p.get("authors", [])) > 3:
            author_str += " et al."
        journal = (p.get("publicationVenue") or {}).get("name", "")
        oa = (p.get("openAccessPdf") or {}).get("url", "")
        abstract = p.get("abstract") or ""

        papers.append({
            "title": title,
            "authors": author_str,
            "journal": journal,
            "year": p.get("year", datetime.utcnow().year),
            "doi": doi,
            "abstract": abstract[:1200],
            "oa_url": oa,
            "source": "Semantic Scholar",
        })
    return papers


def deduplicate(papers):
    """Remove duplicates by title similarity and DOI."""
    seen_dois, seen_titles, unique = set(), set(), []
    for p in papers:
        doi = p.get("doi", "").strip().lower()
        title_key = p.get("title", "").lower()[:60]
        if doi and doi in seen_dois:
            continue
        if title_key in seen_titles:
            continue
        if doi:
            seen_dois.add(doi)
        seen_titles.add(title_key)
        unique.append(p)
    return unique


def collect_papers(days_back=7, max_papers=20):
    """Run all queries and return deduplicated paper list."""
    print("Fetching papers from OpenAlex and Semantic Scholar...")
    all_papers = []
    for i, q in enumerate(SEARCH_QUERIES):
        print(f"  [{i+1}/{len(SEARCH_QUERIES)}] {q}")
        all_papers += fetch_openalex(q, days_back)
        all_papers += fetch_semantic_scholar(q, days_back)

    all_papers = deduplicate(all_papers)
    # Filter to papers that have at least a title and abstract
    all_papers = [p for p in all_papers if p.get("abstract") and len(p["abstract"]) > 80]
    print(f"  Found {len(all_papers)} unique papers with abstracts.")
    return all_papers[:max_papers]


# ── Claude API ──────────────────────────────────────────────────────────────────
def claude(messages, system="", max_tokens=4096):
    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    resp = http_post(
        "https://api.anthropic.com/v1/messages",
        payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    return resp["content"][0]["text"]


def score_and_summarise(papers):
    """Ask Claude to score relevance and write summaries for each paper."""
    print("Scoring and summarising with Claude...")
    paper_list = json.dumps([{
        "id": i,
        "title": p["title"],
        "journal": p["journal"],
        "abstract": p["abstract"],
    } for i, p in enumerate(papers)], indent=2)

    system = "You are an expert battery scientist helping a PhD student track the literature."
    prompt = f"""Research focus:
{RESEARCH_FOCUS}

Below are {len(papers)} recent papers. For each, return ONLY a JSON array (no markdown) where each object has:
  "id": same integer as input,
  "relevance_score": integer 0-100 (how directly relevant to the research focus),
  "summary": "2-3 sentence plain-English summary of key findings",
  "connection": "1-2 sentences on exactly how this relates to silicon-graphite anodes and cathode degradation"

Return only the JSON array.

Papers:
{paper_list}"""

    raw = claude([{"role": "user", "content": prompt}], system=system, max_tokens=4000)
    raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    scored = json.loads(raw)

    score_map = {s["id"]: s for s in scored}
    for i, p in enumerate(papers):
        s = score_map.get(i, {})
        p["relevance_score"] = s.get("relevance_score", 0)
        p["summary"] = s.get("summary", p["abstract"][:300])
        p["connection"] = s.get("connection", "")

    papers = [p for p in papers if p.get("relevance_score", 0) >= 50]
    papers.sort(key=lambda x: x["relevance_score"], reverse=True)
    print(f"  {len(papers)} papers passed relevance threshold (>=50).")
    return papers[:12]


def write_executive_summary(papers):
    """Ask Claude to write the weekly executive summary."""
    titles_and_summaries = "\n".join(
        f"- {p['title']}: {p['summary']}" for p in papers[:8]
    )
    prompt = f"""Research focus:
{RESEARCH_FOCUS}

This week's top papers:
{titles_and_summaries}

Write a 4-5 sentence executive summary of the most important findings this week and what they collectively mean for a PhD student studying cathode degradation in silicon-graphite full cells. Be specific, not generic."""
    return claude([{"role": "user", "content": prompt}], max_tokens=500)


# ── Email building ──────────────────────────────────────────────────────────────
def purdue_link(paper):
    """Return the best available link, proxied through Purdue if it's a subscription journal."""
    if paper.get("oa_url"):
        return paper["oa_url"]
    if paper.get("doi"):
        raw = f"https://doi.org/{paper['doi']}"
        if any(j.lower() in paper.get("journal", "").lower() for j in PURDUE_PROXY_JOURNALS):
            return PURDUE_PROXY + urllib.parse.quote(raw, safe="")
        return raw
    return ""


def score_bar(score):
    filled = round(score / 10)
    return "█" * filled + "░" * (10 - filled)


def build_html_email(papers, exec_summary):
    date_str = datetime.utcnow().strftime("%B %d, %Y")
    paper_html = ""
    for p in papers:
        link = purdue_link(p)
        link_html = f'<a href="{link}" style="color:#0F6E56;font-size:12px;">Read paper →</a>' if link else ""
        paper_html += f"""
        <div style="background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:16px;margin-bottom:14px;">
          <div style="background:#E1F5EE;color:#085041;font-size:11px;padding:3px 8px;border-radius:20px;display:inline-block;margin-bottom:8px;">
            {p.get('journal','Unknown journal')} · {p.get('year','')}
          </div>
          <h3 style="margin:0 0 6px;font-size:15px;font-weight:600;color:#1a1a1a;line-height:1.4;">
            {p['title']}
          </h3>
          <p style="margin:0 0 8px;font-size:12px;color:#666;">{p.get('authors','')}</p>
          <p style="margin:0 0 8px;font-size:13px;color:#333;line-height:1.6;">{p.get('summary','')}</p>
          <div style="background:#E1F5EE;border-left:3px solid #1D9E75;padding:8px 12px;border-radius:4px;margin-top:8px;">
            <strong style="font-size:12px;color:#085041;">Relevance to your work ({p.get('relevance_score',0)}/100):</strong>
            <span style="font-size:12px;color:#085041;margin-left:4px;">{score_bar(p.get('relevance_score',0))}</span><br>
            <span style="font-size:12px;color:#085041;">{p.get('connection','')}</span>
          </div>
          <div style="margin-top:10px;">{link_html}</div>
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:24px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

        <!-- Header -->
        <tr><td style="background:#085041;border-radius:10px 10px 0 0;padding:24px 28px;">
          <h1 style="margin:0;font-size:20px;font-weight:600;color:#fff;">Weekly Research Digest</h1>
          <p style="margin:4px 0 0;font-size:13px;color:#9FE1CB;">Si-Graphite Anodes & Cathode Degradation · {date_str}</p>
        </td></tr>

        <!-- Exec summary -->
        <tr><td style="background:#1D9E75;padding:16px 28px;">
          <p style="margin:0;font-size:13px;color:#fff;line-height:1.7;">{exec_summary}</p>
        </td></tr>

        <!-- Stats bar -->
        <tr><td style="background:#fff;padding:12px 28px;border-bottom:1px solid #eee;">
          <p style="margin:0;font-size:12px;color:#888;">{len(papers)} relevant papers this week · Sourced from OpenAlex & Semantic Scholar · Summarised by Claude</p>
        </td></tr>

        <!-- Papers -->
        <tr><td style="background:#f5f5f5;padding:16px 28px;">
          {paper_html}
        </td></tr>

        <!-- Footer -->
        <tr><td style="background:#fff;border-radius:0 0 10px 10px;padding:16px 28px;border-top:1px solid #eee;">
          <p style="margin:0;font-size:11px;color:#aaa;">Generated automatically · Papers sourced from OpenAlex & Semantic Scholar (open access) · Full-text links routed via Purdue University library proxy where available.</p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def build_plain_text(papers, exec_summary):
    date_str = datetime.utcnow().strftime("%B %d, %Y")
    lines = [
        f"WEEKLY RESEARCH DIGEST — {date_str}",
        "Si-Graphite Anodes & Cathode Degradation",
        "=" * 52,
        "",
        "EXECUTIVE SUMMARY",
        exec_summary,
        "",
        "=" * 52,
        f"TOP {len(papers)} PAPERS THIS WEEK",
        "",
    ]
    for i, p in enumerate(papers, 1):
        link = purdue_link(p)
        lines += [
            f"{i}. {p['title']}",
            f"   {p.get('authors','')} | {p.get('journal','')} ({p.get('year','')})",
            f"   Relevance: {p.get('relevance_score',0)}/100  {score_bar(p.get('relevance_score',0))}",
            f"   Summary: {p.get('summary','')}",
            f"   → Your work: {p.get('connection','')}",
            f"   Link: {link}" if link else "",
            "",
        ]
    return "\n".join(lines)


# ── Email sending ───────────────────────────────────────────────────────────────
def send_email(html_body, plain_body, paper_count):
    date_str = datetime.utcnow().strftime("%b %d")
    subject = f"Research Digest {date_str} — {paper_count} new papers on Si-graphite & cathode degradation"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT_EMAIL
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    print(f"Sending email to {RECIPIENT_EMAIL}...")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())
    print("Email sent successfully!")


# ── Main ────────────────────────────────────────────────────────────────────────
def main():
    print("=" * 52)
    print("Battery Research Newsletter Bot")
    print(f"Running at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 52)

    papers = collect_papers(days_back=7, max_papers=25)

    if not papers:
        print("No papers found this week — skipping email.")
        return

    papers = score_and_summarise(papers)

    if not papers:
        print("No relevant papers this week — skipping email.")
        return

    exec_summary = write_executive_summary(papers)
    html_body = build_html_email(papers, exec_summary)
    plain_body = build_plain_text(papers, exec_summary)
    send_email(html_body, plain_body, len(papers))

    print("Done!")


if __name__ == "__main__":
    main()
