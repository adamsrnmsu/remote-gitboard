# laya-bench

This measured whether [Laya](https://huggingface.co/convaiinnovations/laya) picks an issue's `type::` label faster or better than Claude does, on the same 300 issues. The code (`laya-bench`) was deleted on 2026-10-04 when pi_suite was dissolved; this page keeps the result. `bench.py` and the data were deleted with it, so the commands and code names below (`bench.py laya-variants`, `CRITERIA`) refer to code that is gone. The conclusion stands: Laya was less accurate than every Claude run.

## Result (30 September 2026, Intel i7-9750H MacBook)

| system | accuracy | s per card |
|---|---|---|
| Laya | 46% | 0.50 one at a time, 0.17 batched |
| Claude Haiku 4.5, thinking off | 63% | 0.07 |
| Claude Haiku 4.5 | 69% | 0.86 |
| Claude Sonnet 5.5 | 71% | 0.08 |
| Claude Opus 5.5 | 70% | 0.06 |

Laya was less accurate than every Claude run and, on this machine, slower
than all but Haiku with thinking on. Its confidence does not rescue it: the
44% of cards it answers at 0.5 or above are 56% right.

Laya is sensitive to phrasing. Five other phrasings and checkpoints (the
state as a `title` field, shorter definitions, the multilingual and
typed-decisions checkpoints) scored 50% to 58% on the same issues
(`bench.py laya-variants`). The best
of those was picked on the test set, so it flatters Laya, and it is still
below Sonnet and Opus by more than the margin of error.

## What is measured

- **Data.** 300 issues from `gitlab-org/gitlab`, 100 per class, created after
  1 July 2026. The truth is the `type::` label a GitLab maintainer set:
  `type::bug` is `bug`, `type::feature` is `task`, `type::maintenance` is
  `chore`. `fetch --project` points it at another public project.
- **Input.** The title only, for both systems. GitLab's issue templates put
  the answer in the description ("Steps to reproduce"), and gitboard cards
  are mostly a title.
- **Labels.** Both systems get the same three definitions (`CRITERIA` in
  `bench.py`). They were written once and not tuned against the results.
- **Laya speed.** Warm, one card per call (median and p95), and batched. Load
  and warm-up are reported separately: a process pays them once.
- **Claude speed.** API time per call divided by the 20 titles in it. CLI
  start-up is excluded, because inside `/board` Claude is already running.
  `--no-thinking` sets `MAX_THINKING_TOKENS=0`.

`type::verify` is left out: `gitboard ingest` sets it by rule, nothing guesses it.

## What it does not show

- These are GitLab's own issues, not your board. Titles on a small team's
  board are shorter and the split between task and chore is a local habit.
- Maintainer labels are noisy, and a title alone is sometimes not enough to
  tell. Neither system can reach 100%.
- 300 issues gives about ±5 points on accuracy. Differences smaller than
  that are not differences.
- Laya's speed is this Intel Mac's GPU through MPS, at full precision. A CUDA
  GPU or Apple Silicon is faster; the model card quotes 33 ms per card on a T4.

## Full report

## Laya vs Claude: issue type from title (300 issues)

| system | accuracy | macro F1 | recall bug / task / chore | s per card | USD per card |
|---|---|---|---|---|---|
| Claude claude-haiku-4-5-20251001 (thinking off) | 62.7% ± 5.5% | 0.613 | 71% / 84% / 33% | 0.065 | 0.00007 |
| Claude claude-haiku-4-5-20251001 | 69.3% ± 5.2% | 0.686 | 79% / 85% / 44% | 0.859 | 0.00053 |
| Laya (local) | 46.3% ± 5.6% | 0.463 | 49% / 58% / 32% | 0.503 | 0 |
| Claude claude-opus-5-5 | 70.0% ± 5.2% | 0.703 | 73% / 70% / 67% | 0.061 | 0.00053 |
| Claude claude-sonnet-5-5 | 70.7% ± 5.2% | 0.706 | 74% / 81% / 57% | 0.077 | 0.00026 |

### Laya timing

- one card at a time: median 0.503 s, p95 0.833 s
- batched: 0.175 s per card (same answer as one-at-a-time on 100.0% of cards)
- cold start: 44 s load + 5 s warm-up, paid once per process

### Laya by confidence

Accuracy on the cards Laya answers at or above a threshold; the rest would go to Claude.

| min confidence | cards kept | accuracy on kept |
|---|---|---|
| 0.0 | 100% | 46.3% |
| 0.5 | 44% | 56.1% |
| 0.7 | 10% | 60.0% |
| 0.9 | 0% | 0.0% |

### Laya against each Claude run, card by card

| Claude run | both right | only Laya right | only Claude right | both wrong |
|---|---|---|---|---|
| Claude claude-haiku-4-5-20251001 (thinking off) | 102 | 37 | 86 | 75 |
| Claude claude-haiku-4-5-20251001 | 111 | 28 | 97 | 64 |
| Claude claude-opus-5-5 | 108 | 31 | 102 | 59 |
| Claude claude-sonnet-5-5 | 112 | 27 | 100 | 61 |

Claude seconds are API time per call divided by the batch size; Laya seconds are the median of one card per call (its batched figure is under Laya timing). Accuracy is ± a 95% interval. See README.md for what this does and does not show.
