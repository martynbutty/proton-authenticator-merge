#!/usr/bin/env python3
"""Merge two Proton Authenticator JSON export files into a deduplicated master file.

Outputs all files to an 'output/' subdirectory relative to the target directory.
Also generates individual single-entry import files for entries unique to one source,
with a summary showing which input file each unique entry is missing from.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple
from urllib.parse import urlparse, parse_qs

OUTPUT_DIR_NAME = "output"
OUTPUT_FILENAME = "merged_proton_auth.json"
UNIQUE_PREFIX = "unique_"
MAX_DESCRIPTIVE_LENGTH = 80
UNSAFE_CHARS_PATTERN = re.compile(r'[/\\:*?"<>| ]')
GENERIC_EXPORT_PATTERN = re.compile(
    r"^Proton Authenticator_export_\d{4}-\d{2}-\d{2}\.json_\d+\.json$"
)


def parse_args() -> Path:
    """Parse CLI arguments and return the target directory path."""
    parser = argparse.ArgumentParser(
        description="Merge two Proton Authenticator JSON export files into a deduplicated master file."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory containing Proton Authenticator export files (default: current directory)",
    )
    args = parser.parse_args()
    return Path(args.directory)


def discover_json_files(directory: Path) -> List[Path]:
    """Discover JSON files that look like Proton Authenticator exports.

    Only considers .json files in the given directory (not subdirectories).
    Does not look inside the output/ subdirectory.

    Returns:
        Sorted list of .json file paths in the directory.
    """
    all_json = sorted(directory.glob("*.json"))
    return all_json


def is_valid_export(data) -> bool:
    """Return True iff data is a dict with version == 1 and entries as a list."""
    return (
        isinstance(data, dict)
        and data.get("version") == 1
        and isinstance(data.get("entries"), list)
    )


def validate_export_files(json_files: List[Path]) -> Tuple[List[Path], List[str]]:
    """Validate all JSON files as Proton Authenticator exports.

    Returns (valid_files, invalid_filenames).
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


def has_generic_export_names(files: List[Path]) -> List[Path]:
    """Return files whose names match the generic Proton export pattern."""
    return [f for f in files if GENERIC_EXPORT_PATTERN.match(f.name)]


def warn_generic_filenames(generic_files: List[Path]) -> bool:
    """Warn user about generic filenames and ask if they want to continue.

    Returns True if user wants to continue, False to abort.
    """
    print("\n--- Filename Suggestion ---")
    print("The following input files have generic Proton Authenticator export names:")
    for f in generic_files:
        print(f"  - {f.name}")
    print(
        "\nConsider renaming them with meaningful prefixes (e.g. 'mobile_' or 'desktop_')"
    )
    print(
        "so it's easier to identify which Proton instance to import unique entries into."
    )
    print("--------------------------\n")
    response = input("Continue with current filenames? [y/N] ").strip().lower()
    return response in ("y", "yes")


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


def determine_missing_from(
    source_filename: str,
    all_filenames: List[str],
) -> str:
    """Determine which input file the entry is missing from.

    Since there are exactly 2 input files, the entry is missing from
    whichever file it is NOT in (i.e. the other file).
    """
    for name in all_filenames:
        if name != source_filename:
            return name
    return "unknown"


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
            collision_name = f"unique_{source_stem}_{descriptive}_{suffix}.json"
            results.append((source_filename, entry, collision_name))

    return results


def check_output_dir_conflicts(output_dir: Path, filenames: List[str]) -> List[str]:
    """Check which output filenames already exist in the output directory.

    Returns list of existing filenames.
    """
    existing = []
    for name in filenames:
        if (output_dir / name).exists():
            existing.append(name)
    return existing


def confirm_overwrite(existing_files: List[str]) -> bool:
    """Warn user about files that will be overwritten and ask to continue.

    Returns True if user wants to continue, False to abort.
    """
    print(f"\nWarning: The following {len(existing_files)} file(s) in '{OUTPUT_DIR_NAME}/' will be overwritten:")
    for name in existing_files:
        print(f"  - {name}")
    print()
    response = input("Continue and overwrite? [y/N] ").strip().lower()
    return response in ("y", "yes")


def confirm_merge(
    files: List[Path],
    total_entries: int,
    unique_entry_count: int,
    entries_per_file: Dict[str, int],
    unique_per_file: Dict[str, int],
    single_files_to_create: int,
) -> bool:
    """Display merge summary and prompt for confirmation."""
    duplicates = total_entries - unique_entry_count

    print("\n--- Merge Summary ---")
    print(f"Input files ({len(files)}):")
    for f in files:
        count = entries_per_file.get(f.name, 0)
        unique_count = unique_per_file.get(f.name, 0)
        print(f"  - {f.name} ({count} entries, {unique_count} unique to this file)")
    print(f"Total entries found: {total_entries}")
    print(f"Duplicates detected: {duplicates}")
    print(f"Unique entries to write: {unique_entry_count}")
    print(f"Single-entry import files to create: {single_files_to_create}")
    print(f"Output directory: {OUTPUT_DIR_NAME}/")
    print("---------------------\n")

    response = input("Proceed with merge? [y/N] ").strip().lower()
    return response in ("y", "yes")


