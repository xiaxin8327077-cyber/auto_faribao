from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def test_application_has_one_canonical_root_entrypoint():
    assert (ROOT_DIR / "main.py").is_file()
    assert not (ROOT_DIR / "src" / "main.py").exists()
