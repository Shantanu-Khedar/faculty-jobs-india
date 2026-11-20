#!/usr/bin/env python3
"""Daily checker for university and college faculty notices across India.

1. Reads sources.json and scans each official page (and, when "follow" is
   true, up to 5 recruitment/career/notice pages linked from it on the same
   site). Links whose words look like a faculty-recruitment notice are kept.
2. Opens each not-yet-analysed notice (PDF or web page), reads its text
   (pdftotext; OCR with tesseract for scanned PDFs) and tags it with the
   posts, subjects, type of appointment and last date it mentions.
3. Writes everything to notices.json, which the website reads.

Runs on GitHub Actions every morning (.github/workflows/update.yml).
Needs: python package `requests`; programs `pdftotext`, `pdftoppm`,
`tesseract` (poppler-utils, tesseract-ocr) for reading PDFs.
"""
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from html.parser import HTMLParser

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES_FILE = os.path.join(HERE, "sources.json")
NOTICES_FILE = os.path.join(HERE, "notices.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 25
MAX_FOLLOW = 5
WORKERS = 16
KEEP_DAYS = 150            # forget notices not seen for this many days
ANALYSIS_VERSION = 2       # bump to re-read every notice after changing the tagging rules
ANALYSE_PER_RUN = int(os.environ.get("ANALYSE_PER_RUN", "400"))
OCR_PER_RUN = int(os.environ.get("OCR_PER_RUN", "30"))
MAX_DOC_BYTES = 15 * 1024 * 1024
TIME_BUDGET = int(os.environ.get("TIME_BUDGET_SECONDS", str(35 * 60)))
START = time.time()

# ---------------------------------------------------------------- matching
INCLUDE = re.compile(
    r"assistant\s*-?\s*prof|asst\.?\s*prof|associate\s*prof|assoc\.?\s*prof|\bprofessors?\b|"
    r"teaching\s+(post|position|faculty|staff|vacanc|cadre)|faculty\s+(recruit|position|post|vacanc|requirement)|"
    r"recruitment\s+of\s+(teachers|faculty)|guest\s+(faculty|lecturer)|extension\s+lecturer|"
    r"ad-?hoc\s+(faculty|teacher|lecturer)|contractual\s+faculty|\blecturers?\b|"
    r"walk[\s-]*in.*(faculty|teach|professor)|rolling\s+advertisement",
    re.I)
EXCLUDE = re.compile(
    r"\bresults?\b|answer\s*key|admit\s*card|e-?admit|roll\s*n|syllabus|scrutiny|short\s*-?\s*list|"
    r"screening|(?<!in)eligible|marks|cut\s*-?\s*off|interview\s+schedule|schedule\s+of\s+(the\s+)?interview|"
    r"document\s+verification|appointment\s+letter|posting|transfer|seniority|promotion|"
    r"career\s+advancement|\bcas\b|joining|attendance|time\s*table|exam(ination)?\s+(date|schedule|centre|center)|"
    r"emeritus|professor\s+of\s+practice\s+lecture|award|felicitat|obituar|condolence|seminar|webinar|conference|"
    r"workshop|lecture\s+series|memorial\s+lecture|admission|ph\.?\s*d\.?\s+(entrance|admission)|tender|quotation|"
    r"non[\s-]*teaching|project\s+(technical|assistant|associate|fellow|staff|scientist|consultant)|"
    r"research\s+(assistant|associate|fellow)|\bjrf\b|\bsrf\b|grievance|select(ed|ion)\s+(list|candidates)|"
    r"final\s+select|list\s+of\s+(selected|candidates|applicants)|application\s+status|selection\s+criteria|"
    r"counsel+ing|knowledge\s+test|additional\s+(duty|charge)|in-?charge|assigning|"
    r"regarding\s+(the\s+)?(appointment|assignment|nomination)\s+of\s+(prof|dr|shri|smt)|appoint(ed|ment\s+of)?\s+prof\.?\s+[a-z]|"
    r"nominat|superannuat|retire|relieving|extension\s+of\s+(service|tenure)|re-?employment|"
    r"provisional\s+answer|objection|hall\s+ticket|call\s+letter|provisional\s+list|wait\s*-?\s*list|"
    r"eligibility\s+list|eligibility_list|regarding\s+(the\s+)?applicants|written\s+test|non[\s-]*faculty|"
    r"screening\s+report|merit\s+list|interview\s+result|joining\s+report",
    re.I)
FOLLOW = re.compile(r"recruit|career|\bjobs?\b|vacanc|employment|advertisement|notices?\b|teaching\s+posts|"
                    r"faculty\s+(position|recruit)|opportunit", re.I)

# Posts a notice mentions.
POSTS = [
    ("Assistant Professor", re.compile(r"assistant\s*-?\s*professor|asst\.?\s*prof|assistant\s*(&|and|/|,)\s*associate\s+prof", re.I)),
    ("Associate Professor", re.compile(r"associate\s*-?\s*professor|assoc\.?\s*prof", re.I)),
    ("Professor", re.compile(r"(?<!assistant )(?<!associate )(?<!asst\. )(?<!asst )\bprofessors?\b(?!\s+of\s+practice)", re.I)),
    ("Guest / contract faculty", re.compile(r"guest\s+(faculty|lecturer|teacher)|contractual\s+(faculty|basis)|on\s+contract|ad-?hoc|extension\s+lecturer|visiting\s+faculty|teaching\s+assistant", re.I)),
    ("Lecturer", re.compile(r"\blecturers?\b(?!\s+series)", re.I)),
    ("Librarian", re.compile(r"\b(assistant\s+|deputy\s+)?librarian\b", re.I)),
]
BASIS = [
    ("Regular", re.compile(r"\bregular\s+(basis|post|appointment)|permanent\s+(basis|post)|direct\s+recruitment", re.I)),
    ("Contract", re.compile(r"contract(ual)?\s*(basis)?|on\s+contract", re.I)),
    ("Guest", re.compile(r"guest\s+(faculty|lecturer)|lecture\s+basis|visiting", re.I)),
    ("Ad-hoc / temporary", re.compile(r"ad-?hoc|temporary|stop[\s-]*gap", re.I)),
]

# Subjects. "strict" ones are common words, counted only next to post or
# department words so that "Department of Higher Education" etc. don't count.
SUBJECTS = [
    # sciences
    ("Mathematics", r"mathematics|maths\b|applied\s+math", False),
    ("Statistics", r"statistics|biostatistics", False),
    ("Physics", r"physics|astrophysics", False),
    ("Chemistry", r"chemistry", False),
    ("Botany", r"botany|plant\s+science", False),
    ("Zoology", r"zoology|animal\s+science", False),
    ("Biotechnology", r"biotechnology|bio-?technology", False),
    ("Biochemistry", r"biochemistry", False),
    ("Microbiology", r"microbiology", False),
    ("Life Sciences", r"life\s+sciences?|bioscience", False),
    ("Bioinformatics", r"bioinformatics", False),
    ("Environmental Science", r"environmental\s+(science|studies|engineering)", False),
    ("Geology / Earth Science", r"geology|earth\s+science|geophysics|applied\s+geology", False),
    ("Geography", r"geography", False),
    ("Forensic Science", r"forensic\s+science", False),
    ("Home Science", r"home\s+science|food\s+(and|&)\s+nutrition", False),
    ("Food Technology", r"food\s+(technology|science)", False),
    ("Electronics", r"electronics(?!\s+and\s+communication)|electronic\s+science", False),
    # engineering / computing
    ("Computer Science / IT", r"computer\s+science|\bcse\b|computer\s+applications?|information\s+technology|\bmca\b|\bbca\b|data\s+science|artificial\s+intelligence|machine\s+learning", False),
    ("Electronics & Communication Engg", r"electronics\s+(and|&)\s+communication|\bece\b", False),
    ("Electrical Engineering", r"electrical\s+(and\s+electronics\s+)?engineering|\beee\b|electrical(?=[\w\s,&]{0,40}engineering)", False),
    ("Mechanical Engineering", r"mechanical\s+engineering|mechatronics|mechanical(?=[\w\s,&]{0,40}engineering)", False),
    ("Civil Engineering", r"civil\s+engineering|civil(?=[\w\s,&]{0,40}engineering)", False),
    ("Chemical Engineering", r"chemical\s+engineering|chemical(?=[\w\s,&]{0,30}engineering)", False),
    ("Aerospace Engineering", r"aerospace|aeronautical", False),
    ("Metallurgy & Materials", r"metallurg|materials\s+science", False),
    ("Biomedical Engineering", r"biomedical\s+engineering", False),
    ("Architecture & Planning", r"architecture|planning", True),
    ("Pharmacy", r"pharmac(y|eutics|eutical|ology|ognosy)", False),
    # humanities & social sciences
    ("English", r"english", True),
    ("Hindi", r"hindi", True),
    ("Sanskrit", r"sanskrit", False),
    ("Punjabi", r"punjabi", True),
    ("Urdu", r"urdu", True),
    ("Bengali", r"bengali|bangla", True),
    ("Tamil", r"tamil", True),
    ("Telugu", r"telugu", True),
    ("Kannada", r"kannada", True),
    ("Malayalam", r"malayalam", True),
    ("Marathi", r"marathi", True),
    ("Gujarati", r"gujarati", True),
    ("Odia", r"odia|oriya", True),
    ("Assamese", r"assamese", True),
    ("Persian / Arabic", r"persian|arabic", True),
    ("Foreign Languages", r"french|german|spanish|japanese|chinese|russian|korean", True),
    ("Linguistics", r"linguistics", False),
    ("History", r"history|archaeology", True),
    ("Political Science", r"political\s+science|politics|international\s+relations", False),
    ("Public Administration", r"public\s+administration", False),
    ("Economics", r"economics", False),
    ("Sociology", r"sociology", False),
    ("Psychology", r"psychology", False),
    ("Philosophy", r"philosophy", False),
    ("Social Work", r"social\s+work", False),
    ("Anthropology", r"anthropology", False),
    ("Library Science", r"library\s+(and|&)\s+information\s+science|library\s+science", False),
    ("Journalism & Mass Comm.", r"journalism|mass\s+communication|media\s+studies", False),
    ("Music", r"\bmusic\b|vocal|instrumental|tabla", True),
    ("Fine Arts", r"fine\s+arts?|drawing\s+(and|&)\s+painting|visual\s+arts", False),
    ("Performing Arts / Dance / Theatre", r"performing\s+arts|\bdance\b|theatre|dramatics", True),
    ("Physical Education", r"physical\s+education|sports\s+science", False),
    ("Education (B.Ed./M.Ed.)", r"education|b\.?\s*ed\b|m\.?\s*ed\b|teacher\s+education", True),
    ("Law", r"\blaws?\b|legal\s+studies", True),
    ("Commerce", r"commerce", True),
    ("Management / MBA", r"management|business\s+administration|\bmba\b", True),
    ("Tourism & Hotel Mgmt", r"tourism|hotel\s+management|hospitality", False),
    ("Religious Studies", r"religious\s+studies|guru\s+granth|theology|buddhist\s+studies|islamic\s+studies", False),
    ("Women's Studies", r"women'?s\s+studies|gender\s+studies", False),
    ("Yoga", r"\byoga\b", True),
    # agriculture & veterinary
    ("Agriculture (all branches)", r"agronomy|agricultur|horticulture|entomology|plant\s+pathology|plant\s+breeding|soil\s+science|agricultural\s+economics|extension\s+education|seed\s+science|floriculture|forestry", False),
    ("Veterinary Science", r"veterinary|animal\s+husbandry|livestock", False),
    ("Fisheries", r"fisheries|aquaculture", False),
    ("Dairy Technology", r"dairy", True),
    # health
    ("Medicine (MBBS / MD / MS)", r"anatomy|physiology|pathology|anaesthes|anesthes|general\s+medicine|general\s+surgery|radiology|radio-?diagnosis|paediatric|pediatric|obstetric|gynaecolog|orthopaedic|orthopedic|ophthalmolog|dermatolog|psychiatr|cardiolog|nephrolog|neurolog|urolog|oncolog|gastroenterolog|endocrinolog|pulmonary|community\s+medicine|forensic\s+medicine|emergency\s+medicine|\bent\b|otorhinolaryngolog", False),
    ("Dentistry", r"dental|orthodontic|prosthodontic|periodontic|endodontic|oral\s+(surgery|medicine|pathology)", False),
    ("Nursing", r"nursing", False),
    ("AYUSH (Ayurveda/Homoeo/Unani)", r"ayurved|homoeopath|homeopath|unani|siddha|naturopathy", False),
    ("Physiotherapy & Allied Health", r"physiotherapy|optometry|medical\s+lab|radiotherapy|allied\s+health|occupational\s+therapy", False),
    ("Public Health", r"public\s+health|epidemiolog", False),
]
CONTEXT = r"(?:professor|lecturer|faculty|teacher|department|deptt?\.?|discipline|subject|school|centre|center|in|of|for|-|–|:|\()"
SUBJECT_RX = []
for name, pat, strict in SUBJECTS:
    if strict:
        rx = re.compile(rf"{CONTEXT}\s*(?:the\s+)?(?:{pat})\b|\b(?:{pat})\s*(?:\(|-|–|:|\d|department|deptt)", re.I)
    else:
        rx = re.compile(rf"\b(?:{pat})", re.I)
    SUBJECT_RX.append((name, rx, strict))
# Phrases that use subject words but are not subjects.
NOT_SUBJECT = re.compile(
    r"higher\s+education|department\s+of\s+education|ministry\s+of\s+education|board\s+of\s+(school\s+)?education|"
    r"directorate\s+of\s+(higher\s+)?education|education\s+department|science\s+education\s+and\s+research|"
    r"medical\s+education|technical\s+education|management\s+(information\s+)?system|"
    r"financial\s+management|faculty\s+of\s+law|english\s+version|hindi\s+version|in\s+english|in\s+hindi|"
    r"english\s+(and|&|or)\s+hindi|hindi\s+(and|&|or)\s+english|by-?\s*laws?|law\s+and\s+order|"
    r"history\s+of\s+the\s+(university|institute)|university\s+history",
    re.I)

MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
DATE_RX = re.compile(
    r"(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{4}|\d{2})\b|"
    r"(\d{1,2})(?:st|nd|rd|th)?\s*(?:of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?,?\s*(\d{4})|"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})",
    re.I)
