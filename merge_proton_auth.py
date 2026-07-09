#!/usr/bin/env python3
"""Merge multiple Proton Authenticator JSON export files into a deduplicated master file."""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple
from urllib.parse import urlparse, parse_qs

OUTPUT_FILENAME = "merged_proton_auth.json"
UNIQUE_PREFIX = "unique_"
MAX_DESCRIPTIVE_LENGTH = 80
UNSAFE_CHARS_PATTERN = re.compile(r'[/\\:*?"<>| ]')


def parse_args() -> Path:
    """Parse CLI arguments and return the target directory path."""
    parser = argparse.ArgumentParser(
        description="Merge multiple Proton Authenticator JSON export files into a deduplicated master file."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory containing Proton Authenticator export files (default: current directory)",
    )
    args = parser.parse_args()
    return Path(args.directory)


def discover_json_files(directory: Path) -> Tuple[List[Path], bool]:
    """Discover JSON files, excluding output file and unique_ prefixed files.

    Returns:
        (sorted list of input .json files, output_exists flag)
    """
    all_json = sorted(directory.glob("*.json"))
    output_exists = (directory / OUTPUT_FILENAME).exists()
    json_files = [
        f for f in all_json
        if f.name != OUTPUT_FILENAME and not f.name.startswith(UNIQUE_PREFIX)
    ]
    return json_files, output_exists


def check_output_conflict(output_exists: bool) -> bool:
    """Check if the output file already exists and prompt user to continue.

    Returns True if processing should continue, False if user wants to abort.
    """
    if not output_exists:
        return True
    print(f"Warning: '{OUTPUT_FILENAME}' already exists and will be overwritten.")
    response = input("Continue? [y/N] ").strip().lower()
    return response in ("y", "yes")


def is_valid_export(data) -> bool:
    """Return True iff data is a dict with version == 1 and entries as a list."""
    return (
        isinstance(data, dict)
        and data.get("version") == 1
        and isinstance(data.get("entries"), list)
    )


def validate_export_files(json_files: List[Path]) -> Tuple[List[Path], List[str]]:
    """Validate all JSON files. Returns (valid_files, invalid_filenames).

    If invalid_filenames is non-empty, the caller must abort.
    """
    valid = []
    invalid = []
    for f in json_files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if is_valid_export(data):
                valid.append(f)
            else:
                invalid.append(f.name)
        except (json.JSONDecodeError, OSError):
            invalid.append(f.name)
    return valid, invalid


def parse_entries(files: List[Path]) -> List[Tuple[str, Dict]]:
    """Returns list of (filename, entry) pairs from all validated files.

    Files are already validated so no error handling is needed.
    """
    all_entries = []
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        for entry in data["entries"]:
            all_entries.append((f.name, entry))
    return all_entries


def has_note(entry: Dict) -> bool:
    """Returns True if the entry has a non-empty, non-null note."""
    note = entry.get("note")
    return note is not None and note != ""


def sanitise_filename_part(raw: str) -> str:
    """Sanitise a string for safe use in filenames.

    - Replaces unsafe characters (/ \\ : * ? \" < > | space) with _
    - Truncates to MAX_DESCRIPTIVE_LENGTH characters

    Returns:
        Sanitised, truncated string.
    """
    sanitised = UNSAFE_CHARS_PATTERN.sub("_", raw)
    return sanitised[:MAX_DESCRIPTIVE_LENGTH]


def derive_entry_name(entry: Dict) -> str:
    """Derive a descriptive name from an entry for use in filenames.

    Priority:
        1. Non-empty note field
        2. issuer parameter from otpauth URI
        3. content.name field

    Returns:
        The raw (unsanitised) descriptive string.
    """
    # Priority 1: note
    note = entry.get("note")
    if note is not None and note != "":
        return note

    # Priority 2: issuer from URI
    uri = entry.get("content", {}).get("uri", "")
    try:
        parsed = urlparse(uri)
        params = parse_qs(parsed.query)
        issuer_list = params.get("issuer", [])
        if issuer_list and issuer_list[0]:
            return issuer_list[0]
    except (ValueError, AttributeError):
        pass

    # Priority 3: content.name
    return entry.get("content", {}).get("name", "unknown")


