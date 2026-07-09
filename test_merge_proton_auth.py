"""Property-based tests for merge_proton_auth.py."""

import json as json_module
import shutil
import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st
from pathlib import Path
from unittest.mock import patch
import tempfile
import os

from merge_proton_auth import (
    discover_json_files, is_valid_export, validate_export_files,
    parse_entries, deduplicate, has_note, write_output, print_report, main, OUTPUT_FILENAME
)


# Strategy to generate valid JSON filenames (excluding the output filename)
json_filenames = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
    min_size=1,
    max_size=20,
).map(lambda s: s + ".json")


class TestOutputFileExclusion:
    """Property 2: Output File Exclusion.

    For any directory containing a file named `merged_proton_auth.json` alongside
    other `.json` files, the file discovery phase SHALL never include
    `merged_proton_auth.json` in the set of files to be validated or processed.

    **Validates: Requirements 2.1**
    """

    @given(
        filenames=st.lists(json_filenames, min_size=0, max_size=10),
        include_output=st.booleans(),
    )
    @settings(max_examples=200)
    def test_output_file_never_in_results(self, filenames, include_output):
        """discover_json_files never includes merged_proton_auth.json in results."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            # Create the generated JSON files
            for name in filenames:
                (tmp_path / name).write_text("{}", encoding="utf-8")

            # Optionally create the output file
            if include_output:
                (tmp_path / OUTPUT_FILENAME).write_text("{}", encoding="utf-8")

            # Call discover_json_files
            discovered, output_exists = discover_json_files(tmp_path)

            # Property: OUTPUT_FILENAME is never in the returned list
            discovered_names = [f.name for f in discovered]
            assert OUTPUT_FILENAME not in discovered_names

            # Also verify output_exists boolean correctness
            assert output_exists == include_output


class TestFileValidationCorrectness:
    """Property 1: File Validation Correctness.

    For any JSON value, is_valid_export returns True iff value is a dict
    with version == 1 and entries as a list.

    Validates: Requirements 3.1
    """

    @given(entries=st.lists(st.dictionaries(st.text(), st.text()), max_size=5))
    @settings(max_examples=200)
    def test_valid_exports_accepted(self, entries):
        """Valid structure always returns True."""
        data = {"version": 1, "entries": entries}
        assert is_valid_export(data) is True

    @given(data=st.one_of(
        st.none(),
        st.integers(),
        st.text(),
        st.lists(st.integers()),
        # dict with wrong version
        st.fixed_dictionaries({"version": st.integers().filter(lambda x: x != 1), "entries": st.just([])}),
        # dict missing entries
        st.fixed_dictionaries({"version": st.just(1)}),
        # dict where entries is not a list
        st.fixed_dictionaries({"version": st.just(1), "entries": st.one_of(st.text(), st.integers(), st.none())}),
    ))
    @settings(max_examples=200)
    def test_invalid_exports_rejected(self, data):
        """Invalid structures always return False."""
        assert is_valid_export(data) is False


# Strategy to generate different kinds of invalid file content
invalid_content_strategy = st.one_of(
    # Malformed JSON
    st.just("not json at all"),
    st.just("{invalid json"),
    st.just(""),
    # Dict without version key
    st.just('{"entries": []}'),
    # Dict with wrong version
    st.just('{"version": 2, "entries": []}'),
    st.just('{"version": "1", "entries": []}'),
    # Dict without entries key
    st.just('{"version": 1}'),
    # Dict where entries is not a list
    st.just('{"version": 1, "entries": "not a list"}'),
    st.just('{"version": 1, "entries": 42}'),
    st.just('{"version": 1, "entries": {}}'),
)


class TestStrictValidationAbort:
    """Property 3: Strict Validation Abort.

    For any set of discovered .json files where at least one fails validation,
    the script reports all failing filenames and exits without writing output.

    Validates: Requirements 3.2, 3.4
    """

    @given(
        valid_count=st.integers(min_value=0, max_value=3),
        invalid_count=st.integers(min_value=1, max_value=3),
        invalid_content=st.lists(invalid_content_strategy, min_size=1, max_size=3),
    )
    @settings(max_examples=100)
    def test_invalid_files_detected(self, valid_count, invalid_count, invalid_content):
        """When any file is invalid, invalid list is non-empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            files = []

            # Create valid files
            for i in range(valid_count):
                f = tmp_path / f"valid_{i}.json"
                f.write_text('{"version": 1, "entries": []}', encoding="utf-8")
                files.append(f)

            # Create invalid files using the generated content
            for i in range(invalid_count):
                f = tmp_path / f"invalid_{i}.json"
                content = invalid_content[i % len(invalid_content)]
                f.write_text(content, encoding="utf-8")
                files.append(f)

            valid, invalid = validate_export_files(files)

            # Property: when invalid files exist, invalid list is non-empty
            assert len(invalid) > 0
            assert len(invalid) == invalid_count
            assert len(valid) == valid_count

    @given(
        valid_count=st.integers(min_value=1, max_value=5),
    )
    @settings(max_examples=100)
    def test_all_valid_files_pass(self, valid_count):
        """When all files are valid, invalid list is empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            files = []

            for i in range(valid_count):
                f = tmp_path / f"export_{i}.json"
                f.write_text('{"version": 1, "entries": []}', encoding="utf-8")
                files.append(f)

            valid, invalid = validate_export_files(files)

            assert len(invalid) == 0
            assert len(valid) == valid_count

    @given(
        valid_count=st.integers(min_value=0, max_value=3),
        invalid_count=st.integers(min_value=1, max_value=3),
    )
    @settings(max_examples=100)
    def test_no_output_written_when_invalid(self, valid_count, invalid_count):
        """When any file is invalid, no output file is written."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            files = []

            # Create valid files
            for i in range(valid_count):
                f = tmp_path / f"valid_{i}.json"
                f.write_text('{"version": 1, "entries": []}', encoding="utf-8")
                files.append(f)

            # Create invalid files
            for i in range(invalid_count):
                f = tmp_path / f"invalid_{i}.json"
                f.write_text("not json at all", encoding="utf-8")
                files.append(f)

            valid, invalid = validate_export_files(files)

            # When invalid files exist, no output should be written
            # (the caller is responsible for aborting, but we verify the
            # validation function correctly identifies the invalid state)
            assert len(invalid) > 0
            output_path = tmp_path / OUTPUT_FILENAME
            assert not output_path.exists()