LASTDATE_KEY = re.compile(
    r"last\s+date|closing\s+date|on\s+or\s+before|latest\s+by|not\s+later\s+than|up\s*to|upto|till|"
    r"due\s+date|deadline|submission\s+of\s+(online\s+)?application|walk[\s-]*in|interview\s+(will\s+be\s+held|on)",
    re.I)


# ---------------------------------------------------------------- fetching
BLOCK_TAGS = {"tr", "li", "p", "dd", "dt", "h1", "h2", "h3", "h4", "h5", "h6", "td"}


class LinkParser(HTMLParser):
    """Collects (link text, href, text of the surrounding row/item) for every <a>.

    The row/item text is used only when that row holds few links, so that
    navigation menus don't lend their words to every link inside them."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self._href, self._title, self._text = [], None, "", []
        self._blocks = []            # stack of [tag, [text parts], [link count]]
        self._skip = 0
        self._block = None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
            return
        if tag in BLOCK_TAGS:
            # a <td> inside a <tr> shares the row's text
            if not (tag == "td" and self._blocks and self._blocks[-1][0] == "tr"):
                self._blocks.append([tag, [], [0]])
        if tag == "a":
            a = dict(attrs)
            self._href, self._title, self._text = a.get("href"), a.get("title") or "", []
            self._block = self._blocks[-1] if self._blocks else None
            if self._block is not None:
                self._block[2][0] += 1

    def handle_data(self, data):
        if self._skip:
            return
        if self._blocks:
            self._blocks[-1][1].append(data)
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip = max(0, self._skip - 1)
            return
        if tag == "a" and self._href is not None:
            text = " ".join(" ".join(self._text).split())
            self.links.append([text or self._title.strip(), self._href, self._block])
            self._href = None
        if tag in BLOCK_TAGS and self._blocks:
            for i in range(len(self._blocks) - 1, -1, -1):
                if self._blocks[i][0] == tag:
                    del self._blocks[i:]
                    break

    def results(self):
        out = []
        for text, href, block in self.links:
            ctx = ""
            if block is not None and block[2][0] <= 3:
                ctx = " ".join(" ".join(block[1]).split())[:400]
            out.append((text, href, ctx))
        return out


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"})


def get(url, stream=False):
    try:
        r = SESSION.get(url, timeout=TIMEOUT, stream=stream)
    except requests.exceptions.SSLError:
        # Several government sites have broken certificate chains.
        r = SESSION.get(url, timeout=TIMEOUT, stream=stream, verify=False)
    r.raise_for_status()
    return r


def fetch_page(url):
    r = get(url)
    ctype = r.headers.get("content-type", "")
    if "charset" not in ctype.lower():
        r.encoding = r.apparent_encoding or "utf-8"
    if "html" not in ctype and "<a" not in r.text[:5000].lower():
        raise ValueError(f"not an HTML page ({ctype})")
    return r.url, r.text


def links_on(page_url, html):
    p = LinkParser()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001
        pass
    out = []
    for text, href, ctx in p.results():
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absu = urllib.parse.urljoin(page_url, href.strip())
        if absu.startswith(("http://", "https://")):
            out.append((text, absu, ctx))
    return out


GENERIC = re.compile(r"(click\s+here|download|view|read\s+more|details?|pdf|new|link|here|apply(\s+online)?|"
                     r"advertisement|notification|notice|corrigendum|addendum|english|hindi|more|open|\d+(\.\d+)?\s*(kb|mb))\W*", re.I)


def label_for(text, url, ctx=""):
    """Words to judge a link by: its own text, else its row/item's text, else its file name."""
    name = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])
    name = re.sub(r"[_\-+]+", " ", name.rsplit(".", 1)[0])
    own = text if text and len(text) > 3 and not GENERIC.fullmatch(text) else ""
    if own and is_notice(own):
        return own
    if ctx and len(ctx) > len(own) and is_notice(ctx):
        return ctx
    if own:
        return own
    return f"{text} – {name}".strip(" –") if text else name


