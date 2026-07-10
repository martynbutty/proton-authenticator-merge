"""Tests for merge_proton_auth.py."""

import json as json_module
import io
import shutil
import sys
import tempfile
import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st
from pathlib import Path
from unittest.mock import patch

from merge_proton_auth import (
    discover_json_files, is_valid_export, validate_export_files,
    parse_entries, deduplicate, has_note, write_output, write_single_entry_files,
    print_report, print_missing_from_summary, main,
    OUTPUT_FILENAME, OUTPUT_DIR_NAME, GENERIC_EXPORT_PATTERN,
    identify_unique_entries, generate_unique_filenames,
    check_output_dir_conflicts, has_generic_export_names,
    determine_missing_from, sanitise_filename_part, derive_entry_name,
)


# --- Strategies ---

json_filenames = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
    min_size=1,
    max_size=20,
).map(lambda s: s + ".json")

entry_strategy = st.fixed_dictionaries({
    "id": st.uuids().map(str),
    "content": st.fixed_dictionaries({
        "uri": st.text(min_size=1, max_size=50),
        "entry_type": st.just("Totp"),
        "name": st.text(min_size=1, max_size=30),
    }),
    "note": st.one_of(st.none(), st.just(""), st.text(min_size=1, max_size=50)),
})

filename_entry_strategy = st.tuples(
    st.text(alphabet="abcdefghij", min_size=1, max_size=10).map(lambda s: s + ".json"),
    entry_strategy,
)


# --- Helper ---

def make_export(entries):
    """Create a valid Proton Authenticator export dict."""
    return {"version": 1, "entries": entries}


