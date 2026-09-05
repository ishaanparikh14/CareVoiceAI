# -*- coding: utf-8 -*-
"""
train_intent_ml.py — fine-tune multilingual DistilBERT (mBERT-distil) on the
trilingual (en/hi/kn) 9-intent dataset.

Model: distilbert-base-multilingual-cased — supports 100+ languages including
Hindi and Kannada. Saves to server/storage/models/intent_ml/.

Run:
    python train_intent_ml.py
"""

import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from datasets import Dataset
from sklearn.metrics import accuracy_score, f1_score
from transformers import (
    AutoTokenizer,
    AutoModelForSequenceClassification,
    TrainingArguments,
    Trainer,
    DataCollatorWithPadding,
)

sys.path.insert(0, str(Path(__file__).parent))
from intent_labels import INTENTS, LABEL2ID, ID2LABEL  # noqa: E402

BASE_MODEL   = "distilbert-base-multilingual-cased"
PREPARED_DIR = Path(__file__).parent / "data" / "prepared_ml"
OUTPUT_DIR   = Path(__file__).parent.parent / "storage" / "models" / "intent_ml"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def load_split(name: str) -> Dataset:
    df = pd.read_csv(PREPARED_DIR / f"{name}.csv")
    return Dataset.from_pandas(df[["text", "label"]], preserve_index=False)


def compute_metrics(eval_pred):
    logits, labels = eval_pred
    preds = np.argmax(logits, axis=-1)
    return {
        "accuracy": accuracy_score(labels, preds),
        "macro_f1": f1_score(labels, preds, average="macro", zero_division=0),
    }


class WeightedTrainer(Trainer):
    def __init__(self, class_weights=None, **kwargs):
        super().__init__(**kwargs)
        self.class_weights = class_weights

    def compute_loss(self, model, inputs, return_outputs=False, **kwargs):
        labels = inputs.pop("labels")
        outputs = model(**inputs)
        logits = outputs.logits
        loss_fct = nn.CrossEntropyLoss(weight=self.class_weights.to(logits.device))
        loss = loss_fct(logits.view(-1, self.model.config.num_labels), labels.view(-1))
        return (loss, outputs) if return_outputs else loss


def main():
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")

    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)

    def tokenize(batch):
        return tokenizer(batch["text"], truncation=True, max_length=64)

    train_ds = load_split("train").map(tokenize, batched=True)
    val_ds   = load_split("val").map(tokenize, batched=True)

    train_labels = pd.read_csv(PREPARED_DIR / "train.csv")["label"].tolist()
    counts = Counter(train_labels)
    n = len(train_labels)
    weights = [n / (len(INTENTS) * counts.get(i, 1)) for i in range(len(INTENTS))]
    class_weights = torch.tensor(weights, dtype=torch.float)
    print("Class weights:", [round(w, 2) for w in weights])

    model = AutoModelForSequenceClassification.from_pretrained(
        BASE_MODEL,
        num_labels=len(INTENTS),
        id2label=ID2LABEL,
        label2id=LABEL2ID,
    )

    args = TrainingArguments(
        output_dir=str(OUTPUT_DIR / "_checkpoints"),
        num_train_epochs=8,
        per_device_train_batch_size=16,
        per_device_eval_batch_size=32,
        learning_rate=3e-5,
        weight_decay=0.01,
        warmup_ratio=0.1,
        eval_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="accuracy",
        greater_is_better=True,
        logging_steps=25,
        fp16=(device == "cuda"),
        report_to=[],
        seed=42,
    )

    trainer = WeightedTrainer(
        class_weights=class_weights,
        model=model,
        args=args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        tokenizer=tokenizer,
        data_collator=DataCollatorWithPadding(tokenizer),
        compute_metrics=compute_metrics,
    )

    print("\nTraining ...")
    trainer.train()
    print("\nValidation:", trainer.evaluate())

    trainer.save_model(str(OUTPUT_DIR))
    tokenizer.save_pretrained(str(OUTPUT_DIR))
    print(f"\nModel saved to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
