from .protocol import Paper
from .url_utils import safe_http_url
import math
from html import escape


framework = """
<!DOCTYPE HTML>
<html>
<head>
  <style>
    .star-wrapper {
      font-size: 1.3em; /* 调整星星大小 */
      line-height: 1; /* 确保垂直对齐 */
      display: inline-flex;
      align-items: center; /* 保持对齐 */
    }
    .half-star {
      display: inline-block;
      width: 0.5em; /* 半颗星的宽度 */
      overflow: hidden;
      white-space: nowrap;
      vertical-align: middle;
    }
    .full-star {
      vertical-align: middle;
    }
  </style>
</head>
<body>

<div>
    __CONTENT__
</div>

<br><br>
<div>
To unsubscribe, remove your email in your Github Action setting.
</div>

</body>
</html>
"""

def get_empty_html():
  block_template = """
  <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-family: Arial, sans-serif; border: 1px solid #ddd; border-radius: 8px; padding: 16px; background-color: #f9f9f9;">
  <tr>
    <td style="font-size: 20px; font-weight: bold; color: #333;">
        No Papers Today. Take a Rest!
    </td>
  </tr>
  </table>
  """
  return block_template

def get_block_html(title:str, authors:str, rate:str, tldr:str, pdf_url:str | None, affiliations:str=None, link_label:str="PDF", source_display:str | None=None, abstract_note:str | None=None):
    safe_url = safe_http_url(pdf_url)
    link_html = ""
    if safe_url:
        link_html = f'''<tr><td style="padding: 8px 0;"><a href="{escape(safe_url, quote=True)}" style="display: inline-block; text-decoration: none; font-size: 14px; font-weight: bold; color: #fff; background-color: #d9534f; padding: 8px 16px; border-radius: 4px;">{escape(link_label)}</a></td></tr>'''
    source_html = ""
    if source_display:
        source_html = f'''<tr><td style="font-size: 14px; color: #666; padding: 4px 0;"><strong>来源：</strong> {escape(source_display)}</td></tr>'''
    abstract_html = ""
    if abstract_note:
        abstract_html = f'''<tr><td style="font-size: 14px; color: #666; padding: 4px 0;"><strong>摘要：</strong> {escape(abstract_note)}</td></tr>'''
    block_template = """
    <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-family: Arial, sans-serif; border: 1px solid #ddd; border-radius: 8px; padding: 16px; background-color: #f9f9f9;">
    <tr>
        <td style="font-size: 20px; font-weight: bold; color: #333;">
            {title}
        </td>
    </tr>
    {source_html}
    <tr>
        <td style="font-size: 14px; color: #666; padding: 8px 0;">
            {authors}
            <br>
            <i>{affiliations}</i>
        </td>
    </tr>
    <tr>
        <td style="font-size: 14px; color: #333; padding: 8px 0;">
            <strong>Relevance:</strong> {rate}
        </td>
    </tr>
    {abstract_html}
    <tr>
        <td style="font-size: 14px; color: #333; padding: 8px 0;">
            <strong>TLDR:</strong> {tldr}
        </td>
    </tr>

    {link_html}
</table>
"""
    return block_template.format(
        title=escape(title or ""),
        authors=escape(authors or ""),
        rate=escape(str(rate)),
        tldr=escape(tldr or ""),
        affiliations=escape(affiliations or ""),
        source_html=source_html,
        abstract_html=abstract_html,
        link_html=link_html,
    )


def _paper_source_display(paper: Paper) -> str:
    if paper.source_display and paper.source_display.strip():
        return paper.source_display.strip()
    defaults = {
        "arxiv": "arXiv",
        "biorxiv": "bioRxiv",
        "medrxiv": "medRxiv",
        "chemrxiv": "ChemRxiv",
    }
    source = (paper.source or "").strip()
    if not source:
        return "Unknown"
    return defaults.get(source.casefold(), source.replace("_", " ").replace("-", " ").title())

def get_stars(score:float):
    full_star = '<span class="full-star">⭐</span>'
    half_star = '<span class="half-star">⭐</span>'
    low = 6
    high = 8
    if score <= low:
        return ''
    elif score >= high:
        return full_star * 5
    else:
        interval = (high-low) / 10
        star_num = math.ceil((score-low) / interval)
        full_star_num = int(star_num/2)
        half_star_num = star_num - full_star_num * 2
        return '<div class="star-wrapper">'+full_star * full_star_num + half_star * half_star_num + '</div>'


def render_email(papers:list[Paper]) -> str:
    parts = []
    if len(papers) == 0 :
        return framework.replace('__CONTENT__', get_empty_html())
    
    for p in papers:
        #rate = get_stars(p.score)
        rate = round(p.score, 1) if p.score is not None else 'Unknown'
        author_list = [a for a in p.authors]
        num_authors = len(author_list)
        if num_authors <= 5:
            authors = ', '.join(author_list)
        else:
            authors = ', '.join(author_list[:3] + ['...'] + author_list[-2:])
        if p.affiliations is not None:
            affiliations = p.affiliations[:5]
            affiliations = ', '.join(affiliations)
            if len(p.affiliations) > 5:
                affiliations += ', ...'
        else:
            affiliations = 'Unknown Affiliation'
        if (p.source or "").casefold() == "arxiv":
            tldr = p.tldr or p.abstract or "No abstract available; open the article for details."
            abstract_note = None
        else:
            abstract_available = bool((p.abstract or "").strip())
            tldr = p.tldr or p.abstract or ""
            abstract_note = None
            if not abstract_available:
                if tldr:
                    abstract_note = "未获取到摘要"
                else:
                    tldr = "未获取到摘要"
        pdf_url = safe_http_url(p.pdf_url)
        article_url = safe_http_url(p.url)
        button_url = pdf_url or article_url
        button_label = "PDF" if pdf_url else "Article"
        parts.append(get_block_html(
            p.title,
            authors,
            rate,
            tldr,
            button_url,
            affiliations,
            button_label,
            _paper_source_display(p),
            abstract_note,
        ))

    content = '<br>' + '</br><br>'.join(parts) + '</br>'
    return framework.replace('__CONTENT__', content)
