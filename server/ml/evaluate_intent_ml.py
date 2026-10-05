# -*- coding: utf-8 -*-
"""
evaluate_intent_ml.py — evaluate the multilingual DistilBERT on the held-out
test set, reporting OVERALL accuracy and PER-LANGUAGE accuracy (en / hi / kn / de).
"""

import sys
from pathlib import Path

import pandas as pd
import torch
from sklearn.metrics import accuracy_score, classification_report

sys.path.insert(0, str(Path(__file__).parent))
from intent_labels import INTENTS  # noqa: E402

MODEL_DIR    = Path(__file__).parent.parent / "storage" / "models" / "intent_ml"
PREPARED_DIR = Path(__file__).parent / "data" / "prepared_ml"


def predict(texts, model, tokenizer, device):
    preds = []
    with torch.no_grad():
        for i in range(0, len(texts), 32):
            batch = texts[i:i + 32]
            enc = tokenizer(batch, truncation=True, max_length=64,
                            padding=True, return_tensors="pt").to(device)
            logits = model(**enc).logits
            preds.extend(torch.argmax(logits, dim=-1).cpu().numpy().tolist())
    return preds


def main():
    from transformers import AutoTokenizer, AutoModelForSequenceClassification
    device = "cuda" if torch.cuda.is_available() else "cpu"
    tokenizer = AutoTokenizer.from_pretrained(str(MODEL_DIR))
    model = AutoModelForSequenceClassification.from_pretrained(str(MODEL_DIR)).to(device)
    model.eval()

    df = pd.read_csv(PREPARED_DIR / "test.csv")
    df["text"] = df["text"].astype(str)
    preds = predict(df["text"].tolist(), model, tokenizer, device)
    df["pred"] = preds

    lines = []
    overall = accuracy_score(df["label"], df["pred"])
    lines.append(f"OVERALL test accuracy: {overall:.4f}  (n={len(df)})")
    lines.append("")
    lines.append("Per-language accuracy:")
    for lang in ["en", "hi", "kn", "de"]:
        sub = df[df["lang"] == lang]
        if len(sub) == 0:
            continue
        acc = accuracy_score(sub["label"], sub["pred"])
        lines.append(f"  {lang}: {acc:.4f}  (n={len(sub)})")

    lines.append("")
    present = sorted(set(df["label"]) | set(df["pred"]))
    lines.append(classification_report(
        df["label"], df["pred"], labels=present,
        target_names=[INTENTS[i] for i in present], zero_division=0))

    report = "\n".join(lines)
    (Path(__file__).parent / "ml_eval_result.txt").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
