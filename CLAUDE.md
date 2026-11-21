# Working on PetroKenya

## Saving work
- Commit and push **every** update as soon as it's done and checked — never leave changes
  uncommitted. One commit per change, with a clear message saying what changed and why.
- Push to the branch the live site runs from: `claude/petrokenya-station-dashboard-7djoop`.
  Render redeploys https://petrokenya.onrender.com automatically from it, so every push goes live.
  The owner chose to keep Render on this branch (not `main`).
- **All commits are merged to `main`** (the owner's rule): after every push, open a pull request
  from this branch into `main` and merge it, so `main` always matches this branch. Never push
  directly to `main`. The same applies to the copy in `dariusttt/dariusttt` when it is updated.

## Before each commit
- Run the tests: `cd backend && python3 -m unittest discover -s tests` (all must pass).
- When frontend files change, bump the `?v=N` in `frontend/index.html` and `frontend/sw.js`
  and `VERSION` in `frontend/sw.js`, so installed phones pick up the update.
- When the app changes, rebuild the website preview: `python3 preview/build_preview.py <out.html>`
  (keep `preview/mock-api.js` in step with the server).
