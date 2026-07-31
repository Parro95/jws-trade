# JWS Trading — trade.journeywithshannon.com

Free crypto trading education page. Static site — no build step.

## Files
- `index.html` — the whole site
- `assets/` — emblem, favicon, Shannon photo, social share card

## GitHub → Netlify auto-deploy (one-time setup)
1. Create the repo: github.com → New repository → name it **jws-trade** (public or private).
2. Upload everything in this bundle: **index.html, README.md, robots.txt, sitemap.xml, and the assets folder** (drag them all in together so the folder structure is kept) → Commit.
3. Link the EXISTING Netlify site to it (don't create a new site — this keeps your domain + SSL):
   Netlify → **jws-trade** project → **Site configuration → Build & deploy → Continuous deployment → Link repository** → GitHub → pick **jws-trade** → leave build command empty, publish directory `.` → Save.
4. Netlify deploys immediately from the repo. From now on, **every commit auto-deploys** — no more zip dragging.

## Editing after that
Change a referral code, book, or video by editing `index.html` on GitHub (pencil icon) → Commit → live in ~30 seconds.
The referral codes + video/book lists live in the CONFIG block near the bottom of `index.html`.

## Logo
The header logo + favicon load from the shared repo: `cdn.jsdelivr.net/gh/Parro95/jws-assets/logo.png` (fallback to bundled `assets/emblem.png`).
