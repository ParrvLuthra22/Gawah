from gawah_contract import export


def test_exported_schemas_are_up_to_date():
    for fname, text in export.render().items():
        path = export.SCHEMAS_DIR / fname
        assert path.exists(), f"{fname} missing: run `make contract`"
        assert path.read_text() == text, f"{fname} is stale: run `make contract`"


def test_no_stray_schema_files():
    expected = set(export.render())
    actual = {p.name for p in export.SCHEMAS_DIR.glob("*.json")}
    assert actual == expected
