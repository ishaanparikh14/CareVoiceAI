"""EMR access layer for CareVoice.

This module is deliberately read-only for the first implementation.  The AI
must consume authoritative clinical context rather than inventing it.
"""
from __future__ import annotations

import asyncpg


async def get_patient_emr(conn: asyncpg.Connection, patient_id: int) -> dict | None:
    patient = await conn.fetchrow(
        """
        SELECT p.id, p.user_id, p.room_number, p.full_name, p.age, p.diagnosis,
               p.attending, p.admitted_at
        FROM patients p
        WHERE p.id = $1 AND p.is_discharged = FALSE
        """,
        patient_id,
    )
    if not patient:
        return None

    async def rows(sql: str):
        return [dict(r) for r in await conn.fetch(sql, patient_id)]

    conditions = await rows(
        """SELECT condition_name, status, severity, notes
           FROM patient_conditions WHERE patient_id=$1 ORDER BY id"""
    )
    allergies = await rows(
        """SELECT allergen, reaction, severity
           FROM patient_allergies WHERE patient_id=$1 ORDER BY id"""
    )
    medications = await rows(
        """SELECT medication_name, dose, route, frequency, status, instructions
           FROM patient_medications WHERE patient_id=$1 ORDER BY id"""
    )
    diet_orders = await rows(
        """SELECT diet_type, restrictions, status, instructions
           FROM patient_diet_orders WHERE patient_id=$1 ORDER BY id DESC"""
    )
    restrictions = await rows(
        """SELECT restriction_type, restriction_value, severity, instructions
           FROM patient_restrictions WHERE patient_id=$1 ORDER BY id"""
    )
    doctor_instructions = await rows(
        """SELECT instruction, priority, active
           FROM doctor_instructions WHERE patient_id=$1 AND active=TRUE ORDER BY id"""
    )
    nursing_notes = await rows(
        """SELECT note, author, created_at
           FROM nursing_notes WHERE patient_id=$1 ORDER BY created_at DESC LIMIT 10"""
    )
    vitals = await rows(
        """SELECT measured_at, heart_rate, systolic_bp, diastolic_bp,
                  spo2, temperature_c, blood_glucose
           FROM patient_vitals WHERE patient_id=$1 ORDER BY measured_at DESC LIMIT 5"""
    )
    food_options = [dict(r) for r in await conn.fetch(
        """SELECT id, name, diet_tags, allergens, available, notes
           FROM hospital_food_options
           WHERE available=TRUE ORDER BY name"""
    )]

    return {
        "patient": dict(patient),
        "conditions": conditions,
        "allergies": allergies,
        "medications": medications,
        "diet_orders": diet_orders,
        "restrictions": restrictions,
        "doctor_instructions": doctor_instructions,
        "nursing_notes": nursing_notes,
        "recent_vitals": vitals,
        "available_food_options": food_options,
    }


async def get_patient_by_room(conn: asyncpg.Connection, room_number: str) -> dict | None:
    row = await conn.fetchrow(
        """SELECT id FROM patients
           WHERE room_number=$1 AND is_discharged=FALSE""",
        room_number,
    )
    if not row:
        return None
    return await get_patient_emr(conn, row["id"])
