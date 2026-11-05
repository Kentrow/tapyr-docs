## What and why

<!-- What changes, and why. Link the issue: "Closes #123". -->

## How it was tested

<!-- Checks run, pages looked at (light and dark), versions tried with `tools/site_tool.py publish` on a local gh-pages. -->

## Checklist

- [ ] No exported page is edited here: content fixes go to Kentrow/tapyr
- [ ] `python -m unittest discover -s tools` passes
- [ ] `zensical build --strict` has no warning and `tools/site_tool.py check-links` passes on a `prepare` of `main`
- [ ] A dependency or action change keeps everything pinned (exact versions, actions by SHA with the version comment)

## AI assistance

<!-- Did a coding agent help? Which one, and for what? -->

- [ ] No agent was used
- [ ] An agent helped, and I have reviewed and understood every change:
