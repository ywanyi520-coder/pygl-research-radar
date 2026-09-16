# Codex Automation Prompt

This document is the operating contract for the independent Codex-native
comparison run. It must be used only after the `feature/codex-automation-mode`
implementation has been merged into the repository's default branch. The
existing GitHub Actions DeepSeek/OpenAI-compatible workflow remains the
production pipeline and must not be disabled, reconfigured, or edited by a
scheduled run.

## Schedule

Create a Codex Automation for approximately **08:45 Asia/Shanghai** (00:45
UTC). GitHub Actions currently starts the DeepSeek production run at about
08:30 Asia/Shanghai. The offset makes the two independent runs easier to
compare and reduces resource contention.

## Scheduled prompt

```text
Run today's PYGL Research Radar Codex-native analysis in the
pygl-research-radar repository.

Follow docs/CODEX_AUTOMATION_PROMPT.md exactly.

Run:
codex-prepare
→ scientific triage
→ codex-hydrate
→ detailed review
→ codex-validate
→ write sanitized codex-output.

Do not modify production source/config/tests.
If validation or retrieval fails, stop and report the failure.
```

## Required run contract

1. Run `python -m pygl_radar codex-prepare --config config.yaml`; if the local
   config is absent, use `config.example.yaml`.
2. Read the full `work/YYYY-MM-DD/candidates.json` pool before triage. Use the
   frozen profile embedded in `codex_instructions.md`, and weight experimental
   logic more than keyword overlap. Keep approximately 10–15 papers in
   `shortlist.json`, using only known `paper_id` values.
3. Run `python -m pygl_radar codex-hydrate --workspace work/YYYY-MM-DD
   --shortlist work/YYYY-MM-DD/shortlist.json`. Use only lawful PMC, Europe
   PMC, Unpaywall, or explicitly supplied OA publisher/repository links. Never
   bypass a paywall, CAPTCHA, Cloudflare, or subscription gate.
4. Write `work/YYYY-MM-DD/codex_reviewed.json` for the final Top 3–5 papers.
   Include all six scores and the detailed journal-club schema described in
   `codex_instructions.md`. Use `NOT_EVALUABLE` for missing support.
5. Run `python -m pygl_radar codex-validate --workspace work/YYYY-MM-DD`.
   Only a successful validation may create `codex-output/YYYY-MM-DD.json` and
   `codex-output/YYYY-MM-DD.md`.

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
- On any retrieval, validation, or program error, stop and report the failure
  in the Codex review queue. Do not auto-patch production source.

The comparison output records `review_provider = "codex-automation"` and
`review_mode = "codex-native"`; it intentionally does not name a specific
underlying model.
