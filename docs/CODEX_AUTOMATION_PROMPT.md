# Codex Automation Prompt

This document is the operating contract for the primary Codex-native daily
research run. Use it only after `feature/codex-automation-mode` has merged into
the repository's default branch. Codex performs retrieval, scientific triage,
lawful OA evidence review, and detailed journal-club analysis. GitHub Actions
then performs deterministic Pages publishing and the short WeChat notification.

The existing DeepSeek/OpenAI-compatible workflow remains intact as a **manual
benchmarking/fallback** workflow. A scheduled Codex run must never edit it.

## Schedule

Create a Codex Automation for approximately **08:45 Asia/Shanghai** (00:45
UTC). GitHub Actions does not perform a daily scientific-analysis run; it is
triggered only after Codex pushes a validated `codex-output/*.json` report to
`main`.

## Scheduled prompt

```text
Run today's PYGL Research Radar Codex-native analysis in the
pygl-research-radar repository.

Follow docs/CODEX_AUTOMATION_PROMPT.md exactly.

Run:
git pull
→ codex-prepare
→ scientific triage
→ codex-hydrate
→ detailed review
→ codex-validate
→ commit only sanitized codex-output
→ git push

Do not modify production source/config/tests.
If validation or retrieval fails, stop and report the failure.
```

## Required run contract

1. Start with `git pull --ff-only`. Do not start if the checkout is dirty.
2. Run `python -m pygl_radar codex-prepare --config config.yaml`; if the local
   config is absent, use `config.example.yaml`.
3. Read the full `work/YYYY-MM-DD/candidates.json` pool before triage. Use the
   frozen profile embedded in `codex_instructions.md`, and weight experimental
   logic more than keyword overlap. Keep approximately 10–15 papers in
   `shortlist.json`, using only known `paper_id` values.
4. Run `python -m pygl_radar codex-hydrate --workspace work/YYYY-MM-DD
   --shortlist work/YYYY-MM-DD/shortlist.json`. Use only lawful PMC, Europe
   PMC, Unpaywall, or explicitly supplied OA publisher/repository links. Never
   bypass a paywall, CAPTCHA, Cloudflare, or subscription gate.
5. Write `work/YYYY-MM-DD/codex_reviewed.json` for the final Top 3–5 papers.
   Include all six scores and the detailed journal-club schema described in
   `codex_instructions.md`. Use `NOT_EVALUABLE` for missing support.
6. Run `python -m pygl_radar codex-validate --workspace work/YYYY-MM-DD`.
   Only a successful validation may create `codex-output/YYYY-MM-DD.json` and
   `codex-output/YYYY-MM-DD.md`.
7. Inspect `git status --short`. Stage and commit only the matching sanitized
   pair, for example `git add codex-output/YYYY-MM-DD.json
   codex-output/YYYY-MM-DD.md && git commit -m "Publish Codex report
   YYYY-MM-DD"`, then `git push`. GitHub Actions validates the committed JSON
   again before it builds Pages or notifies WeChat.

## Evidence and safety boundaries

- `ABSTRACT_ONLY` cannot contain figure-number interpretation, dose, sample
  size, detailed protocol, or an unsupported detailed rescue result.
- Figure evidence modes are explicit: `FIGURE_VISUALLY_READ`,
  `FIGURE_LEGEND_ONLY`, `RESULTS_TEXT_ONLY`, and `ABSTRACT_ONLY`. Claim visual
  inspection only when an accessible local image was actually inspected.
- Do not infer Western blot intensity, microscopy morphology, scatter
  distributions, or other image details from text.
- Raw PDFs and raw full text remain local-only under `work/` and must never be
  placed in `codex-output/`, committed, or copied into Pages.
- The scheduled Automation may modify only `work/` and `codex-output/`.
  Production Python, config, tests, GitHub Actions, Pages, Issue, state, and
  notifier files are read-only during the run.
- Never access, request, print, or store `WECHAT_APP_SECRET`, `WECHAT_APP_ID`,
  `WECHAT_OPEN_ID`, or `WECHAT_TEMPLATE_ID`. Those values exist only as GitHub
  Actions Secrets in the deterministic notification job.
- Never commit `work/`, raw PDFs, raw full text, credentials, secrets, or any
  file outside the one matching `codex-output/YYYY-MM-DD.json` and `.md` pair.
- On any retrieval, validation, or program error, stop and report the failure
  in the Codex review queue. Do not auto-patch production source.

The output records `review_provider = "codex-automation"` and
`review_mode = "codex-native"`; it intentionally does not name a specific
underlying model. The JSON is the canonical publishable report. Its Markdown
companion is a GitHub-readable fallback; Pages is generated from JSON only.
