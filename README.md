# Faculty jobs in government universities & colleges – India

A website that lists Professor, Associate Professor, Assistant Professor and guest/contract faculty openings at government universities, colleges (through state commissions) and central institutes across India. Search by state, subject, post and institution type.

## How it updates

- Every morning (06:47 IST) a GitHub Action runs `check_notices.py`:
  1. It reads the official recruitment pages listed in `sources.json` (central universities, IITs, NITs, IISERs, AIIMS, state universities and every state's Public Service Commission / college service commission).
  2. It keeps links that look like faculty recruitment notices.
  3. It opens each new notice (PDF or web page; scanned PDFs are read with OCR) and picks out the posts, subjects, type of appointment and last date.
  4. It saves everything in `notices.json`, which the website reads.
- `openings.json` holds hand-checked openings (Haryana & Punjab, 20 Nov 2026) shown as "Verified details".
- `watch.json` lists big drives to keep an eye on.

Reading is automatic and best-effort. Always open the official notice before applying.

## Common tasks

- **Run the check now:** Actions → Daily notice check → Run workflow.
- **Add or fix a source:** edit `sources.json` (name, state, type, url, follow). `follow: true` also reads up to 5 recruitment/career pages linked from that page.
- **See which pages failed:** at the bottom of the website, "Which official pages are checked".

## Files

| File | What it is |
|---|---|
| `index.html` | The website |
| `notices.json` | Written by the daily check |
| `openings.json` | Hand-checked openings |
| `watch.json` | Big drives to watch |
| `sources.json` | Official pages the check reads |
| `check_notices.py` | The daily check |
| `.github/workflows/update.yml` | The daily schedule |