def tidy(label):
    """Drop serial numbers, 'click here'-style words and file sizes from a label."""
    t = re.sub(r"^\s*(\d+|[ivx]+)\s*[.)]?\s+", "", label, flags=re.I)
    t = re.sub(r"\s*\(?\b\d+(\.\d+)?\s*(kb|mb)\)?", "", t, flags=re.I)
    t = re.sub(r"(\s*[-–|:]?\s*(click\s+here|download|view|read\s+more|apply\s+online|new|pdf|link|here)\W*)+$", "", t, flags=re.I)
    return " ".join(t.split())


YEAR_RX = re.compile(r"(?<!\d)(20[12]\d)(?!\d)")


def year_hint(*texts):
    years = [int(y) for t in texts if t for y in YEAR_RX.findall(t)]
    years = [y for y in years if 2010 <= y <= 2035]
    return max(years) if years else None


def is_notice(label):
    norm = re.sub(r"[_+]+", " ", label)
    return bool(INCLUDE.search(norm)) and not EXCLUDE.search(norm)


def site_key(u):
    h = (urllib.parse.urlparse(u).hostname or "").lower()
    parts = h.split(".")
    n = 3 if h.endswith((".ac.in", ".gov.in", ".edu.in", ".org.in", ".nic.in", ".res.in", ".co.in")) else 2
    return ".".join(parts[-n:])