# Strategy for generating entry-like dicts with UUIDs
entry_strategy = st.fixed_dictionaries({
    "id": st.uuids().map(str),
    "content": st.fixed_dictionaries({
        "uri": st.text(min_size=1, max_size=50),
        "entry_type": st.just("Totp"),
        "name": st.text(min_size=1, max_size=30),
    }),
    "note": st.one_of(st.none(), st.just(""), st.text(min_size=1, max_size=50)),
})

# Strategy for (filename, entry) tuples
filename_entry_strategy = st.tuples(
    st.text(alphabet="abcdefghij", min_size=1, max_size=10).map(lambda s: s + ".json"),
    entry_strategy,
)


class TestDeduplicationUniqueness:
    """Property 5: Deduplication Uniqueness Invariant.

    For any set of input entries, the deduplicated output contains each unique
    Entry_ID exactly once, and the total count equals the number of distinct IDs.

    Validates: Requirements 5.1, 5.5
    """

    @given(entries=st.lists(filename_entry_strategy, min_size=0, max_size=20))
    @settings(max_examples=200)
    def test_unique_ids_in_output(self, entries):
        """Output contains each unique ID exactly once."""
        result = deduplicate(entries)

        # Each ID appears exactly once
        result_ids = [e["id"] for e in result]
        assert len(result_ids) == len(set(result_ids))

        # Count equals distinct input IDs
        input_ids = set(e["id"] for _, e in entries)
        assert len(result) == len(input_ids)

        # All input IDs are represented
        assert set(result_ids) == input_ids


