# Faculty jobs in government universities & colleges – India

A website that lists Professor, Associate Professor, Assistant Professor and guest/contract faculty openings at government universities, colleges (through state commissions) and central institutes across India. Search by state, subject, post and institution type.

## How it updates

Two checks run every day. Both use `check_notices.py`:

1. It reads the official recruitment pages listed in `sources.json` (central universities, IITs, NITs, IISERs, AIIMS, state universities and every state's Public Service Commission / college service commission).
2. It keeps links that look like faculty recruitment notices and drops unrelated ones (results, office orders, residency posts, project jobs…).
3. It opens each new notice — the PDF, or the advertisement PDF on a notice's own page — and picks out the posts, subjects, type of appointment and last date. Scanned PDFs are read with OCR where available.
4. It marks notices meant for people who already applied (interview calls, postponements, rank lists) as updates; the website hides them unless asked.

| Check | Where it runs | Sources | Writes |
|---|---|---|---|
| **Daily notice check** (`.github/workflows/update.yml`) | GitHub's servers, 06:47 IST | everything not marked `"runOn": "india"` | `notices.json` |
| **India check** (`.github/workflows/india.yml`) | a computer in India (self-hosted runner), 10:17 IST or whenever it is next switched on | sources marked `"runOn": "india"` — sites that refuse connections from abroad — plus any source that failed in GitHub's last run | `india.json` |

The website reads both files and merges them. `openings.json` holds hand-checked openings (Haryana & Punjab, 20 Nov 2026) shown as "Verified details"; `watch.json` lists big drives to keep an eye on.

Reading is automatic and best-effort. Always open the official notice before applying.

## The computer in India (self-hosted runner)

Set up once on a Mac in India:

1. On GitHub: **Settings → Actions → Runners → New self-hosted runner → macOS**. Copy the commands from the *Download* and *Configure* boxes into Terminal and run them (press Enter to accept each default question).
2. Instead of `./run.sh`, run `./svc.sh install` and then `./svc.sh start` in the same folder, so the runner starts by itself whenever you log in.
3. **Settings → Actions → General → Approval for running fork pull request workflows from contributors → Require approval for all external contributors → Save.** This keeps strangers' pull requests from running anything on your computer. Never approve a workflow run on a pull request from someone you don't know.

The India check only downloads this repository's code and a private copy of Python (via `uv`) into the runner's own folder (`actions-runner/_work`). To remove it later: `./svc.sh stop`, `./svc.sh uninstall`, then remove the runner under Settings → Actions → Runners and delete the `actions-runner` folder. GitHub removes a runner by itself if it hasn't connected for 14 days.

## Common tasks

- **Run a check now:** Actions → *Daily notice check* or *India check* → Run workflow.
- **Add or fix a source:** edit `sources.json` (name, state, type, url, follow, optional `"runOn": "india"`). `follow: true` also reads up to 5 recruitment/career pages linked from that page.
- **See which pages failed:** at the bottom of the website, "Which official pages are checked".
- **Run it on your own computer:** `pip install requests pypdfium2` then `python check_notices.py --group main` (or `--group india`).

## Files

| File | What it is |
|---|---|
| `index.html` | The website |
| `notices.json` | Written by the daily check on GitHub |
| `india.json` | Written by the India check |
| `openings.json` | Hand-checked openings |
| `watch.json` | Big drives to watch |
| `sources.json` | Official pages the checks read |
| `check_notices.py` | The checker |
| `.github/workflows/update.yml` | The daily schedule on GitHub |
| `.github/workflows/india.yml` | The daily schedule on the computer in India |