def scan_source(src):
    res = {"name": src["name"], "state": src["state"], "type": src.get("type", ""), "url": src["url"],
           "ok": False, "error": "", "pagesChecked": 0, "matches": 0, "found": []}
    try:
        final_url, html = fetch_page(src["url"])
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {str(e)[:150]}"
        return res
    res["ok"] = True
    pages = [(final_url, html)]
    if src.get("follow"):
        seen, follow = {final_url}, []
        for text, u, _ctx in links_on(final_url, html):
            if len(follow) >= MAX_FOLLOW:
                break
            low = u.lower().split("?")[0]
            if u in seen or site_key(u) != site_key(final_url) or low.endswith((".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx", ".xls", ".xlsx", ".zip")):
                continue
            if FOLLOW.search(text or "") or FOLLOW.search(urllib.parse.urlparse(u).path):
                seen.add(u)
                follow.append(u)
        for u in follow:
            try:
                pages.append(fetch_page(u))
            except Exception:  # noqa: BLE001
                pass
    found = {}
    for page_url, page_html in pages:
        res["pagesChecked"] += 1
        for text, u, ctx in links_on(page_url, page_html):
            label = label_for(text, u, ctx)
            if not is_notice(label):
                continue
            label = tidy(label) or label
            key = hashlib.sha1((u + "|" + label.lower()).encode()).hexdigest()[:14]
            found[key] = {"id": key, "title": label[:240], "url": u, "page": page_url,
                          "yearHint": year_hint(label, urllib.parse.unquote(u))}
    res["found"] = list(found.values())
    res["matches"] = len(found)
    return res


