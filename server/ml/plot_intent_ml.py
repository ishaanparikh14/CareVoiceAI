# -*- coding: utf-8 -*-
"""
plot_intent_ml.py — run the trained multilingual DistilBERT intent classifier
on the held-out test set and generate evaluation graphs:

  1. confusion_matrix.png            — raw-count confusion matrix (heatmap)
  2. confusion_matrix_normalized.png — row-normalized (recall) confusion matrix
  3. per_class_metrics.png           — precision / recall / f1 bar chart per intent
  4. per_language_accuracy.png       — overall + per-language accuracy bars
  5. class_support.png               — test-set sample count per intent

All PNGs are written to server/ml/plots/.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    precision_recall_fscore_support,
)

sys.path.insert(0, str(Path(__file__).parent))
from intent_labels import INTENTS  # noqa: E402

MODEL_DIR    = Path(__file__).parent.parent / "storage" / "models" / "intent_ml"
PREPARED_DIR = Path(__file__).parent / "data" / "prepared_ml"
PLOTS_DIR    = Path(__file__).parent / "plots"
PLOTS_DIR.mkdir(parents=True, exist_ok=True)


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


def plot_confusion(y_true, y_pred, labels, names, normalize, out_path, title):
    cm = confusion_matrix(y_true, y_pred, labels=labels)
    if normalize:
        with np.errstate(all="ignore"):
            cm_disp = cm.astype(float) / cm.sum(axis=1, keepdims=True)
            cm_disp = np.nan_to_num(cm_disp)
        fmt, cmap, vmax = ".2f", "Blues", 1.0
    else:
        cm_disp, fmt, cmap, vmax = cm, "d", "Blues", None

    plt.figure(figsize=(10, 8))
    sns.heatmap(cm_disp, annot=True, fmt=fmt, cmap=cmap, vmax=vmax,
                xticklabels=names, yticklabels=names,
                cbar_kws={"label": "proportion" if normalize else "count"},
                linewidths=0.5, linecolor="#dddddd", square=True)
    plt.xlabel("Predicted intent")
    plt.ylabel("True intent")
    plt.title(title)
    plt.xticks(rotation=45, ha="right")
    plt.yticks(rotation=0)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()
    print(f"  wrote {out_path.name}")


def plot_per_class_metrics(y_true, y_pred, labels, names, out_path):
    p, r, f1, support = precision_recall_fscore_support(
        y_true, y_pred, labels=labels, zero_division=0)
    x = np.arange(len(names))
    w = 0.25
    plt.figure(figsize=(12, 6))
    plt.bar(x - w, p, w, label="Precision", color="#4C72B0")
    plt.bar(x,     r, w, label="Recall",    color="#55A868")
    plt.bar(x + w, f1, w, label="F1-score",  color="#C44E52")
    plt.xticks(x, names, rotation=45, ha="right")
    plt.ylim(0, 1.05)
    plt.ylabel("Score")
    plt.title("Per-class precision / recall / F1 (test set)")
    plt.legend(loc="lower right")
    plt.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()
    print(f"  wrote {out_path.name}")


def plot_per_language_accuracy(df, out_path):
    rows = [("overall", accuracy_score(df["label"], df["pred"]), len(df))]
    for lang in ["en", "hi", "kn", "de"]:
        sub = df[df["lang"] == lang]
        if len(sub):
            rows.append((lang, accuracy_score(sub["label"], sub["pred"]), len(sub)))
    names = [f"{n}\n(n={c})" for n, _, c in rows]
    vals = [a for _, a, _ in rows]
    colors = ["#333333"] + ["#4C72B0"] * (len(rows) - 1)
    plt.figure(figsize=(9, 6))
    bars = plt.bar(names, vals, color=colors)
    for b, v in zip(bars, vals):
        plt.text(b.get_x() + b.get_width() / 2, v + 0.01, f"{v:.3f}",
                 ha="center", va="bottom", fontsize=10)
    plt.ylim(0, 1.08)
    plt.ylabel("Accuracy")
    plt.title("Overall and per-language accuracy (test set)")
    plt.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()
    print(f"  wrote {out_path.name}")


def plot_class_support(y_true, labels, names, out_path):
    counts = [int(np.sum(np.array(y_true) == l)) for l in labels]
    plt.figure(figsize=(11, 6))
    bars = plt.bar(names, counts, color="#8172B3")
    for b, c in zip(bars, counts):
        plt.text(b.get_x() + b.get_width() / 2, c + 0.3, str(c),
                 ha="center", va="bottom", fontsize=10)
    plt.xticks(rotation=45, ha="right")
    plt.ylabel("Number of test samples")
    plt.title("Test-set class distribution (support per intent)")
    plt.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()
    print(f"  wrote {out_path.name}")


def main():
    from transformers import AutoTokenizer, AutoModelForSequenceClassification
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")
    print("Loading model ...")
    tokenizer = AutoTokenizer.from_pretrained(str(MODEL_DIR))
    model = AutoModelForSequenceClassification.from_pretrained(str(MODEL_DIR)).to(device)
    model.eval()

    df = pd.read_csv(PREPARED_DIR / "test.csv")
    df["text"] = df["text"].astype(str)
    print(f"Test set: {len(df)} samples")
    df["pred"] = predict(df["text"].tolist(), model, tokenizer, device)

    present = sorted(set(df["label"]) | set(df["pred"]))
    names = [INTENTS[i] for i in present]
    y_true, y_pred = df["label"].tolist(), df["pred"].tolist()

    print("Generating plots ...")
    plot_confusion(y_true, y_pred, present, names, False,
                   PLOTS_DIR / "confusion_matrix.png",
                   "Confusion matrix — intent classifier (test set)")
    plot_confusion(y_true, y_pred, present, names, True,
                   PLOTS_DIR / "confusion_matrix_normalized.png",
                   "Normalized confusion matrix (recall) — intent classifier")
    plot_per_class_metrics(y_true, y_pred, present, names,
                           PLOTS_DIR / "per_class_metrics.png")
    plot_per_language_accuracy(df, PLOTS_DIR / "per_language_accuracy.png")
    plot_class_support(y_true, present, names,
                       PLOTS_DIR / "class_support.png")

    acc = accuracy_score(y_true, y_pred)
    print(f"\nOverall test accuracy: {acc:.4f}  (n={len(df)})")
    print(f"All plots saved to: {PLOTS_DIR}")


if __name__ == "__main__":
    main()