def deduplicate(entries: List[Tuple[str, Dict]]) -> List[Dict]:
    """Deduplicate entries by ID with note preference.

    Returns list of unique entries preserving insertion order.
    """
    seen: Dict[str, Dict] = {}
    for _filename, entry in entries:
        entry_id = entry["id"]
        if entry_id not in seen:
            seen[entry_id] = entry
        else:
            existing = seen[entry_id]
            if has_note(entry) and not has_note(existing):
                seen[entry_id] = entry
    return list(seen.values())


UniqueEntryInfo = Tuple[str, Dict]  # (source_filename, entry)


def identify_unique_entries(
    entries: List[Tuple[str, Dict]],
) -> List[UniqueEntryInfo]:
    """Identify entries whose UUID appears in exactly one source file.

    Args:
        entries: List of (source_filename, entry) pairs from parse_entries.

    Returns:
        List of (source_filename, entry) for entries unique to one file,
        sorted by source filename then entry ID for deterministic output.
    """
    # Map each entry_id to the set of source files it appears in
    id_to_files: Dict[str, Set[str]] = {}
    # Map each (entry_id, filename) to the entry dict (first occurrence per file)
    id_file_to_entry: Dict[Tuple[str, str], Dict] = {}

    for filename, entry in entries:
        entry_id = entry["id"]
        if entry_id not in id_to_files:
            id_to_files[entry_id] = set()
        id_to_files[entry_id].add(filename)
        # Keep first occurrence per file for this ID
        if (entry_id, filename) not in id_file_to_entry:
            id_file_to_entry[(entry_id, filename)] = entry

    unique: List[UniqueEntryInfo] = []
    for entry_id, files in id_to_files.items():
        if len(files) == 1:
            source = next(iter(files))
            unique.append((source, id_file_to_entry[(entry_id, source)]))

    # Sort for deterministic output
    unique.sort(key=lambda x: (x[0], x[1]["id"]))
    return unique


def generate_unique_filenames(
    unique_entries: List[UniqueEntryInfo],
) -> List[Tuple[str, Dict, str]]:
    """Generate unique filenames for single-entry files.

    Args:
        unique_entries: List of (source_filename, entry) pairs.

    Returns:
        List of (source_filename, entry, output_filename) triples.
    """
    results: List[Tuple[str, Dict, str]] = []
    seen_filenames: Dict[str, int] = {}  # base_name -> occurrence count

    for source_filename, entry in unique_entries:
        source_stem = Path(source_filename).stem
        descriptive = sanitise_filename_part(derive_entry_name(entry))
        base_name = f"unique_{source_stem}_{descriptive}.json"

        if base_name not in seen_filenames:
            seen_filenames[base_name] = 1
            results.append((source_filename, entry, base_name))
        else:
            seen_filenames[base_name] += 1
            suffix = seen_filenames[base_name]
            # Insert suffix before .json
            collision_name = f"unique_{source_stem}_{descriptive}_{suffix}.json"
            results.append((source_filename, entry, collision_name))

    return results


def count_existing_unique_files(
    directory: Path,
    filenames: List[str],
) -> int:
    """Count how many of the given filenames already exist in directory."""
    return sum(1 for name in filenames if (directory / name).exists())


def confirm_merge(
    files: List[Path],
    total_entries: int,
    unique_entry_count: int,
    output_exists: bool,
    entries_per_file: Dict[str, int],
    unique_per_file: Dict[str, int],
    single_files_to_create: int,
    existing_overwrite_count: int,
) -> bool:
    """Display merge summary with unique entry info and prompt for confirmation."""
    duplicates = total_entries - unique_entry_count
    action = "Replace existing" if output_exists else "Create new"

    print("\n--- Merge Summary ---")
    print(f"Input files ({len(files)}):")
    for f in files:
        count = entries_per_file.get(f.name, 0)
        unique_count = unique_per_file.get(f.name, 0)
        print(f"  - {f.name} ({count} entries, {unique_count} unique)")
    print(f"Total entries found: {total_entries}")
    print(f"Duplicates detected: {duplicates}")
    print(f"Unique entries to write: {unique_entry_count}")
    print(f"Output: {action} '{OUTPUT_FILENAME}'")
    print(f"Single-entry files to create: {single_files_to_create}")
    if existing_overwrite_count > 0:
        print(f"  (will replace {existing_overwrite_count} existing file(s))")
    print("---------------------\n")

    response = input("Proceed with merge? [y/N] ").strip().lower()
    return response in ("y", "yes")


