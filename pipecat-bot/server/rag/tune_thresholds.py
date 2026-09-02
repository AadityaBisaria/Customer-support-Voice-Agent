"""Threshold tuning: run against the committed corpus, copy numbers to .env.

    uv run python -m rag.tune_thresholds

Held-out probes:
- each entry's paraphrases queried against an index built WITHOUT that
  entry's paraphrase rows (question-only), so a paraphrase can't match itself;
- every unanswerable trap question and its paraphrases.

Recommends HIGH around the answerable p10 (most direct hits clear it) and
FLOOR above the strongest trap score (no trap ever grounds an answer).
"""

import json
from pathlib import Path

import numpy as np

from .embedder import embed
from .gate import decide
from .index import QAEntry, QAIndex

CORPUS = Path(__file__).parent / "corpus"


def main() -> None:
    data = json.loads((CORPUS / "qa.json").read_text(encoding="utf-8"))
    traps = json.loads((CORPUS / "unanswerable.json").read_text(encoding="utf-8"))

    # Held-out index (questions only): paraphrases act as unseen phrasings, a
    # lower bound on how answerable queries score.
    heldout = QAIndex(
        [QAEntry(id=e["id"], question=e["question"], answer=e["answer"]) for e in data["entries"]],
        embed_fn=embed,
    )
    # Full index: exactly what the bot searches at runtime — traps must be
    # judged against THIS one, since paraphrase rows can inflate their scores.
    full = QAIndex.load(CORPUS / "qa.json", embed_fn=embed)

    answerable_scores: list[float] = []
    misrouted = 0
    for e in data["entries"]:
        for probe in e.get("paraphrases", []):
            matches = heldout.search(heldout.embed_query(probe), top_k=1)
            answerable_scores.append(matches[0][1])
            if matches[0][0].id != e["id"]:
                misrouted += 1

    trap_scores: list[float] = []
    print("trap probes vs FULL runtime index (top match):")
    for t in traps["entries"]:
        for probe in [t["question"], *t.get("paraphrases", [])]:
            entry, score = full.search(full.embed_query(probe), top_k=1)[0]
            trap_scores.append(score)
            marker = " <-- HOT" if score >= 0.70 else ""
            print(f"  {score:.3f}  {t['id']} -> {entry.id}  {probe!r}{marker}")

    ans = np.array(answerable_scores)
    trp = np.array(trap_scores)

    print()
    print(f"answerable held-out probes: n={len(ans)}  misrouted-top1={misrouted}")
    print(
        f"  min={ans.min():.3f}  p10={np.percentile(ans, 10):.3f}  "
        f"p50={np.percentile(ans, 50):.3f}  max={ans.max():.3f}"
    )
    print(f"trap probes: n={len(trp)}")
    print(
        f"  min={trp.min():.3f}  p50={np.percentile(trp, 50):.3f}  "
        f"p90={np.percentile(trp, 90):.3f}  max={trp.max():.3f}"
    )

    # HIGH must sit above every trap score: no trap may ever reach the
    # "answer strictly from this" band. The gray zone in between lands in
    # MID, whose instruction already allows the model to deflect.
    high = float(trp.max()) + 0.03
    # FLOOR keeps clearly off-corpus queries deterministically deflected
    # without swallowing too many genuine unseen phrasings.
    floor = float(np.percentile(trp, 50))
    print()
    print(f"recommended RAG_THRESHOLD_HIGH={high:.2f} (trap max {trp.max():.3f} + margin)")
    print(f"recommended RAG_THRESHOLD_FLOOR={floor:.2f} (trap p50)")
    print(
        f"check: answerable held-out probes below floor = "
        f"{int((ans < floor).sum())}/{len(ans)} (over-cautious deflections; "
        f"runtime scores run higher because paraphrases are indexed)"
    )
    print(
        f"check: traps reaching HIGH = {int((trp >= high).sum())}/{len(trp)} (must be 0); "
        f"traps in MID = {int(((trp >= floor) & (trp < high)).sum())}/{len(trp)} "
        f"(deflected by the MID instruction + evals)"
    )


if __name__ == "__main__":
    main()