def write_export_file(path, entries):
    """Write a valid export file at the given path."""
    path.write_text(
        json_module.dumps(make_export(entries), indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# --- Tests ---


class TestDiscoverJsonFiles:
    """Tests for discover_json_files."""

    def test_finds_json_files(self, tmp_path):
        """Finds .json files in directory."""
        (tmp_path / "a.json").write_text("{}", encoding="utf-8")
        (tmp_path / "b.json").write_text("{}", encoding="utf-8")
        (tmp_path / "readme.txt").write_text("hi", encoding="utf-8")
        result = discover_json_files(tmp_path)
        names = [f.name for f in result]
        assert "a.json" in names
        assert "b.json" in names
        assert "readme.txt" not in names

    def test_returns_sorted(self, tmp_path):
        """Results are sorted alphabetically."""
        (tmp_path / "z.json").write_text("{}", encoding="utf-8")
        (tmp_path / "a.json").write_text("{}", encoding="utf-8")
        result = discover_json_files(tmp_path)
        assert result[0].name == "a.json"
        assert result[1].name == "z.json"

    def test_does_not_recurse_into_subdirs(self, tmp_path):
        """Does not find .json files in subdirectories."""
        sub = tmp_path / "output"
        sub.mkdir()
        (sub / "nested.json").write_text("{}", encoding="utf-8")
        (tmp_path / "top.json").write_text("{}", encoding="utf-8")
        result = discover_json_files(tmp_path)
        names = [f.name for f in result]
        assert "top.json" in names
        assert "nested.json" not in names


class TestFileValidation:
    """Tests for is_valid_export and validate_export_files."""

    @given(entries=st.lists(st.dictionaries(st.text(), st.text()), max_size=5))
    @settings(max_examples=200)
    def test_valid_exports_accepted(self, entries):
        """Valid structure always returns True."""
        data = {"version": 1, "entries": entries}
        assert is_valid_export(data) is True

    @given(data=st.one_of(
        st.none(), st.integers(), st.text(), st.lists(st.integers()),
        st.fixed_dictionaries({"version": st.integers().filter(lambda x: x != 1), "entries": st.just([])}),
        st.fixed_dictionaries({"version": st.just(1)}),
        st.fixed_dictionaries({"version": st.just(1), "entries": st.one_of(st.text(), st.integers(), st.none())}),
    ))
    @settings(max_examples=200)
    def test_invalid_exports_rejected(self, data):
        """Invalid structures always return False."""
        assert is_valid_export(data) is False

    def test_validate_separates_valid_and_invalid(self, tmp_path):
        """validate_export_files correctly categorises files."""
        valid_file = tmp_path / "good.json"
        valid_file.write_text('{"version": 1, "entries": []}', encoding="utf-8")
        invalid_file = tmp_path / "bad.json"
        invalid_file.write_text("not json", encoding="utf-8")

        valid, invalid = validate_export_files([valid_file, invalid_file])
        assert len(valid) == 1
        assert len(invalid) == 1
        assert valid[0].name == "good.json"
        assert invalid[0] == "bad.json"


class TestGenericFilenameDetection:
    """Tests for has_generic_export_names and GENERIC_EXPORT_PATTERN."""

    def test_matches_generic_pattern(self):
        """Generic Proton export filenames are detected."""
        generic = Path("Proton Authenticator_export_2026-07-09.json_1783587600.json")
        assert GENERIC_EXPORT_PATTERN.match(generic.name)

    def test_does_not_match_custom_names(self):
        """Custom/renamed filenames are not flagged."""
        custom = Path("mobile-proton_authenticator_backup.json")
        assert not GENERIC_EXPORT_PATTERN.match(custom.name)

    def test_has_generic_export_names_filters(self, tmp_path):
        """has_generic_export_names returns only generic-named files."""
        generic = tmp_path / "Proton Authenticator_export_2026-07-09.json_1783587600.json"
        custom = tmp_path / "mobile_backup.json"
        generic.write_text("{}", encoding="utf-8")
        custom.write_text("{}", encoding="utf-8")

        result = has_generic_export_names([generic, custom])
        assert len(result) == 1
        assert result[0] == generic


class TestExactlyTwoFilesEnforcement:
    """Tests for the 2-file requirement in main()."""

    def test_aborts_with_zero_valid_files(self, tmp_path):
        """Exits with error when no valid export files found."""
        (tmp_path / "not_export.json").write_text('{"bad": true}', encoding="utf-8")
        with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
            result = main()
        assert result == 1

    def test_aborts_with_one_valid_file(self, tmp_path):
        """Exits with error when only 1 valid export file found."""
        write_export_file(tmp_path / "only_one.json", [{"id": "a", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}])
        with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
            result = main()
        assert result == 1

    def test_aborts_with_three_valid_files(self, tmp_path, capsys):
        """Exits with error when more than 2 valid export files found."""
        for i in range(3):
            write_export_file(
                tmp_path / f"file{i}.json",
                [{"id": f"id-{i}", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}],
            )
        with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
            result = main()
        assert result == 1
        captured = capsys.readouterr()
        assert "Expected exactly 2" in captured.err

    def test_succeeds_with_two_valid_files(self, tmp_path):
        """Succeeds when exactly 2 valid export files are present."""
        write_export_file(
            tmp_path / "file1.json",
            [{"id": "shared", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}],
        )
        write_export_file(
            tmp_path / "file2.json",
            [{"id": "shared", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}],
        )
        with patch('builtins.input', return_value='y'):
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                result = main()
        assert result == 0
        assert (tmp_path / OUTPUT_DIR_NAME / OUTPUT_FILENAME).exists()


class TestOutputDirectory:
    """Tests for output going to the output/ subdirectory."""

    def test_output_dir_created(self, tmp_path):
        """output/ directory is created if it doesn't exist."""
        write_export_file(
            tmp_path / "a.json",
            [{"id": "1", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}],
        )
        write_export_file(
            tmp_path / "b.json",
            [{"id": "1", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}],
        )
        with patch('builtins.input', return_value='y'):
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                main()
        assert (tmp_path / OUTPUT_DIR_NAME).is_dir()

    def test_merged_file_in_output_dir(self, tmp_path):
        """Merged file is written inside output/ not the source directory."""
        write_export_file(
            tmp_path / "a.json",
            [{"id": "1", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}],
        )
        write_export_file(
            tmp_path / "b.json",
            [{"id": "1", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}],
        )
        with patch('builtins.input', return_value='y'):
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                main()
        assert (tmp_path / OUTPUT_DIR_NAME / OUTPUT_FILENAME).exists()
        assert not (tmp_path / OUTPUT_FILENAME).exists()

    def test_unique_files_in_output_dir(self, tmp_path):
        """Single-entry import files are written inside output/."""
        write_export_file(
            tmp_path / "mobile.json",
            [
                {"id": "shared", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None},
                {"id": "mob-only", "content": {"uri": "y", "entry_type": "Totp", "name": "mobileapp"}, "note": None},
            ],
        )
        write_export_file(
            tmp_path / "desktop.json",
            [{"id": "shared", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}],
        )
        with patch('builtins.input', return_value='y'):
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                main()
        output_dir = tmp_path / OUTPUT_DIR_NAME
        unique_files = list(output_dir.glob("unique_*.json"))
        assert len(unique_files) == 1


class TestOverwriteWarning:
    """Tests for overwrite confirmation when output/ already has files."""

    def test_warns_and_aborts_on_decline(self, tmp_path):
        """Declining overwrite prevents any files being written."""
        write_export_file(
            tmp_path / "a.json",
            [{"id": "1", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}],
        )
        write_export_file(
            tmp_path / "b.json",
            [{"id": "1", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}],
        )
        # Pre-create output dir with existing file
        output_dir = tmp_path / OUTPUT_DIR_NAME
        output_dir.mkdir()
        (output_dir / OUTPUT_FILENAME).write_text("old data", encoding="utf-8")

        # First input: confirm merge (y), second input: decline overwrite (n)
        with patch('builtins.input', side_effect=['y', 'n']):
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                result = main()
        assert result == 0
        # File should still have old content
        assert (output_dir / OUTPUT_FILENAME).read_text(encoding="utf-8") == "old data"

    def test_overwrites_on_accept(self, tmp_path):
        """Accepting overwrite replaces existing files."""
        write_export_file(
            tmp_path / "a.json",
            [{"id": "1", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}],
        )
        write_export_file(
            tmp_path / "b.json",
            [{"id": "1", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}],
        )
        output_dir = tmp_path / OUTPUT_DIR_NAME
        output_dir.mkdir()
        (output_dir / OUTPUT_FILENAME).write_text("old data", encoding="utf-8")

        with patch('builtins.input', return_value='y'):
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                result = main()
        assert result == 0
        data = json_module.loads((output_dir / OUTPUT_FILENAME).read_text(encoding="utf-8"))
        assert data["version"] == 1

    def test_no_warning_when_output_dir_empty(self, tmp_path):
        """No overwrite warning when output dir doesn't exist yet."""
        write_export_file(
            tmp_path / "a.json",
            [{"id": "1", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}],
        )
        write_export_file(
            tmp_path / "b.json",
            [{"id": "1", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}],
        )
        # Only one input call needed (confirm merge), no overwrite prompt
        with patch('builtins.input', return_value='y') as mock_input:
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                result = main()
        assert result == 0
        # input called exactly once (confirm merge only)
        assert mock_input.call_count == 1


class TestCheckOutputDirConflicts:
    """Tests for check_output_dir_conflicts."""

    def test_no_conflicts(self, tmp_path):
        """Returns empty list when no files exist."""
        assert check_output_dir_conflicts(tmp_path, ["a.json", "b.json"]) == []

    def test_some_conflicts(self, tmp_path):
        """Returns only the filenames that exist."""
        (tmp_path / "a.json").write_text("{}", encoding="utf-8")
        result = check_output_dir_conflicts(tmp_path, ["a.json", "b.json"])
        assert result == ["a.json"]

    def test_all_conflicts(self, tmp_path):
        """Returns all filenames when all exist."""
        (tmp_path / "a.json").write_text("{}", encoding="utf-8")
        (tmp_path / "b.json").write_text("{}", encoding="utf-8")
        result = check_output_dir_conflicts(tmp_path, ["a.json", "b.json"])
        assert result == ["a.json", "b.json"]


class TestDeduplication:
    """Tests for deduplicate function."""

    @given(entries=st.lists(filename_entry_strategy, min_size=0, max_size=20))
    @settings(max_examples=200)
    def test_unique_ids_in_output(self, entries):
        """Output contains each unique ID exactly once."""
        result = deduplicate(entries)
        result_ids = [e["id"] for e in result]
        assert len(result_ids) == len(set(result_ids))
        input_ids = set(e["id"] for _, e in entries)
        assert len(result) == len(input_ids)
        assert set(result_ids) == input_ids

    @given(
        entry_id=st.uuids().map(str),
        note_text=st.text(min_size=1, max_size=50),
        num_no_note=st.integers(min_value=1, max_value=5),
    )
    @settings(max_examples=200)
    def test_entry_with_note_preferred(self, entry_id, note_text, num_no_note):
        """When exactly one entry has a note, it wins regardless of position."""
        entries_no_note = [
            (f"file_{i}.json", {"id": entry_id, "content": {"uri": "x", "entry_type": "Totp", "name": "test"}, "note": None})
            for i in range(num_no_note)
        ]
        entry_with_note = ("noted.json", {"id": entry_id, "content": {"uri": "y", "entry_type": "Totp", "name": "noted"}, "note": note_text})
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
        assert result[0]["content"]["uri"] == "uri_0"


class TestIdentifyUniqueEntries:
    """Tests for identify_unique_entries."""

    def test_basic_unique_identification(self):
        """Entries in only one file are identified as unique."""
        entries = [
            ("file1.json", {"id": "aaa", "content": {"uri": "x"}, "note": None}),
            ("file1.json", {"id": "bbb", "content": {"uri": "y"}, "note": None}),
            ("file2.json", {"id": "aaa", "content": {"uri": "x"}, "note": None}),
            ("file2.json", {"id": "ccc", "content": {"uri": "z"}, "note": None}),
        ]
        result = identify_unique_entries(entries)
        assert len(result) == 2
        ids = [e["id"] for _, e in result]
        assert "bbb" in ids
        assert "ccc" in ids

    def test_no_unique_entries(self):
        """When all entries are shared, result is empty."""
        entries = [
            ("file1.json", {"id": "aaa", "content": {"uri": "x"}, "note": None}),
            ("file2.json", {"id": "aaa", "content": {"uri": "x"}, "note": None}),
        ]
        result = identify_unique_entries(entries)
        assert len(result) == 0

    def test_empty_input(self):
        """Empty input returns empty result."""
        assert identify_unique_entries([]) == []

    def test_sorted_by_source_then_id(self):
        """Results sorted by (source_filename, entry_id)."""
        entries = [
            ("z.json", {"id": "zzz", "content": {"uri": "a"}, "note": None}),
            ("a.json", {"id": "mmm", "content": {"uri": "b"}, "note": None}),
            ("a.json", {"id": "aaa", "content": {"uri": "c"}, "note": None}),
        ]
        result = identify_unique_entries(entries)
        assert result[0] == ("a.json", {"id": "aaa", "content": {"uri": "c"}, "note": None})
        assert result[1] == ("a.json", {"id": "mmm", "content": {"uri": "b"}, "note": None})
        assert result[2] == ("z.json", {"id": "zzz", "content": {"uri": "a"}, "note": None})


class TestDetermineMissingFrom:
    """Tests for determine_missing_from."""

    def test_returns_other_file(self):
        """Returns the file the entry is NOT in."""
        assert determine_missing_from("mobile.json", ["mobile.json", "desktop.json"]) == "desktop.json"
        assert determine_missing_from("desktop.json", ["mobile.json", "desktop.json"]) == "mobile.json"

    def test_returns_unknown_for_single_file(self):
        """Returns 'unknown' if source is the only file (edge case)."""
        assert determine_missing_from("only.json", ["only.json"]) == "unknown"


class TestGenerateUniqueFilenames:
    """Tests for generate_unique_filenames."""

    def test_basic_filename_generation(self):
        """Template: unique_<source_stem>_<descriptive>.json"""
        entries = [
            ("mobile-backup.json", {"id": "a", "content": {"uri": "otpauth://totp/x?secret=ABC&issuer=Google", "entry_type": "Totp", "name": "t"}, "note": "My AWS"}),
        ]
        result = generate_unique_filenames(entries)
        assert result[0][2] == "unique_mobile-backup_My_AWS.json"

    def test_collision_resolution(self):
        """Duplicate filenames get _2, _3 suffixes."""
        entries = [
            ("src.json", {"id": "a", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": "Same"}),
            ("src.json", {"id": "b", "content": {"uri": "y", "entry_type": "Totp", "name": "t"}, "note": "Same"}),
            ("src.json", {"id": "c", "content": {"uri": "z", "entry_type": "Totp", "name": "t"}, "note": "Same"}),
        ]
        result = generate_unique_filenames(entries)
        assert result[0][2] == "unique_src_Same.json"
        assert result[1][2] == "unique_src_Same_2.json"
        assert result[2][2] == "unique_src_Same_3.json"

    def test_different_sources_no_collision(self):
        """Different source stems produce different filenames."""
        entries = [
            ("file1.json", {"id": "a", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": "Same"}),
            ("file2.json", {"id": "b", "content": {"uri": "y", "entry_type": "Totp", "name": "t"}, "note": "Same"}),
        ]
        result = generate_unique_filenames(entries)
        assert result[0][2] == "unique_file1_Same.json"
        assert result[1][2] == "unique_file2_Same.json"

    def test_empty_input(self):
        """Empty input returns empty result."""
        assert generate_unique_filenames([]) == []


class TestWriteOutput:
    """Tests for write_output."""

    @given(entries=st.lists(entry_strategy, min_size=0, max_size=10))
    @settings(max_examples=100)
    def test_output_is_valid_format(self, entries):
        """Written output is valid JSON with correct structure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            write_output(tmp_path, entries)
            output_path = tmp_path / OUTPUT_FILENAME
            content = output_path.read_text(encoding="utf-8")
            assert content.endswith("\n")
            data = json_module.loads(content)
            assert data["version"] == 1
            assert isinstance(data["entries"], list)
            assert len(data["entries"]) == len(entries)


class TestWriteSingleEntryFiles:
    """Tests for write_single_entry_files."""

    def test_writes_valid_export(self, tmp_path):
        """Each file is a valid Proton export with one entry."""
        entry = {"id": "abc", "content": {"uri": "x", "entry_type": "Totp", "name": "test"}, "note": None}
        file_entries = [("source.json", entry, "unique_source_test.json")]
        result = write_single_entry_files(tmp_path, file_entries)
        assert result == ["unique_source_test.json"]
        data = json_module.loads((tmp_path / "unique_source_test.json").read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert len(data["entries"]) == 1
        assert data["entries"][0] == entry

    def test_4_space_indent_and_trailing_newline(self, tmp_path):
        """Output uses 4-space indent and trailing newline."""
        entry = {"id": "a", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}
        write_single_entry_files(tmp_path, [("src.json", entry, "out.json")])
        content = (tmp_path / "out.json").read_text(encoding="utf-8")
        assert content.endswith("\n")
        for line in content.split("\n"):
            stripped = line.lstrip(" ")
            if stripped and line != stripped:
                indent = len(line) - len(stripped)
                assert indent % 4 == 0

    def test_empty_input(self, tmp_path):
        """Empty input produces no files."""
        assert write_single_entry_files(tmp_path, []) == []

    def test_overwrites_existing(self, tmp_path):
        """Overwrites existing files."""
        (tmp_path / "out.json").write_text("old", encoding="utf-8")
        entry = {"id": "new", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}
        write_single_entry_files(tmp_path, [("src.json", entry, "out.json")])
        data = json_module.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
        assert data["entries"][0]["id"] == "new"


class TestPrintReport:
    """Tests for print_report."""

    def test_report_numbers(self, capsys, tmp_path):
        """Report shows correct statistics."""
        print_report(2, 34, 18, ["unique_a.json", "unique_b.json"], tmp_path)
        output = capsys.readouterr().out
        assert "Files processed: 2" in output
        assert "Total entries found: 34" in output
        assert "Unique entries written to merged file: 18" in output
        assert "Duplicates resolved: 16" in output
        assert "Single-entry import files created: 2" in output

    def test_no_unique_entries_message(self, capsys, tmp_path):
        """Shows 'no single-entry files needed' when list is empty."""
        print_report(2, 10, 10, [], tmp_path)
        output = capsys.readouterr().out
        assert "No single-entry import files needed" in output


class TestPrintMissingFromSummary:
    """Tests for print_missing_from_summary."""

    def test_shows_missing_from_info(self, capsys):
        """Summary shows which file each entry is missing from."""
        file_entries = [
            ("mobile.json", {"id": "a", "content": {"uri": "otpauth://totp/x?secret=ABC&issuer=MyService", "entry_type": "Totp", "name": "test"}, "note": "My App"}, "unique_mobile_My_App.json"),
        ]
        all_filenames = ["mobile.json", "desktop.json"]
        print_missing_from_summary(file_entries, all_filenames)
        output = capsys.readouterr().out
        assert "unique_mobile_My_App.json" in output
        assert "Missing from: desktop.json" in output
        assert "Entry: My App" in output

    def test_no_output_when_empty(self, capsys):
        """No output when no unique entries."""
        print_missing_from_summary([], ["a.json", "b.json"])
        output = capsys.readouterr().out
        assert output == ""


class TestUserDecline:
    """Tests for user declining at various prompts."""

    def test_decline_merge_confirmation(self, tmp_path, capsys):
        """Declining merge confirmation writes nothing."""
        write_export_file(
            tmp_path / "a.json",
            [{"id": "1", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}],
        )
        write_export_file(
            tmp_path / "b.json",
            [{"id": "1", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}],
        )
        with patch('builtins.input', return_value='n'):
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                result = main()
        assert result == 0
        assert not (tmp_path / OUTPUT_DIR_NAME).exists()

    def test_decline_generic_filename_warning(self, tmp_path, capsys):
        """Declining generic filename warning aborts cleanly."""
        write_export_file(
            tmp_path / "Proton Authenticator_export_2026-07-09.json_1783587600.json",
            [{"id": "1", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}],
        )
        write_export_file(
            tmp_path / "Proton Authenticator_export_2026-07-10.json_1783674000.json",
            [{"id": "2", "content": {"uri": "y", "entry_type": "Totp", "name": "t2"}, "note": None}],
        )
        with patch('builtins.input', return_value='n'):
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                result = main()
        assert result == 0
        captured = capsys.readouterr()
        assert "Rename your files" in captured.out
        assert not (tmp_path / OUTPUT_DIR_NAME).exists()


class TestIntegration:
    """Integration tests using real example files."""

    def setup_example_dir(self, tmp_path):
        """Copy example files to a temp directory."""
        examples_dir = Path(__file__).parent / "examples"
        for f in examples_dir.glob("*.json"):
            # Skip any previously generated output files
            if f.name.startswith("merged_") or f.name.startswith("unique_"):
                continue
            shutil.copy(f, tmp_path / f.name)
        return tmp_path

    def test_full_pipeline(self, tmp_path):
        """Full pipeline produces merged output and unique entry files."""
        self.setup_example_dir(tmp_path)

        # The generic-named file will trigger the warning, accept it + merge
        with patch('builtins.input', return_value='y'):
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                result = main()

        assert result == 0
        output_dir = tmp_path / OUTPUT_DIR_NAME
        assert output_dir.is_dir()
        assert (output_dir / OUTPUT_FILENAME).exists()

        data = json_module.loads((output_dir / OUTPUT_FILENAME).read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert len(data["entries"]) == 18  # 16 shared + 2 unique

    def test_unique_entry_files_created(self, tmp_path):
        """Unique entry files are created for entries in only one source."""
        self.setup_example_dir(tmp_path)

        with patch('builtins.input', return_value='y'):
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                main()

        output_dir = tmp_path / OUTPUT_DIR_NAME
        unique_files = list(output_dir.glob("unique_*.json"))
        # Should have 2 unique entries: one mobile-only, one desktop-only
        assert len(unique_files) == 2

        # Each unique file is a valid single-entry export
        for uf in unique_files:
            data = json_module.loads(uf.read_text(encoding="utf-8"))
            assert data["version"] == 1
            assert len(data["entries"]) == 1

    def test_missing_from_summary_printed(self, tmp_path, capsys):
        """Import summary is printed showing which file entries are missing from."""
        self.setup_example_dir(tmp_path)

        with patch('builtins.input', return_value='y'):
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                main()

        output = capsys.readouterr().out
        assert "Import Summary" in output
        assert "Missing from:" in output

    def test_rerun_with_existing_output(self, tmp_path):
        """Re-running with existing output/ prompts for overwrite."""
        self.setup_example_dir(tmp_path)

        # First run
        with patch('builtins.input', return_value='y'):
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                main()

        # Second run - accept all prompts
        with patch('builtins.input', return_value='y'):
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                result = main()

        assert result == 0
        # Files should still be there (overwritten)
        assert (tmp_path / OUTPUT_DIR_NAME / OUTPUT_FILENAME).exists()

    def test_output_dir_does_not_affect_input_discovery(self, tmp_path):
        """Files in output/ subdirectory don't count as input files."""
        self.setup_example_dir(tmp_path)

        # Create output dir with extra json files that look like exports
        output_dir = tmp_path / OUTPUT_DIR_NAME
        output_dir.mkdir()
        write_export_file(
            output_dir / "decoy.json",
            [{"id": "decoy", "content": {"uri": "x", "entry_type": "Totp", "name": "t"}, "note": None}],
        )

        with patch('builtins.input', return_value='y'):
            with patch('sys.argv', ['merge_proton_auth', str(tmp_path)]):
                result = main()

        # Should succeed - only the 2 input files in the root dir are found
        assert result == 0


class TestSanitiseFilenamePart:
    """Tests for sanitise_filename_part."""

    def test_replaces_unsafe_chars(self):
        """Unsafe characters replaced with underscore."""
        assert sanitise_filename_part("a/b\\c:d*e") == "a_b_c_d_e"
        assert sanitise_filename_part("hello world") == "hello_world"
        assert sanitise_filename_part('a"b<c>d|e') == "a_b_c_d_e"

    def test_truncates_to_max_length(self):
        """Output truncated to MAX_DESCRIPTIVE_LENGTH."""
        long_str = "a" * 200
        result = sanitise_filename_part(long_str)
        assert len(result) == 80

    def test_safe_chars_unchanged(self):
        """Safe characters pass through unchanged."""
        assert sanitise_filename_part("hello-world_123") == "hello-world_123"


class TestDeriveEntryName:
    """Tests for derive_entry_name."""

    def test_priority_1_note(self):
        """Non-empty note takes priority."""
        entry = {"content": {"uri": "otpauth://totp/x?issuer=GitHub", "name": "fallback"}, "note": "My Note"}
        assert derive_entry_name(entry) == "My Note"

    def test_priority_2_issuer(self):
        """Issuer from URI when no note."""
        entry = {"content": {"uri": "otpauth://totp/x?secret=ABC&issuer=Google", "name": "fallback"}, "note": None}
        assert derive_entry_name(entry) == "Google"

    def test_priority_3_content_name(self):
        """content.name when no note and no issuer."""
        entry = {"content": {"uri": "otpauth://totp/x?secret=ABC", "name": "myaccount"}, "note": None}
        assert derive_entry_name(entry) == "myaccount"

    def test_fallback_unknown(self):
        """Returns 'unknown' when no name data available."""
        entry = {"content": {"uri": "", "entry_type": "Totp"}, "note": None}
        assert derive_entry_name(entry) == "unknown"

    def test_empty_note_not_used(self):
        """Empty string note is treated as no note."""
        entry = {"content": {"uri": "otpauth://totp/x?issuer=GitHub", "name": "fallback"}, "note": ""}
        assert derive_entry_name(entry) == "GitHub"