def write_output(directory: Path, entries: List[Dict]) -> Path:
    """Write the merged output file."""
    output = {
        "version": 1,
        "entries": entries
    }
    output_path = directory / OUTPUT_FILENAME
    output_path.write_text(
        json.dumps(output, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8"
    )
    return output_path


def write_single_entry_files(
    directory: Path,
    file_entries: List[Tuple[str, Dict, str]],
) -> List[str]:
    """Write individual export files for unique entries.

    Args:
        directory: Output directory path.
        file_entries: List of (source_filename, entry, output_filename) triples.

    Returns:
        List of filenames written.
    """
    written: List[str] = []
    for _source, entry, filename in file_entries:
        output = {
            "version": 1,
            "entries": [entry]
        }
        output_path = directory / filename
        output_path.write_text(
            json.dumps(output, indent=4, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        written.append(filename)
    return written


def print_report(
    num_files: int,
    total_entries: int,
    unique_entries: int,
    single_entry_filenames: List[str],
) -> None:
    """Print summary of the merge operation including single-entry files."""
    duplicates = total_entries - unique_entries
    print(f"Files processed: {num_files}")
    print(f"Total entries found: {total_entries}")
    print(f"Unique entries written: {unique_entries}")
    print(f"Duplicates resolved: {duplicates}")

    if single_entry_filenames:
        print(f"\nSingle-entry files created ({len(single_entry_filenames)}):")
        for name in single_entry_filenames:
            print(f"  - {name}")
    else:
        print("\nNo single-entry files needed (all entries shared across files).")


def main() -> int:
    """Main entry point for the merge script."""
    directory = parse_args()

    # Check directory existence
    if not directory.is_dir():
        print(f"Error: Directory not found: {directory}", file=sys.stderr)
        return 1

    # Discover JSON files
    json_files, output_exists = discover_json_files(directory)

    # Output file conflict check
    if output_exists:
        if not check_output_conflict(output_exists):
            print("No files were written.")
            return 0

    # Strict validation — abort if any file is invalid
    valid_files, invalid_files = validate_export_files(json_files)
    if invalid_files:
        print("The following files are not valid Proton Authenticator exports:")
        for name in invalid_files:
            print(f"  - {name}")
        print("Aborting.", file=sys.stderr)
        return 1
    if not valid_files:
        print("Error: No valid Proton Authenticator export files found.", file=sys.stderr)
        return 1

    # Entry parsing
    all_entries = parse_entries(valid_files)

    # Count entries per file for summary display
    entries_per_file: Dict[str, int] = {}
    for filename, _ in all_entries:
        entries_per_file[filename] = entries_per_file.get(filename, 0) + 1

    # Deduplication
    unique_entries = deduplicate(all_entries)

    # Unique entry identification (only with 2+ files)
    if len(valid_files) >= 2:
        unique_to_file = identify_unique_entries(all_entries)
        file_entries = generate_unique_filenames(unique_to_file)
    else:
        unique_to_file = []
        file_entries = []

    # Compute per-file unique counts for summary
    unique_per_file: Dict[str, int] = {}
    for source, _entry in unique_to_file:
        unique_per_file[source] = unique_per_file.get(source, 0) + 1

    # Count existing files that will be overwritten
    filenames_to_write = [fname for _, _, fname in file_entries]
    existing_overwrite_count = count_existing_unique_files(directory, filenames_to_write)

    # Confirmation prompt
    if not confirm_merge(
        valid_files,
        len(all_entries),
        len(unique_entries),
        output_exists,
        entries_per_file,
        unique_per_file,
        len(file_entries),
        existing_overwrite_count,
    ):
        print("No files were written.")
        return 0

    # Output generation
    write_output(directory, unique_entries)

    # Write single-entry files
    written_filenames = write_single_entry_files(directory, file_entries)

    # Reporting
    print_report(len(valid_files), len(all_entries), len(unique_entries), written_filenames)
    return 0


if __name__ == "__main__":
    sys.exit(main())
