"""Bounded-memory archive utilities, independent of Frappe and the database."""
from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import shutil
from datetime import date
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

CHUNK = 1024 * 1024
PART_BYTES = 512 * 1024 * 1024


def cleanup_eligibility(term, today):
    if term.get("status") not in {"Completed", "Archived"}:
        return False, "term_not_closed"
    try:
        end = date.fromisoformat(str(term.get("end_date")))
        today = date.fromisoformat(str(today))
    except (TypeError, ValueError):
        return False, "invalid_end_date"
    if end >= today:
        return False, "term_not_ended"
    return True, ""


def segment(label, identity=""):
    clean = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "-", str(label or "Unnamed")).strip(" .")
    clean = clean[:70] or "Unnamed"
    # A digest prevents collisions caused by sanitization, truncation or case folding.
    digest = hashlib.sha256(str(identity or label).encode()).hexdigest()[:12]
    return f"{clean}-{digest}"


def archive_path(row):
    extension = Path(row.get("filename") or "file").suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,12}", extension):
        extension = ".bin"
    return "/".join([
        segment(row["term"]), segment(row["course_label"], row["course"]),
        segment(row["timeslot_label"], row["timeslot"]),
        segment(f'{row["session_date"]} {row["session"]}', row["session"]),
        segment(f'{row["post"]}-{row.get("idx", 0)}', row["key"]) + extension,
    ])


def hash_file(path):
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while data := stream.read(CHUNK):
            digest.update(data)
            size += len(data)
    return digest.hexdigest(), size


def manifest_csv(rows):
    fields = ["term", "course_label", "campus", "timeslot_label", "session", "session_date",
              "post", "kind", "teacher", "published_at", "filename", "archive_path", "bytes", "sha256", "status", "reason"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        # Prevent spreadsheet formula interpretation of human-controlled text.
        writer.writerow({key: ("'" + str(value) if str(value).lstrip().startswith(("=", "+", "-", "@")) else value)
                         for key, value in row.items() if key in fields})
    return output.getvalue().encode("utf-8-sig")


def write_parts(directory, rows, resolve_path, progress=None, part_bytes=PART_BYTES, notes=None):
    """Stream immutable snapshots into independent verified ZIPs. Per-file failures are explicit."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    required = sum(int(row.get("bytes") or 0) for row in rows)
    if shutil.disk_usage(directory).free < required + 128 * CHUNK:
        raise OSError("insufficient_storage")
    parts, current_rows, archive, size = [], [], None, 0
    index = 0

    def finish():
        nonlocal archive, current_rows
        if archive is None:
            return
        for entry in current_rows:
            if entry["status"] == "written":
                entry["status"], entry["part"] = "exported", index
        archive.writestr("manifest.csv", manifest_csv(current_rows))
        if notes:
            for path, content in notes.items():
                archive.writestr(path, content)
        archive.close()
        archive = None
        temporary = directory / f"part-{index:03d}.zip.tmp"
        with ZipFile(temporary) as check:
            bad = check.testzip()
            if bad:
                raise OSError("archive_verification_failed")
        final = directory / f"part-{index:03d}.zip"
        temporary.replace(final)
        parts.append({"index": index, "filename": final.name, "bytes": final.stat().st_size})
        for entry in current_rows:
            if entry["status"] == "written":
                entry["status"] = "exported"
                entry["part"] = index
        current_rows = []

    try:
        for number, row in enumerate(rows, 1):
            if archive is None or size + int(row.get("bytes") or 0) > part_bytes:
                finish()
                index += 1
                archive = ZipFile(directory / f"part-{index:03d}.zip.tmp", "w", compression=ZIP_STORED, allowZip64=True)
                size = 0
            current_rows.append(row)
            try:
                path = resolve_path(row)
                actual_hash, actual_size = hash_file(path)
                if row.get("sha256") and (actual_hash != row["sha256"] or actual_size != row["bytes"]):
                    raise ValueError("file_changed")
                row["sha256"], row["bytes"] = actual_hash, actual_size
                row["archive_path"] = archive_path(row)
                # Copy and hash in one pass; changes between hashing and copying cannot qualify for cleanup.
                digest = hashlib.sha256()
                with open(path, "rb") as source, archive.open(row["archive_path"], "w", force_zip64=True) as destination:
                    while data := source.read(CHUNK):
                        digest.update(data)
                        destination.write(data)
                if digest.hexdigest() != actual_hash:
                    raise ValueError("file_changed")
                row["status"], row["reason"] = "written", ""
                size += actual_size
            except (OSError, ValueError) as exc:
                row["status"] = "failed"
                row["reason"] = str(exc) if isinstance(exc, ValueError) else "file_unavailable"
            if progress:
                progress(number, rows, parts)
        # An empty archive still provides notes and manifest, never cleanup eligibility.
        if archive is None:
            index += 1
            archive = ZipFile(directory / f"part-{index:03d}.zip.tmp", "w", compression=ZIP_STORED)
        finish()
        # Include a complete manifest for missing/failed entries in every part.
        for part in parts:
            with ZipFile(directory / part["filename"], "a") as output:
                output.writestr("all-files.csv", manifest_csv(rows))
            with ZipFile(directory / part["filename"]) as check:
                if check.testzip():
                    raise OSError("archive_verification_failed")
            part["bytes"] = (directory / part["filename"]).stat().st_size
        return parts
    except BaseException:
        if archive is not None:
            archive.close()
        # No packages are published on a job-level failure.
        for file in directory.glob("part-*.zip*"):
            file.unlink(missing_ok=True)
        raise