# ---------------------------------------------------------------- reading notices
def run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)


def pdf_text(path, allow_ocr):
    text, used_ocr = "", False
    if shutil.which("pdftotext"):
        p = run(["pdftotext", "-layout", "-l", "8", path, "-"])
        text = p.stdout.decode("utf-8", "ignore")
    if len(re.sub(r"\s+", "", text)) < 200 and allow_ocr and shutil.which("pdftoppm") and shutil.which("tesseract"):
        with tempfile.TemporaryDirectory() as td:
            run(["pdftoppm", "-r", "150", "-f", "1", "-l", "3", "-png", path, os.path.join(td, "p")], timeout=180)
            parts = []
            for img in sorted(os.listdir(td)):
                p = run(["tesseract", os.path.join(td, img), "-", "-l", "eng"], timeout=180)
                parts.append(p.stdout.decode("utf-8", "ignore"))
            text = "\n".join(parts)
            used_ocr = True
    return text, used_ocr


def read_notice(url, allow_ocr):
    """Return (text, kind, used_ocr) for a notice link."""
    r = get(url, stream=True)
    ctype = (r.headers.get("content-type") or "").lower()
    data = b""
    for chunk in r.iter_content(65536):
        data += chunk
        if len(data) > MAX_DOC_BYTES:
            raise ValueError("document too large")
    is_pdf = "pdf" in ctype or data[:5] == b"%PDF-" or url.lower().split("?")[0].endswith(".pdf")
    if is_pdf:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
            fh.write(data)
            path = fh.name
        try:
            text, ocr = pdf_text(path, allow_ocr)
        finally:
            os.unlink(path)
        return text, "pdf", ocr
    if "html" in ctype or data[:200].lower().find(b"<html") >= 0 or b"<a" in data[:4000].lower():
        tp = TextParser()
        tp.feed(data.decode(r.encoding or "utf-8", "ignore"))
        return " ".join(" ".join(tp.parts).split()), "html", False
    return "", ctype.split(";")[0] or "other", False