class TestNotePreference:
    """Property 6: Note Preference in Deduplication.

    For entries sharing the same ID: if exactly one has a non-empty note,
    that version is kept; if none have notes, first-encountered wins;
    if multiple have notes, first among those with notes wins.

    Validates: Requirements 4.3, 5.2, 5.3, 5.4
    """

    @given(
        entry_id=st.uuids().map(str),
        note_text=st.text(min_size=1, max_size=50),
        num_no_note=st.integers(min_value=1, max_value=5),
    )
    @settings(max_examples=200)
    def test_entry_with_note_preferred_over_no_note(self, entry_id, note_text, num_no_note):
        """When exactly one entry has a note, it wins regardless of position."""
        # Create entries without notes
        entries_no_note = [
            (f"file_{i}.json", {"id": entry_id, "content": {"uri": "x", "entry_type": "Totp", "name": "test"}, "note": None})
            for i in range(num_no_note)
        ]
        # Create one entry with a note
        entry_with_note = ("noted_file.json", {"id": entry_id, "content": {"uri": "y", "entry_type": "Totp", "name": "noted"}, "note": note_text})

        # Put the noted entry at various positions
        for pos in range(len(entries_no_note) + 1):
            test_entries = entries_no_note[:pos] + [entry_with_note] + entries_no_note[pos:]
            result = deduplicate(test_entries)
            assert len(result) == 1
            assert result[0]["note"] == note_text

    @given(
        entry_id=st.uuids().map(str),
        num_entries=st.integers(min_value=2, max_value=5),
    )
    @settings(max_examples=200)
    def test_first_encountered_wins_when_no_notes(self, entry_id, num_entries):
        """When no entries have notes, first-encountered wins."""
        entries = [
            (f"file_{i}.json", {"id": entry_id, "content": {"uri": f"uri_{i}", "entry_type": "Totp", "name": f"name_{i}"}, "note": None})
            for i in range(num_entries)
        ]
        result = deduplicate(entries)
        assert len(result) == 1
        assert result[0]["content"]["uri"] == "uri_0"  # First encountered

    @given(
        entry_id=st.uuids().map(str),
        notes=st.lists(st.text(min_size=1, max_size=30), min_size=2, max_size=5),
    )
    @settings(max_examples=200)
    def test_first_noted_wins_when_multiple_have_notes(self, entry_id, notes):
        """When multiple entries have notes, first-encountered with a note wins."""
        entries = [
            (f"file_{i}.json", {"id": entry_id, "content": {"uri": f"uri_{i}", "entry_type": "Totp", "name": f"name_{i}"}, "note": note})
            for i, note in enumerate(notes)
        ]
        result = deduplicate(entries)
        assert len(result) == 1
        assert result[0]["note"] == notes[0]  # First with note wins



class TestContentPreservation:
    """Property 7: Content Preservation.

    For any entry in the merged output, its content field is structurally
    identical to the source entry from which it was kept.

    Validates: Requirements 7.4
    """

    @given(entries=st.lists(filename_entry_strategy, min_size=1, max_size=20))
    @settings(max_examples=200)
    def test_content_preserved_after_dedup(self, entries):
        """Content field of kept entries is never modified."""
        result = deduplicate(entries)

        # For each result entry, verify its content matches the expected source
        for result_entry in result:
            entry_id = result_entry["id"]
            # Find all source entries with this ID
            sources = [e for _, e in entries if e["id"] == entry_id]

            # Determine which source should have been kept
            # Logic: first with note wins; if none have notes, first overall wins
            noted_sources = [s for s in sources if has_note(s)]
            if noted_sources:
                expected = noted_sources[0]
            else:
                expected = sources[0]

            # Content must be structurally identical
            assert result_entry["content"] == expected["content"]
            assert result_entry["id"] == expected["id"]
            assert result_entry["note"] == expected["note"]


