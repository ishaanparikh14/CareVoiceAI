# -*- coding: utf-8 -*-
"""
his_adapter.py — HIS / EMR integration layer (FHIR R4-style).

Pull:  patient, conditions, observations (vitals + labs), medication requests and
       encounters feed the care agent's summaries and the patient Q&A.
Push:  patient summaries, shift summaries and alert logs are written back as
       DocumentReference / Communication so everything stays in one record.

Two implementations, chosen at runtime from the admin Workflow Settings
('his_adapter'):

  LocalDemoEMR   seeded demo data in the CareVoice Postgres (default). Labelled
                 DEMO everywhere it is shown.
  FhirHttpEMR    generic FHIR R4 REST client (HIS_BASE_URL, HIS_AUTH bearer token).
                 Built to the FHIR spec but NOT tested against a real vendor; a
                 real hospital needs its own endpoint, credentials and mapping.

Everything stays inside the hospital network: the FHIR base URL is the
hospital's own HIS, and the care agent uses a self-hosted Ollama.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod

from config import settings

logger = logging.getLogger(__name__)


class EMRAdapter(ABC):
    name = "abstract"
    label = ""

    @abstractmethod
    async def patient(self, room: str) -> dict | None: ...
    @abstractmethod
    async def conditions(self, room: str) -> list[dict]: ...
    @abstractmethod
    async def observations(self, room: str, category: str | None = None) -> list[dict]: ...
    @abstractmethod
    async def medications(self, room: str) -> list[dict]: ...
    @abstractmethod
    async def encounters(self, room: str) -> list[dict]: ...
    @abstractmethod
    async def documents(self, room: str) -> list[dict]: ...
    @abstractmethod
    async def write_document(self, room: str, doc_type: str, title: str, content: str, author: str) -> dict: ...

    async def record(self, room: str) -> dict:
        """Full record for the doctor / care agent."""
        return {
            "source": self.name,
            "source_label": self.label,
            "patient": await self.patient(room),
            "conditions": await self.conditions(room),
            "vitals": await self.observations(room, "vital-signs"),
            "labs": await self.observations(room, "laboratory"),
            "medications": await self.medications(room),
            "encounters": await self.encounters(room),
            "documents": await self.documents(room),
        }

    async def key_parameters(self, room: str) -> dict:
        """Patient-safe projection: latest value per vital/lab, diet, diagnosis
        label and medication NAMES only (no doses, notes or other patients)."""
        p = await self.patient(room) or {}
        vit = _latest(await self.observations(room, "vital-signs"))
        lab = _latest(await self.observations(room, "laboratory"))
        meds = await self.medications(room)
        return {
            "source": self.name,
            "full_name": p.get("full_name"),
            "room_number": room,
            "diagnosis": p.get("diagnosis"),
            "diet_restriction": p.get("diet_restriction") or "none",
            "vitals": [_public_obs(o) for o in vit],
            "labs": [_public_obs(o) for o in lab],
            "medication_names": sorted({m["name"] for m in meds}),
        }


def _latest(obs: list[dict]) -> list[dict]:
    seen, out = set(), []
    for o in sorted(obs, key=lambda x: str(x.get("effective_at", "")), reverse=True):
        if o["code"] in seen:
            continue
        seen.add(o["code"])
        out.append(o)
    return out


def _public_obs(o: dict) -> dict:
    return {k: o.get(k) for k in ("display", "value", "unit", "ref_range", "flag", "effective_at")}


def _iso(v):
    try:
        return v.isoformat()
    except Exception:
        return v


# ── Local demo EMR (Postgres) ─────────────────────────────────────────────────

class LocalDemoEMR(EMRAdapter):
    name = "local"
    label = "Demo EMR (sample data)"

    async def _fetch(self, sql: str, *args) -> list[dict]:
        import pg_database
        if pg_database._pool is None:
            return []
        async with pg_database._pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [{k: _iso(v) for k, v in dict(r).items()} for r in rows]

    async def patient(self, room):
        rows = await self._fetch(
            "SELECT full_name, room_number, age, diagnosis, attending, acuity, diet_restriction, admitted_at "
            "FROM patients WHERE room_number = $1 AND is_discharged = FALSE ORDER BY admitted_at DESC LIMIT 1",
            room)
        return rows[0] if rows else None

    async def conditions(self, room):
        return await self._fetch(
            "SELECT display, status, onset FROM emr_conditions WHERE room_number = $1 ORDER BY id", room)

    async def observations(self, room, category=None):
        if category:
            return await self._fetch(
                "SELECT category, code, display, value, unit, ref_range, flag, effective_at "
                "FROM emr_observations WHERE room_number = $1 AND category = $2 ORDER BY effective_at DESC",
                room, category)
        return await self._fetch(
            "SELECT category, code, display, value, unit, ref_range, flag, effective_at "
            "FROM emr_observations WHERE room_number = $1 ORDER BY effective_at DESC", room)

    async def medications(self, room):
        return await self._fetch(
            "SELECT name, kind, dosage FROM patient_medications WHERE room_number = $1 ORDER BY kind, name", room)

    async def encounters(self, room):
        return await self._fetch(
            "SELECT kind, summary, author, occurred_at FROM emr_encounters WHERE room_number = $1 "
            "ORDER BY occurred_at DESC", room)

    async def documents(self, room):
        return await self._fetch(
            "SELECT id, doc_type, title, content, author, created_at FROM emr_documents "
            "WHERE room_number = $1 ORDER BY created_at DESC LIMIT 20", room)

    async def write_document(self, room, doc_type, title, content, author):
        import pg_database
        async with pg_database._pool.acquire() as conn:
            r = await conn.fetchrow(
                "INSERT INTO emr_documents (room_number, doc_type, title, content, author) "
                "VALUES ($1,$2,$3,$4,$5) RETURNING id, created_at", room, doc_type, title, content, author)
        return {"id": r["id"], "created_at": _iso(r["created_at"]), "stored_in": self.name}


# ── Generic FHIR R4 over HTTP (untested against a real vendor) ────────────────

class FhirHttpEMR(EMRAdapter):
    """Maps a CareVoice room to a FHIR Patient via an identifier search
    (system 'urn:carevoice:room'). Every call is time-boxed; failures return
    empty data so callers degrade instead of erroring."""
    name = "fhir"
    label = "FHIR R4 (untested against a real vendor)"

    def __init__(self, base_url: str, token: str = ""):
        self.base = base_url.rstrip("/")
        self.token = token

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Accept": "application/fhir+json",
            **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
        })
        with urllib.request.urlopen(req, timeout=settings.HIS_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))

    async def _call(self, path, params=None) -> dict:
        import asyncio
        try:
            return await asyncio.to_thread(self._get, path, params)
        except Exception as exc:
            logger.warning("[HIS] FHIR %s failed: %s", path, exc)
            return {}

    async def _pid(self, room) -> str | None:
        b = await self._call("Patient", {"identifier": f"urn:carevoice:room|{room}"})
        for e in b.get("entry", []) or []:
            return e.get("resource", {}).get("id")
        return None

    @staticmethod
    def _entries(bundle):
        return [e.get("resource", {}) for e in (bundle.get("entry") or [])]

    async def patient(self, room):
        pid = await self._pid(room)
        if not pid:
            return None
        r = await self._call(f"Patient/{pid}")
        name = (r.get("name") or [{}])[0]
        full = " ".join((name.get("given") or []) + [name.get("family", "")]).strip()
        return {"full_name": full or None, "room_number": room, "fhir_id": pid,
                "birth_date": r.get("birthDate"), "gender": r.get("gender")}

    async def conditions(self, room):
        pid = await self._pid(room)
        if not pid:
            return []
        out = []
        for c in self._entries(await self._call("Condition", {"patient": pid})):
            out.append({"display": (c.get("code") or {}).get("text", ""),
                        "status": ((c.get("clinicalStatus") or {}).get("coding") or [{}])[0].get("code", ""),
                        "onset": c.get("onsetDateTime", "")})
        return out

    async def observations(self, room, category=None):
        pid = await self._pid(room)
        if not pid:
            return []
        params = {"patient": pid, "_sort": "-date", "_count": 50}
        if category:
            params["category"] = category
        out = []
        for o in self._entries(await self._call("Observation", params)):
            q = o.get("valueQuantity") or {}
            code = (o.get("code") or {})
            out.append({
                "category": category or "",
                "code": ((code.get("coding") or [{}])[0].get("code") or code.get("text", "")),
                "display": code.get("text", ""),
                "value": str(q.get("value", o.get("valueString", ""))),
                "unit": q.get("unit", ""),
                "ref_range": "",
                "flag": ((o.get("interpretation") or [{}])[0].get("text", "normal") if o.get("interpretation") else "normal"),
                "effective_at": o.get("effectiveDateTime", ""),
            })
        return out

    async def medications(self, room):
        pid = await self._pid(room)
        if not pid:
            return []
        out = []
        for m in self._entries(await self._call("MedicationRequest", {"patient": pid, "status": "active"})):
            out.append({"name": (m.get("medicationCodeableConcept") or {}).get("text", ""),
                        "kind": "prescribed",
                        "dosage": ((m.get("dosageInstruction") or [{}])[0].get("text", ""))})
        return out

    async def encounters(self, room):
        pid = await self._pid(room)
        if not pid:
            return []
        out = []
        for e in self._entries(await self._call("Encounter", {"patient": pid, "_sort": "-date"})):
            out.append({"kind": ((e.get("class") or {}).get("code") or "visit"),
                        "summary": ((e.get("reasonCode") or [{}])[0].get("text", "")),
                        "author": "", "occurred_at": (e.get("period") or {}).get("start", "")})
        return out

    async def documents(self, room):
        return []

    async def write_document(self, room, doc_type, title, content, author):
        import asyncio, base64
        pid = await self._pid(room)
        if not pid:
            return {"error": "patient_not_found_in_his"}
        resource = {
            "resourceType": "DocumentReference", "status": "current",
            "type": {"text": doc_type}, "description": title,
            "subject": {"reference": f"Patient/{pid}"},
            "author": [{"display": author}],
            "content": [{"attachment": {"contentType": "text/plain",
                                        "data": base64.b64encode(content.encode()).decode()}}],
        }

        def _post():
            req = urllib.request.Request(
                f"{self.base}/DocumentReference", data=json.dumps(resource).encode(), method="POST",
                headers={"Content-Type": "application/fhir+json",
                         **({"Authorization": f"Bearer {self.token}"} if self.token else {})})
            with urllib.request.urlopen(req, timeout=settings.HIS_TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read().decode() or "{}")
        try:
            r = await asyncio.to_thread(_post)
            return {"id": r.get("id"), "stored_in": self.name}
        except Exception as exc:
            logger.warning("[HIS] write-back failed: %s", exc)
            return {"error": str(exc)}


async def get_adapter(choice: str | None = None) -> EMRAdapter:
    """Return the adapter selected in the workflow settings (default local)."""
    if choice is None:
        try:
            import aiosqlite
            from workflow_config import get_config
            async with aiosqlite.connect(settings.DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                choice = (await get_config(db)).get("his_adapter", "local")
        except Exception:
            choice = "local"
    if choice == "fhir" and settings.HIS_BASE_URL:
        return FhirHttpEMR(settings.HIS_BASE_URL, settings.HIS_AUTH)
    return LocalDemoEMR()