def parse_date(m):
    try:
        if m.group(1):
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            y = y + 2000 if y < 100 else y
        elif m.group(4):
            d, mo, y = int(m.group(4)), MONTHS[m.group(5)[:3].lower()], int(m.group(6))
        else:
            mo, d, y = MONTHS[m.group(7)[:3].lower()], int(m.group(8)), int(m.group(9))
        return dt.date(y, mo, d)
    except Exception:  # noqa: BLE001
        return None


def find_last_date(text, seen_on):
    """Best guess at the last date to apply: a date shortly after a 'last date' phrase."""
    lo, hi = seen_on - dt.timedelta(days=45), seen_on + dt.timedelta(days=150)
    best = None
    for k in LASTDATE_KEY.finditer(text):
        window = text[k.end(): k.end() + 160]
        for m in DATE_RX.finditer(window):
            d = parse_date(m)
            if d and lo <= d <= hi:
                if best is None or d > best:
                    best = d
                break
    return best.isoformat() if best else ""


def tag(text, title):
    blob = f"{title}\n{text}"
    clean = NOT_SUBJECT.sub(" ", blob)
    posts = [name for name, rx in POSTS if rx.search(blob)]
    basis = [name for name, rx in BASIS if rx.search(blob)]
    subjects = []
    for name, rx, strict in SUBJECT_RX:
        if rx.search(clean):
            subjects.append(name)
    # A notice listing almost every subject is usually a general list; keep it but mark it.
    return posts, basis, subjects


def analyse(item, allow_ocr, today):
    try:
        text, kind, ocr = read_notice(item["url"], allow_ocr)
    except Exception as e:  # noqa: BLE001
        text, kind, ocr = "", "unreadable", False
        item["readError"] = f"{type(e).__name__}: {str(e)[:100]}"
    seen_on = dt.date.fromisoformat(item.get("firstSeen") or today)
    title = item["title"]
    if kind == "pdf":
        # A PDF is the notice itself: read posts, subjects and dates from its text.
        posts, basis, subjects = tag(text, title)
        if len(subjects) > 30:
            # Lists nearly every subject: a general notice or a prospectus. Trust the title only.
            subjects = tag("", title)[2]
        last = find_last_date(text, seen_on) or find_last_date(title, seen_on)
    else:
        # A web page carries menus and other notices too, so only its link text is trusted.
        posts, basis, subjects = tag("", title)
        last = find_last_date(title, seen_on)
    item.update({
        "analysed": True,
        "analysisVersion": ANALYSIS_VERSION,
        "docKind": kind,
        "ocr": ocr,
        "textRead": kind == "pdf" and len(text.strip()) > 150,
        "posts": posts,
        "basis": basis,
        "subjects": subjects,
        "lastDate": last,
        "excerpt": " ".join(text.split())[:320] if kind == "pdf" else "",
        "yearHint": year_hint(title, urllib.parse.unquote(item["url"]), text[:3000] if kind == "pdf" else "") or item.get("yearHint"),
    })
    return item


