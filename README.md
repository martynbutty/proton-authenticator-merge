# Proton Authenticator Merge Tool

Merge two Proton Authenticator JSON export files into a single deduplicated master file, and generate individual import files for entries that only exist in one of the two sources.

## What it does

1. Looks for exactly **2** valid Proton Authenticator export files in the target directory
2. Deduplicates entries across both files (preferring entries with notes when duplicates exist)
3. Writes a **merged master file** containing all unique entries
4. Writes **individual single-entry import files** for entries that only appear in one source
5. Prints an **import summary** showing which input file each unique entry is missing from, so you know which Proton Authenticator instance to import it into

All output is written to an `output/` subdirectory, keeping input and output cleanly separated.

## Prerequisites

- Python 3.10+ (no external dependencies for the script itself)
- For running tests: `pytest` and `hypothesis`

## Usage

```
python merge_proton_auth.py [directory]
```

| Argument | Description |
|----------|-------------|
| `directory` | Directory containing the two export files (default: current directory) |

### Examples

```bash
# Process files in current directory
python merge_proton_auth.py

# Process files in a specific directory
python merge_proton_auth.py ./my_exports
```

## Input requirements

- The target directory must contain **exactly 2** valid Proton Authenticator export files
- A valid export is a `.json` file with `{"version": 1, "entries": [...]}` structure
- Other `.json` files that don't match this structure are ignored
- If more than 2 valid exports are found, the script aborts with a message listing the files

## Output

All output goes into `output/` relative to the target directory:

| File | Description |
|------|-------------|
| `merged_proton_auth.json` | Deduplicated master file with all unique entries from both sources |
| `unique_<source>_<name>.json` | Individual import files for entries found in only one source |

If the `output/` directory already contains files that would be overwritten, you are prompted before proceeding.

## Filename suggestions

If your input files have the generic Proton Authenticator export name (`Proton Authenticator_export_<date>.json_<timestamp>.json`), the script will pause and suggest renaming them with meaningful prefixes like `mobile_` or `desktop_`. This makes the import summary clearer about which Proton instance each unique entry should be imported into. You can continue without renaming if you prefer.

## Import summary

After a successful run that produces unique entry files, the script prints a summary like:

```
--- Import Summary ---
The following single-entry files contain items missing from one of your inputs.
Import each file into the Proton Authenticator instance corresponding to the
file it is missing from:

  unique_desktop_Confluence_(JLR).json
    Entry: Confluence (JLR)
    Missing from: mobile-proton_authenticator_backup.json

  unique_mobile_mattermost.web-brainz.co.uk.json
    Entry: mattermost.web-brainz.co.uk
    Missing from: desktop-proton_authenticator_backup.json

----------------------

Alternatively, you can clear all entries from your Proton Authenticator
instance and import everything from 'merged_proton_auth.json' which contains
the complete deduplicated set of entries from both sources.
```

## Note for WSL users

Proton Authenticator running on Windows may fail to import files from a WSL filesystem path with a "forbidden path" error. If this happens, copy the output files to a Windows filesystem location and import from there:

```bash
cp output/* /mnt/c/Users/<your-username>/Desktop/
```

## Running tests

```bash
pip install pytest hypothesis
python -m pytest test_merge_proton_auth.py -v
```