class TestOutputFormatValidity:
    """Property 8: Output Format Validity.

    For any successful merge, output is valid JSON with version == 1,
    entries as array, and 4-space indentation.

    Validates: Requirements 7.2, 7.3
    """

    @given(entries=st.lists(entry_strategy, min_size=0, max_size=10))
    @settings(max_examples=100)
    def test_output_is_valid_format(self, entries):
        """Written output file is valid JSON with correct structure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            write_output(tmp_path, entries)

            output_path = tmp_path / OUTPUT_FILENAME
            assert output_path.exists()

            content = output_path.read_text(encoding="utf-8")

            # Trailing newline
            assert content.endswith("\n")

            # Valid JSON
            data = json_module.loads(content)

            # Correct structure
            assert data["version"] == 1
            assert isinstance(data["entries"], list)
            assert len(data["entries"]) == len(entries)

            # 4-space indentation (check that "entries" key is indented)
            # The format should use 4-space indent
            lines = content.split("\n")
            # Find a line with entries content (if entries exist)
            if entries:
                # The "entries" key should be at 4-space indent
                assert any(line.startswith("    ") for line in lines)
                # Check that indented lines use multiples of 4 spaces
                for line in lines:
                    stripped = line.lstrip(" ")
                    if stripped and line != stripped:
                        indent = len(line) - len(stripped)
                        assert indent % 4 == 0, f"Non-4-space indent found: {indent} spaces"


import io
import sys


class TestReportingAccuracy:
    """Property 9: Reporting Accuracy.

    For F files with T total entries producing U unique entries:
    files_processed == F, total_entries == T, unique_entries == U,
    duplicates_resolved == T - U.

    Validates: Requirements 8.1, 8.2, 8.3, 8.4
    """

    @given(
        num_files=st.integers(min_value=1, max_value=100),
        total_entries=st.integers(min_value=0, max_value=1000),
    )
    @settings(max_examples=200)
    def test_report_numbers_correct(self, num_files, total_entries):
        """Report accurately reflects merge statistics."""
        # unique_entries must be <= total_entries
        unique_entries = total_entries  # simplification for no duplicates

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            print_report(num_files, total_entries, unique_entries)
        finally:
            sys.stdout = old_stdout

        output = captured.getvalue()
        assert f"Files processed: {num_files}" in output
        assert f"Total entries found: {total_entries}" in output
        assert f"Unique entries written: {unique_entries}" in output
        assert f"Duplicates resolved: {total_entries - unique_entries}" in output

    @given(
        num_files=st.integers(min_value=1, max_value=50),
        total_entries=st.integers(min_value=1, max_value=500),
        duplicate_count=st.integers(min_value=0, max_value=200),
    )
    @settings(max_examples=200)
    def test_duplicates_calculated_correctly(self, num_files, total_entries, duplicate_count):
        """Duplicates resolved = total - unique."""
        assume(duplicate_count <= total_entries)
        unique_entries = total_entries - duplicate_count

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            print_report(num_files, total_entries, unique_entries)
        finally:
            sys.stdout = old_stdout

        output = captured.getvalue()
        assert f"Duplicates resolved: {duplicate_count}" in output



class TestIntegration:
    """Integration tests using real example files.

    Validates: Requirements 1.1, 2.2, 2.3, 3.2, 3.4, 5.1, 5.2, 6.6, 7.1, 7.2
    """

    def setup_example_dir(self, tmp_path):
        """Copy example files to a temp directory."""
        examples_dir = Path(__file__).parent / "examples"
        for f in examples_dir.glob("*.json"):
            shutil.copy(f, tmp_path / f.name)
        return tmp_path

    def test_full_merge_pipeline(self, tmp_path):
        """Full pipeline produces correct merged output."""
        self.setup_example_dir(tmp_path)

        files, output_exists = discover_json_files(tmp_path)
        assert len(files) == 2
        assert output_exists is False

        valid, invalid = validate_export_files(files)
        assert len(valid) == 2
        assert len(invalid) == 0

        entries = parse_entries(valid)
        assert len(entries) == 34  # 17 + 17

        unique = deduplicate(entries)
        assert len(unique) == 18  # 16 shared + 1 mobile-only + 1 desktop-only

        write_output(tmp_path, unique)
        output_path = tmp_path / OUTPUT_FILENAME
        assert output_path.exists()

        data = json_module.loads(output_path.read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert len(data["entries"]) == 18

    def test_merged_output_contains_mobile_only_entry(self, tmp_path):
        """Merged output includes the mobile-only entry (mattermost)."""
        self.setup_example_dir(tmp_path)

        files, _ = discover_json_files(tmp_path)
        valid, _ = validate_export_files(files)
        entries = parse_entries(valid)
        unique = deduplicate(entries)

        unique_ids = {e["id"] for e in unique}
        # Mobile-only entry: mattermost
        assert "a7e1d804-7e24-4db2-9508-b3eb301f123b" in unique_ids

    def test_merged_output_contains_desktop_only_entry(self, tmp_path):
        """Merged output includes the desktop-only entry (NEW confluence)."""
        self.setup_example_dir(tmp_path)

        files, _ = discover_json_files(tmp_path)
        valid, _ = validate_export_files(files)
        entries = parse_entries(valid)
        unique = deduplicate(entries)

        unique_ids = {e["id"] for e in unique}
        # Desktop-only entry: NEW confluence
        assert "7f1f51da-0fed-4a87-a15c-e024ab443a95" in unique_ids

    def test_duplicate_note_preference(self, tmp_path):
        """Duplicates resolved: both null and '' are 'no note', first-encountered wins."""
        self.setup_example_dir(tmp_path)

        files, _ = discover_json_files(tmp_path)
        valid, _ = validate_export_files(files)
        entries = parse_entries(valid)
        unique = deduplicate(entries)

        # For shared entries, neither has a meaningful note (null vs "").
        # First-encountered (by alphabetical file order) wins.
        # Desktop file (Proton Authenticator_export...) sorts before mobile file.
        # So shared entries should have note == "" (from desktop, processed first).
        shared_id = "e1bdeffc-dc78-4d32-9e4f-01dcccc97a56"  # mbutter9, in both
        matched = [e for e in unique if e["id"] == shared_id]
        assert len(matched) == 1
        # Desktop is processed first (alphabetically), so note should be ""
        assert matched[0]["note"] == ""

    def test_output_file_structure(self, tmp_path):
        """Output file has correct JSON structure with 4-space indentation."""
        self.setup_example_dir(tmp_path)

        files, _ = discover_json_files(tmp_path)
        valid, _ = validate_export_files(files)
        entries = parse_entries(valid)
        unique = deduplicate(entries)
        write_output(tmp_path, unique)

        output_path = tmp_path / OUTPUT_FILENAME
        content = output_path.read_text(encoding="utf-8")

        # Trailing newline
        assert content.endswith("\n")

        # Valid JSON
        data = json_module.loads(content)
        assert data["version"] == 1
        assert isinstance(data["entries"], list)

        # 4-space indentation check
        lines = content.split("\n")
        for line in lines:
            stripped = line.lstrip(" ")
            if stripped and line != stripped:
                indent = len(line) - len(stripped)
                assert indent % 4 == 0

    def test_main_with_confirmation_yes(self, tmp_path):
        """main() produces output when user confirms."""
        self.setup_example_dir(tmp_path)
        with patch('builtins.input', return_value='y'):
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                result = main()
        assert result == 0
        assert (tmp_path / OUTPUT_FILENAME).exists()

        data = json_module.loads((tmp_path / OUTPUT_FILENAME).read_text(encoding="utf-8"))
        assert len(data["entries"]) == 18

    def test_main_with_confirmation_no(self, tmp_path):
        """main() produces no output when user declines."""
        self.setup_example_dir(tmp_path)
        with patch('builtins.input', return_value='n'):
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                result = main()
        assert result == 0
        assert not (tmp_path / OUTPUT_FILENAME).exists()

    def test_overwrite_prompt_decline(self, tmp_path):
        """main() respects overwrite decline when output file exists."""
        self.setup_example_dir(tmp_path)
        # Create existing output file
        (tmp_path / OUTPUT_FILENAME).write_text('{"version": 1, "entries": []}', encoding='utf-8')
        with patch('builtins.input', return_value='n'):
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                result = main()
        assert result == 0
        # Output should still contain the original content (not overwritten)
        data = json_module.loads((tmp_path / OUTPUT_FILENAME).read_text(encoding="utf-8"))
        assert len(data["entries"]) == 0  # Still the old content

    def test_overwrite_prompt_accept(self, tmp_path):
        """main() overwrites output file when user accepts both prompts."""
        self.setup_example_dir(tmp_path)
        # Create existing output file
        (tmp_path / OUTPUT_FILENAME).write_text('{"version": 1, "entries": []}', encoding='utf-8')
        with patch('builtins.input', return_value='y'):
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                result = main()
        assert result == 0
        # Output should now contain merged entries
        data = json_module.loads((tmp_path / OUTPUT_FILENAME).read_text(encoding="utf-8"))
        assert len(data["entries"]) == 18

    def test_validation_abort_with_invalid_file(self, tmp_path):
        """main() aborts when invalid file present."""
        self.setup_example_dir(tmp_path)
        (tmp_path / "bad_file.json").write_text("not json", encoding="utf-8")
        with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
            result = main()
        assert result == 1
        assert not (tmp_path / OUTPUT_FILENAME).exists()

    def test_validation_abort_with_wrong_structure(self, tmp_path):
        """main() aborts when a JSON file has wrong structure."""
        self.setup_example_dir(tmp_path)
        (tmp_path / "wrong_structure.json").write_text(
            '{"version": 2, "data": []}', encoding="utf-8"
        )
        with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
            result = main()
        assert result == 1
        assert not (tmp_path / OUTPUT_FILENAME).exists()



class TestUserDeclineCleanExit:
    """Property 10: User Decline Clean Exit.

    For any prompt where the user declines, script exits with code 0,
    prints no-files-written message, and does not write/modify any files.

    Validates: Requirements 2.4, 6.7
    """

    def test_overwrite_decline_no_files_written(self, tmp_path, capsys):
        """Declining overwrite prompt exits cleanly with no file changes."""
        # Create a valid export file
        export = {"version": 1, "entries": [{"id": "test-id", "content": {"uri": "x", "entry_type": "Totp", "name": "test"}, "note": None}]}
        (tmp_path / "test.json").write_text(json_module.dumps(export), encoding="utf-8")

        # Create existing output file with known content
        original_content = "original content"
        (tmp_path / OUTPUT_FILENAME).write_text(original_content, encoding="utf-8")

        with patch('builtins.input', return_value='n'):
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                result = main()

        assert result == 0
        captured = capsys.readouterr()
        assert "No files were written." in captured.out
        # Output file unchanged
        assert (tmp_path / OUTPUT_FILENAME).read_text(encoding="utf-8") == original_content

    def test_confirmation_decline_no_files_written(self, tmp_path, capsys):
        """Declining confirmation prompt exits cleanly with no file changes."""
        # Create a valid export file
        export = {"version": 1, "entries": [{"id": "test-id", "content": {"uri": "x", "entry_type": "Totp", "name": "test"}, "note": None}]}
        (tmp_path / "test.json").write_text(json_module.dumps(export), encoding="utf-8")

        # No existing output file - so overwrite prompt won't fire, only confirmation
        with patch('builtins.input', return_value='n'):
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                result = main()

        assert result == 0
        captured = capsys.readouterr()
        assert "No files were written." in captured.out
        # No output file created
        assert not (tmp_path / OUTPUT_FILENAME).exists()

    @given(
        num_entries=st.integers(min_value=1, max_value=5),
        decline_response=st.sampled_from(["n", "N", "no", "NO", "No", "", "x", "nope"]),
    )
    @settings(max_examples=50)
    def test_any_non_yes_response_declines(self, num_entries, decline_response):
        """Any response other than y/yes results in decline."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            # Create valid export files
            export = {"version": 1, "entries": [
                {"id": f"id-{i}", "content": {"uri": "x", "entry_type": "Totp", "name": f"name-{i}"}, "note": None}
                for i in range(num_entries)
            ]}
            (tmp_path / "test.json").write_text(json_module.dumps(export), encoding="utf-8")

            with patch('builtins.input', return_value=decline_response):
                with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                    result = main()

            assert result == 0
            assert not (tmp_path / OUTPUT_FILENAME).exists()