# ---------------------------------------------------------------- main
def main():
    sources = json.load(open(SOURCES_FILE, encoding="utf-8"))["sources"]
    try:
        old = json.load(open(NOTICES_FILE, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        old = {"items": [], "health": []}
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now = dt.datetime.now(ist)
    today = now.date().isoformat()
    items = {it["id"]: it for it in old.get("items", [])}
    checked_before = {h["name"] for h in old.get("health", []) if h.get("ok")} | set(old.get("everRead", []))

    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(scan_source, sources))
    print(f"Scanned {len(results)} sources in {time.time() - START:.0f}s")

    new_count = 0
    for res in results:
        first_time = res["name"] not in checked_before
        for f in res["found"]:
            if f["id"] in items:
                it = items[f["id"]]
                it["lastSeen"] = today
                it["url"] = f["url"]
                it.update({"source": res["name"], "state": res["state"], "type": res["type"]})
                if not it.get("analysed"):
                    it["yearHint"] = f.get("yearHint")
            else:
                items[f["id"]] = {**f, "source": res["name"], "state": res["state"], "type": res["type"],
                                  "firstSeen": today, "lastSeen": today, "baseline": first_time}
                if not first_time:
                    new_count += 1

    # Read notices that haven't been read yet: new ones first, then the rest.
    todo = [it for it in items.values()
            if it.get("lastSeen") == today and (not it.get("analysed") or it.get("analysisVersion") != ANALYSIS_VERSION)]
    todo.sort(key=lambda it: it.get("firstSeen", ""), reverse=True)   # newest first
    todo.sort(key=lambda it: it.get("baseline", False))                # then new before baseline
    todo = todo[:ANALYSE_PER_RUN]
    ocr_left = OCR_PER_RUN
    analysed = 0

    def work(it, allow_ocr):
        return analyse(it, allow_ocr, today)

    # First pass without OCR (fast, in parallel); then OCR for scanned PDFs, a few per run.
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(work, it, False): it for it in todo}
        for fut in cf.as_completed(futs):
            analysed += 1
            if time.time() - START > TIME_BUDGET:
                break
    for it in todo:
        if time.time() - START > TIME_BUDGET or ocr_left <= 0:
            break
        if it.get("analysed") and it.get("docKind") == "pdf" and not it.get("textRead"):
            ocr_left -= 1
            analyse(it, True, today)

    cutoff = (now.date() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    kept = [it for it in items.values()
            if it.get("lastSeen", today) >= cutoff and (it.get("verifiedRule") or is_notice(it.get("title", "")))]
    kept.sort(key=lambda it: (it.get("firstSeen", ""), it.get("source", "")), reverse=True)

    health = [{k: r[k] for k in ("name", "state", "type", "url", "ok", "error", "pagesChecked", "matches")}
              for r in results]
    ever = sorted(checked_before | {r["name"] for r in results if r["ok"]})
    out = {
        "checkedAt": now.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
        "checkedDate": today,
        "sourcesOk": sum(1 for r in results if r["ok"]),
        "sourcesTotal": len(results),
        "newToday": new_count,
        "analysedToday": analysed,
        "waitingToBeRead": sum(1 for it in kept if it.get("lastSeen") == today and
                               (not it.get("analysed") or it.get("analysisVersion") != ANALYSIS_VERSION)),
        "everRead": ever,
        "subjectNames": [n for n, _, _ in SUBJECTS],
        "postNames": [n for n, _ in POSTS],
        "items": kept,
        "health": health,
    }
    with open(NOTICES_FILE, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"Read {out['sourcesOk']}/{out['sourcesTotal']} sources; {new_count} new notices; "
          f"{analysed} notices analysed; {out['waitingToBeRead']} waiting; {len(kept)} tracked; "
          f"{time.time() - START:.0f}s")
    for r in results:
        if not r["ok"]:
            print(f"  [ERR] {r['name']}: {r['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
