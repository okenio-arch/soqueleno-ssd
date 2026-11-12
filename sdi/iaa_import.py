from __future__ import annotations

from pathlib import Path

from .db import connect
from .normalise import clean, normalise_name, normalise_url, domain_of

IAA_SOURCE_ID = "iaa_foi_register"
IAA_SOURCE_URL = "https://www.gov.uk/government/publications/register-of-currently-registered-immigration-advice-organisations"
DATASET_AS_OF = "2026-07-09"
PUBLISHED_AT = "2026-09-25"

# Header names used by the official IAA XLSX, plus older variants.
ALIASES = {
    "name": {
        "organisation name", "organisation", "name",
        "registered organisation name", "business name",
    },
    "reference": {
        "registration number", "registration no", "registration reference",
        "organisation registration number", "organisation number",
        "iaa number", "oisc number", "reference number",
    },
    "level": {
        "registration level", "regulated level", "level",
        "authorisation level",
    },
    "approval_date": {
        "earliest approval date", "approval date", "date of approval",
    },
    "status": {
        "authorisation status", "authorisation", "status",
        "registration status",
    },
    "website": {
        "website", "web site", "website address", "organisation website",
    },
    "address": {"address", "registered address", "business address"},
    "phone": {"telephone", "telephone number", "phone", "telephone no"},
    "email": {"email", "email address", "public email"},
}


def _key(value):
    return " ".join(str(value or "").strip().casefold().replace("_", " ").split())


def _detect(headers):
    result = {}
    for index, header in enumerate(headers):
        key = _key(header)
        for field, aliases in ALIASES.items():
            if key in aliases and field not in result:
                result[field] = index
    return result


def inspect_workbook(path: str) -> dict:
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    result = {"file": Path(path).name, "sheets": []}
    try:
        for worksheet in workbook.worksheets:
            rows = list(worksheet.iter_rows(min_row=1, max_row=15, values_only=True))
            result["sheets"].append({
                "name": worksheet.title,
                "sample_rows": len(rows),
                "candidate_headers": [
                    list(row) for row in rows if any(value is not None for value in row)
                ][:5],
            })
    finally:
        workbook.close()
    return result


