from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
import asyncio
import json
import re
from groq import Groq
from playwright.async_api import async_playwright
import random
import time
import requests as req_lib
from xml.etree import ElementTree as ET
from urllib.parse import urlparse
import base64

app = Flask(__name__, static_folder='.')
app.config['JSON_AS_ASCII'] = False  # Allow non-ASCII characters in JSON responses
CORS(app)

COMPANY_PATTERNS = [
    r'\b(inc|ltd|llc|corp|co|pvt|limited|group|holdings|enterprises|solutions|services|technologies|tech|systems)\b',
    r'\b(amazon|google|microsoft|apple|meta|netflix|uber|airbnb|shopify|salesforce|hubspot|semrush|ahrefs|moz)\b',
    r'\b(flipkart|swiggy|zomato|paytm|razorpay|infosys|wipro|tcs|reliance|hdfc|icici|sbi|bajaj|mahindra)\b',
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

def is_company_or_generic(keyword):
    kw_lower = keyword.lower().strip()
    generic = ['best', 'top', 'free', 'online', 'buy', 'cheap', 'near me', 'review', 'reviews', 'vs', 'price', 'cost', 'how to', 'what is']
    for g in generic:
        if kw_lower == g:
            return True, 'generic'
    for pat in COMPANY_PATTERNS:
        if re.search(pat, kw_lower, re.IGNORECASE):
            return True, 'company'
    words = kw_lower.split()
    if len(words) == 1 and len(kw_lower) <= 3:
        return True, 'too_short'
    return False, None

# ── SITEMAP HELPERS ──────────────────────────────────────────────────────────

def discover_sitemap_urls(base_url):
    if not base_url.startswith('http'):
        base_url = 'https://' + base_url
    base_url = base_url.rstrip('/')
    parsed = urlparse(base_url)
    domain = f"{parsed.scheme}://{parsed.netloc}"

    candidates = []
    try:
        r = req_lib.get(f"{domain}/robots.txt", timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code == 200:
            for line in r.text.splitlines():
                if line.lower().startswith('sitemap:'):
                    sm_url = line.split(':', 1)[1].strip()
                    if sm_url not in candidates:
                        candidates.append(sm_url)
    except Exception:
        pass

    candidates += [
        f"{domain}/sitemap.xml",
        f"{domain}/sitemap_index.xml",
        f"{domain}/wp-sitemap.xml",
        f"{domain}/sitemap-index.xml",
        f"{domain}/post-sitemap.xml",
        f"{domain}/blog-sitemap.xml",
    ]

    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        try:
            r = req_lib.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            ct = r.headers.get('content-type', '')
            if r.status_code == 200 and ('xml' in ct or r.text.strip().startswith('<')):
                urls = parse_sitemap_xml(r.content)
                if urls:
                    return urls
        except Exception:
            continue
    return []


def parse_sitemap_xml(content, depth=0):
    if depth > 2:
        return []
    urls = []
    try:
        root = ET.fromstring(content)
        tag = root.tag
        ns = tag[1:tag.index('}')] if tag.startswith('{') else ''

        def findall(el, local):
            return el.findall(f'{{{ns}}}{local}') if ns else el.findall(local)

        for sm in findall(root, 'sitemap'):
            locs = findall(sm, 'loc')
            if locs and locs[0].text:
                try:
                    r = req_lib.get(locs[0].text.strip(), timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
                    if r.status_code == 200:
                        urls.extend(parse_sitemap_xml(r.content, depth + 1))
                        if len(urls) >= 500:
                            break
                except Exception:
                    pass

        for url_el in findall(root, 'url'):
            locs = findall(url_el, 'loc')
            if locs and locs[0].text:
                urls.append(locs[0].text.strip())
                if len(urls) >= 500:
                    break
    except ET.ParseError:
        pass
    return urls


def filter_blog_urls(urls):
    blog_signals = ['/blog', '/post', '/article', '/news', '/insight', '/resource',
                    '/guide', '/tutorial', '/learn', '/story', '/tips', '/update']
    skip_signals = ['/category/', '/tag/', '/author/', '/page/', '/feed',
                    '.xml', '.json', '?', '#', '/wp-admin', '/wp-content']
    result = []
    for url in urls:
        u = url.lower()
        if any(s in u for s in skip_signals):
            continue
        if any(s in u for s in blog_signals):
            result.append(url)
    if not result:
        result = [u for u in urls if urlparse(u).path not in ('', '/')
                  and not any(s in u.lower() for s in skip_signals)]
    return result

# ─────────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/filter-keywords', methods=['POST'])
def filter_keywords():
    data = request.json
    keywords = data.get('keywords', [])
    results = []
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        flagged, reason = is_company_or_generic(kw)
        results.append({'keyword': kw, 'flagged': flagged, 'reason': reason})
    return jsonify(results)

@app.route('/api/scrape-serp', methods=['POST'])
def scrape_serp():
    data = request.json
    keyword = data.get('keyword', '')
    if not keyword:
        return jsonify({'error': 'No keyword provided'}), 400
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(do_serp_scrape(keyword))
        return jsonify(result)
    finally:
        loop.close()

async def do_serp_scrape(keyword):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                '--no-sandbox',
                '--disable-blink-features=AutomationControlled',
                '--disable-infobars',
            ]
        )
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={'width': 1366, 'height': 768},
            locale='en-IN',
            timezone_id='Asia/Kolkata',
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-IN', 'en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        """)
        
        page = await context.new_page()
        
        try:
            await page.goto('https://www.google.com', wait_until='domcontentloaded')
            await page.wait_for_timeout(random.randint(1000, 2000))
            
            if await page.query_selector('form#captcha-form') or 'sorry' in page.url:
                await browser.close()
                return {'captcha': True, 'message': 'CAPTCHA detected on Google. Please solve it manually.'}
            
            try:
                btn = await page.query_selector('button[id="L2AGLb"]')
                if btn:
                    await btn.click()
                    await page.wait_for_timeout(500)
            except:
                pass
            
            search_box = await page.wait_for_selector('textarea[name="q"], input[name="q"]', timeout=5000)
            await search_box.click()
            await page.wait_for_timeout(random.randint(300, 600))
            for char in keyword:
                await page.keyboard.type(char, delay=random.randint(60, 130))
            
            await page.wait_for_timeout(random.randint(500, 1000))
            await page.keyboard.press('Enter')
            await page.wait_for_load_state('domcontentloaded')
            await page.wait_for_timeout(random.randint(1500, 2500))
            
            if 'sorry' in page.url or await page.query_selector('#captcha'):
                await page.wait_for_url(lambda url: 'google.com/search' in url, timeout=120000)
            
            results = []
            selectors = ['div.g', 'div[data-sokoban-container]', '.tF2Cxc']
            
            for sel in selectors:
                items = await page.query_selector_all(sel)
                if items and len(items) >= 3:
                    for i, item in enumerate(items[:10]):
                        try:
                            title_el = await item.query_selector('h3')
                            link_el = await item.query_selector('a[href]')
                            desc_el = await item.query_selector('.VwiC3b, .lEBKkf, span[data-ved]')
                            
                            title = await title_el.inner_text() if title_el else ''
                            href = await link_el.get_attribute('href') if link_el else ''
                            desc = await desc_el.inner_text() if desc_el else ''
                            
                            if title and href and href.startswith('http'):
                                results.append({
                                    'position': len(results) + 1,
                                    'title': title.strip(),
                                    'url': href,
                                    'description': desc.strip()[:200]
                                })
                        except:
                            continue
                    if results:
                        break
            
            paa = []
            try:
                paa_els = await page.query_selector_all('[data-q], .related-question-pair')
                for el in paa_els[:5]:
                    txt = await el.inner_text()
                    if txt and '?' in txt:
                        paa.append(txt.strip().split('\n')[0])
            except:
                pass
            
            await browser.close()
            return {
                'keyword': keyword,
                'serp': results[:10],
                'paa': paa,
                'captcha': False
            }
        except Exception as e:
            await browser.close()
            return {'error': str(e), 'captcha': False}

@app.route('/api/analyze', methods=['POST'])
def analyze():
    data = request.json
    keyword = data.get('keyword', '')
    serp_data = data.get('serp', [])
    paa = data.get('paa', [])
    groq_key = data.get('groq_key', '')
    
    if not groq_key:
        return jsonify({'error': 'Groq API key required'}), 400
    
    client = Groq(api_key=groq_key)
    
    serp_text = '\n'.join([f"{r['position']}. {r['title']} - {r['url']}\n   {r['description']}" for r in serp_data])
    paa_text = '\n'.join(paa) if paa else 'Not available'
    competitors = [r['url'].split('/')[2] for r in serp_data[:5]]
    
    prompt = f"""You are a world-class SEO strategist and content architect with 15+ years of experience. Deeply analyze the keyword "{keyword}" with these live SERP results and produce an exhaustive, actionable SEO intelligence report.

LIVE SERP DATA:
{serp_text}

People Also Ask:
{paa_text}

Return ONLY valid JSON (no markdown, no code fences) with this EXACT structure. Be extremely detailed, specific, and actionable — generic advice is not acceptable:

{{
  "keyword_overview": {{
    "primary_keyword": "{keyword}",
    "search_intent": "informational|transactional|navigational|commercial",
    "intent_explanation": "2-3 sentence explanation of WHY this is the intent and what the user really wants",
    "difficulty": "Low|Medium|High",
    "difficulty_score": 45,
    "difficulty_reasoning": "Specific reason based on SERP analysis",
    "opportunity_score": 72,
    "opportunity_reasoning": "Why this keyword is a good/bad opportunity",
    "word_count_target": 2800,
    "content_type": "Ultimate Guide|Listicle|How-To|Comparison|Review|News",
    "serp_features": ["Featured Snippet", "PAA Box", "Image Pack", "Video Results"]
  }},
  "keyword_variations": [
    {{"kw": "variation 1", "type": "LSI", "rationale": "why include it"}},
    {{"kw": "variation 2", "type": "Long-tail", "rationale": "why include it"}},
    {{"kw": "variation 3", "type": "Semantic", "rationale": "why include it"}},
    {{"kw": "variation 4", "type": "Question", "rationale": "why include it"}},
    {{"kw": "variation 5", "type": "Local", "rationale": "why include it"}},
    {{"kw": "variation 6", "type": "Comparison", "rationale": "why include it"}},
    {{"kw": "variation 7", "type": "Long-tail", "rationale": "why include it"}}
  ],
  "blog_cluster": {{
    "parent": {{
      "title": "Comprehensive parent blog title",
      "slug": "url-friendly-slug",
      "target_word_count": 4000,
      "primary_angle": "The unique angle or hook that makes this pillar stand out",
      "key_sections": ["Section 1", "Section 2", "Section 3", "Section 4", "Section 5"]
    }},
    "children": [
      {{
        "title": "Child blog 1 full title",
        "slug": "child-slug-1",
        "target_word_count": 1800,
        "focus_keyword": "specific keyword to target",
        "angle": "Unique angle for this child post",
        "internal_link_anchor": "anchor text to use from parent"
      }},
      {{
        "title": "Child blog 2 full title",
        "slug": "child-slug-2",
        "target_word_count": 1600,
        "focus_keyword": "specific keyword to target",
        "angle": "Unique angle for this child post",
        "internal_link_anchor": "anchor text to use from parent"
      }},
      {{
        "title": "Child blog 3 full title",
        "slug": "child-slug-3",
        "target_word_count": 2000,
        "focus_keyword": "specific keyword to target",
        "angle": "Unique angle for this child post",
        "internal_link_anchor": "anchor text to use from parent"
      }},
      {{
        "title": "Child blog 4 full title",
        "slug": "child-slug-4",
        "target_word_count": 1500,
        "focus_keyword": "specific keyword to target",
        "angle": "Unique angle for this child post",
        "internal_link_anchor": "anchor text to use from parent"
      }},
      {{
        "title": "Child blog 5 full title",
        "slug": "child-slug-5",
        "target_word_count": 1700,
        "focus_keyword": "specific keyword to target",
        "angle": "Unique angle for this child post",
        "internal_link_anchor": "anchor text to use from parent"
      }},
      {{
        "title": "Child blog 6 full title",
        "slug": "child-slug-6",
        "target_word_count": 1900,
        "focus_keyword": "specific keyword to target",
        "angle": "Unique angle for this child post",
        "internal_link_anchor": "anchor text to use from parent"
      }}
    ]
  }},
  "competitor_insights": [
    {{
      "domain": "competitor1.com",
      "rank": 1,
      "title": "Their page title",
      "strength": "What makes this page rank — content depth, authority, backlinks, UX",
      "weakness": "Specific gap or weakness in their content",
      "gap_opportunity": "Exactly what you can do better to outrank them"
    }},
    {{
      "domain": "competitor2.com",
      "rank": 2,
      "title": "Their page title",
      "strength": "What makes this page rank",
      "weakness": "Specific gap",
      "gap_opportunity": "Exactly what you can do better"
    }},
    {{
      "domain": "competitor3.com",
      "rank": 3,
      "title": "Their page title",
      "strength": "What makes this page rank",
      "weakness": "Specific gap",
      "gap_opportunity": "Exactly what you can do better"
    }}
  ],
  "content_blueprint": {{
    "meta_title": "SEO optimized title under 60 chars with keyword near front",
    "meta_description": "Compelling meta desc under 155 chars with CTA and keyword",
    "url_slug": "keyword-based-url-slug",
    "introduction_hook": "2-3 sentence opening hook that grabs attention and states the value proposition",
    "h1_tag": "Exact H1 tag text (different from meta title)",
    "h2_structure": [
      {{
        "zone": "Introduction Zone",
        "h2": "H2 heading text",
        "purpose": "Hook reader, define scope, include primary keyword",
        "key_points": ["Point 1 to cover", "Point 2 to cover", "Point 3 to cover"],
        "word_count": 300
      }},
      {{
        "zone": "Problem/Context Zone",
        "h2": "H2 heading text",
        "purpose": "Identify the reader pain point and build empathy",
        "key_points": ["Point 1", "Point 2", "Point 3"],
        "word_count": 350
      }},
      {{
        "zone": "Core Solution Zone",
        "h2": "H2 heading text",
        "purpose": "Primary value delivery — the main answer to the query",
        "key_points": ["Point 1", "Point 2", "Point 3", "Point 4"],
        "word_count": 500
      }},
      {{
        "zone": "Deep Dive Zone",
        "h2": "H2 heading text",
        "purpose": "Expert-level detail that separates you from thin content",
        "key_points": ["Point 1", "Point 2", "Point 3"],
        "word_count": 450
      }},
      {{
        "zone": "Comparison/Options Zone",
        "h2": "H2 heading text",
        "purpose": "Help reader evaluate choices — boosts commercial intent signals",
        "key_points": ["Point 1", "Point 2", "Point 3"],
        "word_count": 400
      }},
      {{
        "zone": "Best Practices Zone",
        "h2": "H2 heading text",
        "purpose": "Actionable tips that add unique value",
        "key_points": ["Point 1", "Point 2", "Point 3"],
        "word_count": 350
      }},
      {{
        "zone": "FAQ / PAA Zone",
        "h2": "Frequently Asked Questions",
        "purpose": "Target PAA boxes and voice search — use exact question format",
        "key_points": ["FAQ 1", "FAQ 2", "FAQ 3"],
        "word_count": 300
      }},
      {{
        "zone": "Action/CTA Zone",
        "h2": "H2 heading text",
        "purpose": "Convert intent to action — clear next step for the reader",
        "key_points": ["CTA 1", "CTA 2"],
        "word_count": 150
      }}
    ]
  }},
  "faq": [
    {{"question": "Question 1 from PAA/common queries?", "answer": "Detailed, specific answer in 2-3 sentences using keyword naturally."}},
    {{"question": "Question 2?", "answer": "Detailed answer."}},
    {{"question": "Question 3?", "answer": "Detailed answer."}},
    {{"question": "Question 4?", "answer": "Detailed answer."}},
    {{"question": "Question 5?", "answer": "Detailed answer."}},
    {{"question": "Question 6?", "answer": "Detailed answer."}}
  ],
  "on_page_seo": {{
    "keyword_density_target": "1.2-1.8%",
    "primary_keyword_placements": ["H1", "First 100 words", "One H2", "Meta title", "Alt text", "Last paragraph"],
    "lsi_keywords_to_use": ["lsi1", "lsi2", "lsi3", "lsi4", "lsi5"],
    "internal_linking_strategy": "Specific internal linking advice for this topic",
    "image_optimization": "Alt text formula and image count recommendation",
    "schema_markup": ["Article", "FAQPage", "BreadcrumbList"],
    "page_speed_note": "Content-specific loading tip"
  }},
  "off_page_seo": {{
    "link_building_tactics": [
      {{"tactic": "Tactic name", "description": "Specific how-to for this keyword/niche", "difficulty": "Easy|Medium|Hard", "impact": "Low|Medium|High"}},
      {{"tactic": "Tactic name", "description": "Specific how-to", "difficulty": "Medium", "impact": "High"}},
      {{"tactic": "Tactic name", "description": "Specific how-to", "difficulty": "Hard", "impact": "High"}}
    ],
    "target_anchor_texts": ["anchor 1", "anchor 2", "anchor 3", "anchor 4"],
    "outreach_ideas": ["Idea 1 specific to this niche", "Idea 2", "Idea 3"]
  }},
  "content_promotion": {{
    "social_channels": ["Channel 1 with why", "Channel 2 with why"],
    "content_repurposing": ["Repurpose idea 1", "Repurpose idea 2", "Repurpose idea 3"],
    "email_subject_line": "Email newsletter subject line to promote this post"
  }},
  "seo_recommendations": [
    {{"priority": "Critical", "tip": "Specific actionable tip 1 directly tied to this keyword/SERP"}},
    {{"priority": "Critical", "tip": "Specific actionable tip 2"}},
    {{"priority": "High", "tip": "Specific actionable tip 3"}},
    {{"priority": "High", "tip": "Specific actionable tip 4"}},
    {{"priority": "Medium", "tip": "Specific actionable tip 5"}},
    {{"priority": "Medium", "tip": "Specific actionable tip 6"}},
    {{"priority": "Low", "tip": "Specific actionable tip 7"}}
  ],
  "ranking_timeline": {{
    "month_1_3": "What to expect and focus on in first 3 months",
    "month_3_6": "What to expect and do in months 3-6",
    "month_6_12": "Expected position and ongoing strategy for month 6-12"
  }}
}}"""

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
            temperature=0.3
        )
        raw = response.choices[0].message.content.strip()
        # Strip markdown code fences
        raw = re.sub(r'^```json\s*', '', raw)
        raw = re.sub(r'^```\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        # Replace smart quotes / em-dashes that break ASCII encoding
        replacements = {
            '\u2014': '-', '\u2013': '-',
            '\u2018': "'", '\u2019': "'",
            '\u201c': '"', '\u201d': '"',
            '\u2026': '...', '\u00a0': ' ',
            '\u2022': '-', '\u00e2\u0080\u0099': "'",
        }
        for bad, good in replacements.items():
            raw = raw.replace(bad, good)
        result = json.loads(raw)
        result['competitors_raw'] = competitors
        # Return explicit UTF-8 Response to avoid Flask ASCII encoding issues
        json_str = json.dumps(result, ensure_ascii=False)
        return Response(json_str, content_type='application/json; charset=utf-8')
    except json.JSONDecodeError as e:
        err = json.dumps({'error': f'JSON parse error: {str(e)}', 'raw': raw[:500]}, ensure_ascii=False)
        return Response(err, status=500, content_type='application/json; charset=utf-8')
    except Exception as e:
        err = json.dumps({'error': str(e)}, ensure_ascii=False)
        return Response(err, status=500, content_type='application/json; charset=utf-8')

# ── SITE ANALYSIS ROUTES ─────────────────────────────────────────────────────

@app.route('/api/scan-sitemap', methods=['POST'])
def scan_sitemap():
    data = request.json
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    try:
        all_urls = discover_sitemap_urls(url)
        if not all_urls:
            out = json.dumps({'error': 'No sitemap found. Ensure the site has sitemap.xml or a Sitemap: line in robots.txt.'}, ensure_ascii=False)
            return Response(out, status=404, content_type='application/json; charset=utf-8')
        blog_urls = filter_blog_urls(all_urls)
        parsed = urlparse(url if url.startswith('http') else 'https://' + url)
        out = json.dumps({
            'domain': parsed.netloc,
            'total_pages': len(all_urls),
            'blog_count': len(blog_urls),
            'blog_urls': blog_urls[:200],
        }, ensure_ascii=False)
        return Response(out, content_type='application/json; charset=utf-8')
    except Exception as e:
        out = json.dumps({'error': str(e)}, ensure_ascii=False)
        return Response(out, status=500, content_type='application/json; charset=utf-8')


@app.route('/api/analyze-gaps', methods=['POST'])
def analyze_gaps():
    data = request.json
    blog_urls = data.get('blog_urls', [])
    domain = data.get('domain', '')
    groq_key = data.get('groq_key', '')

    if not groq_key:
        return jsonify({'error': 'Groq API key required'}), 400
    if not blog_urls:
        return jsonify({'error': 'No blog URLs to analyze'}), 400

    client = Groq(api_key=groq_key)
    url_text = '\n'.join(blog_urls[:120])

    prompt = f"""You are an expert SEO content strategist. Analyze the following blog/content URLs from {domain}.
Identify what topics are already covered and what important content gaps exist.

EXISTING CONTENT URLs:
{url_text}

Return ONLY valid JSON (no markdown, no code fences) with this exact structure:
{{
  "covered_topics": [
    {{
      "category": "Category Name",
      "post_count": 5,
      "sample_urls": ["url1", "url2"],
      "coverage_summary": "1 sentence describing what this category covers"
    }}
  ],
  "content_gaps": [
    {{
      "topic": "Gap Topic Name",
      "priority": "High",
      "rationale": "Why this gap matters — what users search for that this site doesn't cover",
      "content_type": "How-To Guide",
      "suggested_keywords": ["primary keyword", "secondary kw 1", "secondary kw 2", "long-tail variation"]
    }},
    {{
      "topic": "Gap Topic 2",
      "priority": "High",
      "rationale": "...",
      "content_type": "Listicle",
      "suggested_keywords": ["kw1", "kw2", "kw3", "kw4"]
    }},
    {{
      "topic": "Gap Topic 3",
      "priority": "Medium",
      "rationale": "...",
      "content_type": "Comparison",
      "suggested_keywords": ["kw1", "kw2", "kw3"]
    }},
    {{
      "topic": "Gap Topic 4",
      "priority": "Medium",
      "rationale": "...",
      "content_type": "Deep Dive",
      "suggested_keywords": ["kw1", "kw2", "kw3"]
    }},
    {{
      "topic": "Gap Topic 5",
      "priority": "Medium",
      "rationale": "...",
      "content_type": "Case Study",
      "suggested_keywords": ["kw1", "kw2", "kw3"]
    }},
    {{
      "topic": "Gap Topic 6",
      "priority": "Low",
      "rationale": "...",
      "content_type": "How-To Guide",
      "suggested_keywords": ["kw1", "kw2", "kw3"]
    }},
    {{
      "topic": "Gap Topic 7",
      "priority": "Low",
      "rationale": "...",
      "content_type": "Listicle",
      "suggested_keywords": ["kw1", "kw2", "kw3"]
    }},
    {{
      "topic": "Gap Topic 8",
      "priority": "Low",
      "rationale": "...",
      "content_type": "Comparison",
      "suggested_keywords": ["kw1", "kw2", "kw3"]
    }}
  ],
  "strategy_summary": "2-3 sentence assessment of the biggest content opportunity on this site"
}}"""

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3000,
            temperature=0.3
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r'^```json\s*', '', raw)
        raw = re.sub(r'^```\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        for bad, good in {'\u2014': '-', '\u2013': '-', '\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"'}.items():
            raw = raw.replace(bad, good)
        result = json.loads(raw)
        out = json.dumps(result, ensure_ascii=False)
        return Response(out, content_type='application/json; charset=utf-8')
    except json.JSONDecodeError as e:
        out = json.dumps({'error': f'JSON parse error: {str(e)}'}, ensure_ascii=False)
        return Response(out, status=500, content_type='application/json; charset=utf-8')
    except Exception as e:
        out = json.dumps({'error': str(e)}, ensure_ascii=False)
        return Response(out, status=500, content_type='application/json; charset=utf-8')


@app.route('/api/dataforseo-keywords', methods=['POST'])
def dataforseo_keywords():
    data = request.json
    keywords = data.get('keywords', [])
    login = data.get('login', '')
    password = data.get('password', '')
    location_code = data.get('location_code', 2840)
    language_code = data.get('language_code', 'en')

    if not login or not password:
        return jsonify({'error': 'DataForSEO credentials required'}), 400
    if not keywords:
        return jsonify({'error': 'No keywords provided'}), 400

    creds = base64.b64encode(f"{login}:{password}".encode()).decode()
    auth_headers = {
        'Authorization': f'Basic {creds}',
        'Content-Type': 'application/json'
    }

    all_results = {}
    for i in range(0, min(len(keywords), 200), 50):
        batch = keywords[i:i + 50]
        payload = [{"keywords": batch, "language_code": language_code, "location_code": location_code}]
        try:
            resp = req_lib.post(
                'https://api.dataforseo.com/v3/keywords_data/google_ads/search_volume/live',
                headers=auth_headers, json=payload, timeout=30
            )
            resp_data = resp.json()
            if resp_data.get('status_code') == 20000:
                for task in resp_data.get('tasks', []):
                    for item in (task.get('result') or []):
                        kw = item.get('keyword', '')
                        if kw:
                            all_results[kw] = {
                                'volume': item.get('search_volume', 0),
                                'competition': item.get('competition_level', '—'),
                                'cpc': round(item.get('cpc', 0) or 0, 2),
                                'kd': item.get('keyword_difficulty', 0),
                            }
            else:
                out = json.dumps({'error': resp_data.get('status_message', 'DataForSEO API error')}, ensure_ascii=False)
                return Response(out, status=400, content_type='application/json; charset=utf-8')
        except Exception as e:
            out = json.dumps({'error': f'DataForSEO request failed: {str(e)}'}, ensure_ascii=False)
            return Response(out, status=500, content_type='application/json; charset=utf-8')

    out = json.dumps(all_results, ensure_ascii=False)
    return Response(out, content_type='application/json; charset=utf-8')

# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("SEO Automation Server running at http://localhost:5000")
    app.run(debug=False, host='0.0.0.0', port=5000)