def write_output(output_dir: Path, entries: List[Dict]) -> Path:
    """Write the merged output file to the output directory."""
    output = {
        "version": 1,
        "entries": entries,
    }
    output_path = output_dir / OUTPUT_FILENAME
    output_path.write_text(
        json.dumps(output, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return output_path


def write_single_entry_files(
    output_dir: Path,
    file_entries: List[Tuple[str, Dict, str]],
) -> List[str]:
    """Write individual export files for unique entries.

    Args:
        output_dir: Output directory path.
        file_entries: List of (source_filename, entry, output_filename) triples.

    Returns:
        List of filenames written.
    """
    written: List[str] = []
    for _source, entry, filename in file_entries:
        output = {
            "version": 1,
            "entries": [entry],
        }
        output_path = output_dir / filename
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
    output_dir: Path,
) -> None:
    """Print summary of the merge operation."""
    duplicates = total_entries - unique_entries
    print(f"\nFiles processed: {num_files}")
    print(f"Total entries found: {total_entries}")
    print(f"Unique entries written to merged file: {unique_entries}")
    print(f"Duplicates resolved: {duplicates}")
    print(f"Output directory: {output_dir}")

    if single_entry_filenames:
        print(f"Single-entry import files created: {len(single_entry_filenames)}")
    else:
        print("No single-entry import files needed (all entries shared across both files).")


def print_missing_from_summary(
    file_entries: List[Tuple[str, Dict, str]],
    all_input_filenames: List[str],
) -> None:
    """Print a summary showing which input file each unique entry is missing from.

    This helps the user know which Proton Authenticator instance they should
    import each single-entry file into.
    """
    if not file_entries:
        return

    print("\n--- Import Summary ---")
    print("The following single-entry files contain items missing from one of your inputs.")
    print("Import each file into the Proton Authenticator instance corresponding to the")
    print("file it is missing from:\n")

    for source_filename, entry, output_filename in file_entries:
        missing_from = determine_missing_from(source_filename, all_input_filenames)
        name = derive_entry_name(entry)
        print(f"  {output_filename}")
        print(f"    Entry: {name}")
        print(f"    Missing from: {missing_from}")
        print()

    print("----------------------")


def main() -> int:
    """Main entry point for the merge script."""
    directory = parse_args()

    # Check directory existence
    if not directory.is_dir():
        print(f"Error: Directory not found: {directory}", file=sys.stderr)
        return 1

    # Discover JSON files in the target directory
    json_files = discover_json_files(directory)

    # Validate which ones are actual Proton Authenticator exports
    valid_files, _invalid_files = validate_export_files(json_files)

    # Enforce exactly 2 input files
    if len(valid_files) < 2:
        print(
            "Error: Expected exactly 2 Proton Authenticator export files, "
            f"but found {len(valid_files)}.",
            file=sys.stderr,
        )
        if len(valid_files) == 0:
            print("No valid export files found in the directory.", file=sys.stderr)
        else:
            print(f"  Found: {valid_files[0].name}", file=sys.stderr)
        return 1

    if len(valid_files) > 2:
        print(
            "Error: Expected exactly 2 Proton Authenticator export files, "
            f"but found {len(valid_files)}.",
            file=sys.stderr,
        )
        print(
            "Cannot determine which two files to merge. Please ensure only 2 export "
            "files are present in the directory (move extras elsewhere).",
            file=sys.stderr,
        )
        for f in valid_files:
            print(f"  - {f.name}", file=sys.stderr)
        return 1

    # Warn about generic filenames
    generic_files = has_generic_export_names(valid_files)
    if generic_files:
        if not warn_generic_filenames(generic_files):
            print("Aborted. Rename your files and try again.")
            return 0

    # Entry parsing
    all_entries = parse_entries(valid_files)
    all_input_filenames = [f.name for f in valid_files]

    # Count entries per file for summary display
    entries_per_file: Dict[str, int] = {}
    for filename, _ in all_entries:
        entries_per_file[filename] = entries_per_file.get(filename, 0) + 1

    # Deduplication
    unique_entries = deduplicate(all_entries)

    # Unique entry identification (always 2 files at this point)
    unique_to_file = identify_unique_entries(all_entries)
    file_entries = generate_unique_filenames(unique_to_file)

    # Compute per-file unique counts for summary
    unique_per_file: Dict[str, int] = {}
    for source, _entry in unique_to_file:
        unique_per_file[source] = unique_per_file.get(source, 0) + 1

    # Confirmation prompt
    if not confirm_merge(
        valid_files,
        len(all_entries),
        len(unique_entries),
        entries_per_file,
        unique_per_file,
        len(file_entries),
    ):
        print("No files were written.")
        return 0

    # Prepare output directory
    output_dir = directory / OUTPUT_DIR_NAME
    output_dir.mkdir(exist_ok=True)

    # Check for existing files that would be overwritten
    all_output_filenames = [OUTPUT_FILENAME] + [fname for _, _, fname in file_entries]
    existing_files = check_output_dir_conflicts(output_dir, all_output_filenames)
    if existing_files:
        if not confirm_overwrite(existing_files):
            print("No files were written.")
            return 0

    # Write merged output
    write_output(output_dir, unique_entries)

    # Write single-entry files
    written_filenames = write_single_entry_files(output_dir, file_entries)

    # Print report
    print_report(
        len(valid_files),
        len(all_entries),
        len(unique_entries),
        written_filenames,
        output_dir,
    )

    # Print missing-from summary for unique entries
    print_missing_from_summary(file_entries, all_input_filenames)

    return 0


if __name__ == "__main__":
    sys.exit(main())
