"""Clear all alerts from database"""
import asyncio
import aiosqlite
from pathlib import Path

async def clear():
    db_path = Path(__file__).parent / "storage" / "carevoice.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM alerts")
        await db.commit()
        print("✓ All alerts cleared")

if __name__ == "__main__":
    asyncio.run(clear())
