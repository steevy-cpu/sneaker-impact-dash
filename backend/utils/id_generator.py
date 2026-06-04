import sqlite3
from datetime import date


def generate_shoe_id(conn: sqlite3.Connection) -> str:
    """
    Generate the next sequential shoe ID for today.

    Format: SHOE-YYYYMMDD-NNNN
    Example: SHOE-20260504-0001

    Queries the database to find the highest existing sequence for today,
    then increments by one. Starts at 0001 on the first shoe of the day.
    """
    today = date.today().strftime("%Y%m%d")
    prefix = f"SHOE-{today}-"

    row = conn.execute(
        "SELECT id FROM shoes WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()

    seq = (int(str(row["id"]).split("-")[-1]) + 1) if row else 1
    return f"{prefix}{seq:04d}"


def generate_batch_id(conn: sqlite3.Connection) -> str:
    """
    Generate the next sequential batch ID for today.

    Format: BATCH-YYYYMMDD-NNN
    Example: BATCH-20260504-001
    """
    today = date.today().strftime("%Y%m%d")
    prefix = f"BATCH-{today}-"

    row = conn.execute(
        "SELECT id FROM batches WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()

    seq = (int(str(row["id"]).split("-")[-1]) + 1) if row else 1
    return f"{prefix}{seq:03d}"


def generate_table_photo_id(conn: sqlite3.Connection) -> str:
    """
    Generate the next sequential table-photo ID for today.

    Format: TBL-YYYYMMDD-NNNN
    Example: TBL-20260604-0001
    """
    today = date.today().strftime("%Y%m%d")
    prefix = f"TBL-{today}-"

    row = conn.execute(
        "SELECT id FROM table_photos WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()

    seq = (int(str(row["id"]).split("-")[-1]) + 1) if row else 1
    return f"{prefix}{seq:04d}"


def generate_pair_id(conn: sqlite3.Connection) -> str:
    """
    Generate the next sequential pair ID for today.

    Format: PAIR-YYYYMMDD-NNNN
    Example: PAIR-20260604-0001
    """
    today = date.today().strftime("%Y%m%d")
    prefix = f"PAIR-{today}-"

    row = conn.execute(
        "SELECT id FROM pairs WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()

    seq = (int(str(row["id"]).split("-")[-1]) + 1) if row else 1
    return f"{prefix}{seq:04d}"