def import_iaa_xlsx(
    xlsx_path: str,
    db_path: str,
    dry_run: bool = False,
    *,
    dataset_as_of: str | None = None,
    published_at: str | None = None,
) -> dict:
    from openpyxl import load_workbook

    dataset_as_of = dataset_as_of or DATASET_AS_OF
    published_at = published_at or PUBLISHED_AT
    path = Path(xlsx_path)
    if not path.is_file():
        raise FileNotFoundError(path)

    workbook = load_workbook(path, read_only=True, data_only=True)
    found = None
    try:
        for worksheet in workbook.worksheets:
            for row_number, row in enumerate(
                worksheet.iter_rows(min_row=1, max_row=20, values_only=True), 1
            ):
                columns = _detect([clean(value) or "" for value in row])
                if "name" in columns and "reference" in columns:
                    found = (worksheet, row_number, columns)
                    break
            if found:
                break

        if not found:
            raise ValueError(
                "Não foram reconhecidos os cabeçalhos de nome e número de organização "
                "no XLSX do IAA."
            )

        worksheet, header_row, columns = found
        records = []
        seen = 0
        skipped = 0
        for row in worksheet.iter_rows(min_row=header_row + 1, values_only=True):
            seen += 1
            record = {
                field: clean(row[index] if index < len(row) else None)
                for field, index in columns.items()
            }
            if not record.get("name") or not record.get("reference"):
                skipped += 1
                continue
            record["website"] = normalise_url(record.get("website"))
            records.append(record)

        sheet_name = worksheet.title
    finally:
        workbook.close()

    result = {
        "rows_seen": seen,
        "rows_valid": len(records),
        "rows_skipped": skipped,
        "sheet": sheet_name,
        "header_row": header_row,
        "dry_run": dry_run,
        "created": 0,
        "updated": 0,
    }
    if dry_run:
        return result

    with connect(db_path) as conn:
        source = conn.execute(
            "SELECT 1 FROM sources WHERE source_id = ?", (IAA_SOURCE_ID,)
        ).fetchone()
        if not source:
            raise RuntimeError("Fonte IAA não registada. Execute load-sources.")

        batch_id = conn.execute(
            """INSERT INTO import_batches
               (source_id, filename, dataset_as_of, rows_seen, status)
               VALUES (?, ?, ?, ?, 'running')""",
            (IAA_SOURCE_ID, path.name, dataset_as_of, seen),
        ).lastrowid

        for record in records:
            reference = record["reference"]
            name = record["name"]
            domain = domain_of(record.get("website"))
            normalized_name = normalise_name(name)

            existing = conn.execute(
                """SELECT organisation_id FROM organisations
                   WHERE regulator = 'IAA' AND regulator_reference = ?""",
                (reference,),
            ).fetchone()

            # Reuse an existing LAA profile when the website domain matches.
            if not existing and domain:
                existing = conn.execute(
                    """SELECT organisation_id FROM organisations
                       WHERE domain = ? LIMIT 1""",
                    (domain,),
                ).fetchone()

            if not existing and record.get("address"):
                existing = conn.execute(
                    """SELECT organisation_id FROM organisations
                       WHERE normalized_name = ? AND coalesce(address, '') = ?
                       LIMIT 1""",
                    (normalized_name, record["address"]),
                ).fetchone()

            if existing:
                organisation_id = existing[0]
                conn.execute(
                    """UPDATE organisations
                       SET canonical_name = ?,
                           normalized_name = ?,
                           website = COALESCE(?, website),
                           domain = COALESCE(?, domain),
                           address = COALESCE(?, address),
                           phone = COALESCE(?, phone),
                           public_email = COALESCE(?, public_email),
                           regulator = CASE
                               WHEN regulator IS NULL OR regulator = 'IAA' THEN 'IAA'
                               ELSE regulator END,
                           regulator_reference = CASE
                               WHEN regulator IS NULL OR regulator = 'IAA' THEN ?
                               ELSE regulator_reference END,
                           confidence = max(confidence, 0.75),
                           last_seen_at = CURRENT_TIMESTAMP
                       WHERE organisation_id = ?""",
                    (
                        name,
                        normalized_name,
                        record.get("website"),
                        domain,
                        record.get("address"),
                        record.get("phone"),
                        record.get("email"),
                        reference,
                        organisation_id,
                    ),
                )
                result["updated"] += 1
            else:
                cursor = conn.execute(
                    """INSERT INTO organisations
                       (canonical_name, normalized_name, website, domain,
                        address, phone, public_email, regulator,
                        regulator_reference, confidence)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'IAA', ?, 0.75)""",
                    (
                        name,
                        normalized_name,
                        record.get("website"),
                        domain,
                        record.get("address"),
                        record.get("phone"),
                        record.get("email"),
                        reference,
                    ),
                )
                organisation_id = cursor.lastrowid
                result["created"] += 1

            # The unique key makes repeated imports idempotent.
            conn.execute(
                """INSERT OR IGNORE INTO organisation_identifiers
                   (organisation_id, source_id, namespace, identifier, source_url)
                   VALUES (?, ?, 'iaa_registration', ?, ?)""",
                (organisation_id, IAA_SOURCE_ID, reference, IAA_SOURCE_URL),
            )

            evidence_fields = {
                "canonical_name": name,
                "regulator_reference": reference,
                "registration_level": record.get("level"),
                "earliest_approval_date": record.get("approval_date"),
                "website": record.get("website"),
                "address": record.get("address"),
                "phone": record.get("phone"),
                "public_email": record.get("email"),
            }
            for field, value in evidence_fields.items():
                if value:
                    conn.execute(
                        """INSERT OR IGNORE INTO evidence
                           (organisation_id, source_id, source_url, field_name,
                            field_value, source_published_at, dataset_as_of,
                            evidence_status, confidence, source_authority)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 'official_snapshot', 0.90, 'Immigration Advice Authority')""",
                        (
                            organisation_id,
                            IAA_SOURCE_ID,
                            IAA_SOURCE_URL,
                            field,
                            str(value),
                            published_at,
                            dataset_as_of,
                        ),
                    )

        conn.execute(
            """UPDATE import_batches
               SET organisations_created = ?,
                   organisations_updated = ?,
                   rows_skipped = ?,
                   status = 'completed'
               WHERE batch_id = ?""",
            (result["created"], result["updated"], skipped, batch_id),
        )

    result["batch_id"] = batch_id
    return result
