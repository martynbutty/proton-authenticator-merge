#!/usr/bin/env python3
"""Merge multiple Proton Authenticator JSON export files into a deduplicated master file."""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

OUTPUT_FILENAME = "merged_proton_auth.json"


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
    """Discover JSON files in a directory, excluding the output file.

    Returns:
        A tuple of (sorted list of .json file paths excluding the output file,
        boolean flag indicating whether the output file already exists).
    """
    all_json = sorted(directory.glob("*.json"))
    output_exists = (directory / OUTPUT_FILENAME).exists()
    json_files = [f for f in all_json if f.name != OUTPUT_FILENAME]
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


def confirm_merge(
    files: List[Path],
    total_entries: int,
    unique_entries: int,
    output_exists: bool,
    entries_per_file: Dict[str, int],
) -> bool:
    """Display merge summary and prompt for confirmation. Returns True to proceed."""
    duplicates = total_entries - unique_entries
    action = "Replace existing" if output_exists else "Create new"

    print("\n--- Merge Summary ---")
    print(f"Input files ({len(files)}):")
    for f in files:
        count = entries_per_file.get(f.name, 0)
        print(f"  - {f.name} ({count} entries)")
    print(f"Total entries found: {total_entries}")
    print(f"Duplicates detected: {duplicates}")
    print(f"Unique entries to write: {unique_entries}")
    print(f"Output: {action} '{OUTPUT_FILENAME}'")
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


def print_report(num_files: int, total_entries: int, unique_entries: int) -> None:
    """Print a summary of the merge operation."""
    duplicates = total_entries - unique_entries
    print(f"Files processed: {num_files}")
    print(f"Total entries found: {total_entries}")
    print(f"Unique entries written: {unique_entries}")
    print(f"Duplicates resolved: {duplicates}")


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

    # Confirmation prompt
    if not confirm_merge(valid_files, len(all_entries), len(unique_entries), output_exists, entries_per_file):
        print("No files were written.")
        return 0

    # Output generation
    write_output(directory, unique_entries)

    # Reporting
    print_report(len(valid_files), len(all_entries), len(unique_entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
