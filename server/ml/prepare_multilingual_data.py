# -*- coding: utf-8 -*-
"""
prepare_multilingual_data.py — build the trilingual (en/hi/kn) intent training set.

Combines:
  1. Kaggle Medical Speech English phrases (mapped to 9 intents)  [lang=en]
  2. English augmentation phrases (augment_data.py)               [lang=en]
  3. Curated trilingual phrases (trilingual_phrases.py)           [lang=en/hi/kn]

Writes stratified train/val/test CSVs with columns [text, label, intent, lang]
so evaluation can report per-language accuracy.

Output: server/ml/data/prepared_ml/{train,val,test}.csv
"""

import re
import sys
from pathlib import Path

import pandas as pd
from sklearn.model_selection import train_test_split

sys.path.insert(0, str(Path(__file__).parent))
from intent_labels import map_symptom_to_intent, INTENTS, LABEL2ID  # noqa: E402
from augment_data import get_augmentation_rows                       # noqa: E402
from trilingual_phrases import get_trilingual_rows                   # noqa: E402
from augment_multilingual import get_augmented_multilingual          # noqa: E402

DATA_DIR     = Path(__file__).parent / "data"
PREPARED_DIR = DATA_DIR / "prepared_ml"
PREPARED_DIR.mkdir(parents=True, exist_ok=True)


def _clean_text(t: str) -> str:
    """Clean text. Keep Unicode letters (Devanagari/Kannada) — only strip control chars."""
    t = str(t).strip().lower()
    t = re.sub(r"\s+", " ", t)
    # Remove characters that are clearly noise, but KEEP all Unicode letters/marks
    t = re.sub(r"[^\w\s'.,?!\u0900-\u097F\u0C80-\u0CFF-]", "", t)
    return t.strip()


def _load_kaggle_english() -> pd.DataFrame:
    csv = list(DATA_DIR.rglob("overview-of-recordings.csv"))
    if not csv:
        print("WARNING: Kaggle CSV not found — proceeding with augmentation only")
        return pd.DataFrame(columns=["text", "intent", "lang"])
    df = pd.read_csv(csv[0])
    df = df[["phrase", "prompt"]].rename(columns={"phrase": "text", "prompt": "symptom"})
    df["text"]   = df["text"].map(_clean_text)
    df["intent"] = df["symptom"].map(map_symptom_to_intent)
    df["lang"]   = "en"
    return df[["text", "intent", "lang"]]


def main():
    frames = []

    # 1. Kaggle English
    kaggle = _load_kaggle_english()
    frames.append(kaggle)

    # 2. English augmentation
    aug = pd.DataFrame(get_augmentation_rows(), columns=["text", "intent"])
    aug["text"] = aug["text"].map(_clean_text)
    aug["lang"] = "en"
    frames.append(aug[["text", "intent", "lang"]])

    # 3. Trilingual curated
    tri_rows = get_trilingual_rows()
    tri = pd.DataFrame(tri_rows, columns=["text", "intent", "lang"])
    tri["text"] = tri["text"].map(_clean_text)
    frames.append(tri[["text", "intent", "lang"]])

    # 3b. Paraphrase-augment Hindi & Kannada for more per-language signal
    aug_ml = pd.DataFrame(get_augmented_multilingual(tri_rows),
                          columns=["text", "intent", "lang"])
    aug_ml["text"] = aug_ml["text"].map(_clean_text)
    frames.append(aug_ml[["text", "intent", "lang"]])

    df = pd.concat(frames, ignore_index=True)

    # Clean: drop empties/short, dedup
    before = len(df)
    df = df[df["text"].str.len() >= 3]
    df = df.drop_duplicates(subset=["text"]).reset_index(drop=True)
    df["label"] = df["intent"].map(LABEL2ID)

    # Report
    lines = []
    lines.append(f"Combined {before} -> {len(df)} rows after dedup")
    lines.append("\nPer-language counts:")
    lines.append(df["lang"].value_counts().to_string())
    lines.append("\nPer-intent counts:")
    lines.append(df["intent"].value_counts().to_string())

    # Stratify on (intent, lang) pair so each language keeps representation in every split
    df["stratum"] = df["intent"] + "_" + df["lang"]
    strat = df["stratum"] if df["stratum"].value_counts().min() >= 3 else df["label"]

    train_df, temp_df = train_test_split(
        df, test_size=0.20, random_state=42,
        stratify=strat if strat.value_counts().min() >= 2 else None,
    )
    strat2 = temp_df["stratum"] if temp_df["stratum"].value_counts().min() >= 2 else None
    val_df, test_df = train_test_split(
        temp_df, test_size=0.50, random_state=42, stratify=strat2
    )

    cols = ["text", "label", "intent", "lang"]
    for name, part in [("train", train_df), ("val", val_df), ("test", test_df)]:
        part[cols].to_csv(PREPARED_DIR / f"{name}.csv", index=False)
        lines.append(f"Wrote {len(part):5d} rows -> {name}.csv")

    report = "\n".join(lines)
    (Path(__file__).parent / "ml_prep_result.txt").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
