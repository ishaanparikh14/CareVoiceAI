"""Print the Neon DATABASE_URL_OVERRIDE stored in the Modal secret.

Run:  modal run deploy/print_db_url.py
"""
import os

import modal

app = modal.App("carevoice-print-db-url")


@app.function(secrets=[modal.Secret.from_name("carevoice-secrets")])
def f():
    print("DATABASE_URL_OVERRIDE=" + os.environ["DATABASE_URL_OVERRIDE"])


@app.local_entrypoint()
def main():
    f.remote()
